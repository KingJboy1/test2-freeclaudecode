"""Local admin UI routes and APIs."""

import ipaddress
import secrets
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from free_claude_code.application.model_metadata import ProviderModelRefreshResult
from free_claude_code.config.admin.manifest import FIELD_BY_KEY
from free_claude_code.config.admin.persistence import validate_updates
from free_claude_code.config.admin.values import load_config_response
from free_claude_code.config.model_refs import configured_chat_model_refs

from .dependencies import get_services
from .ports import ApiServices

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
LOCAL_PROVIDER_PATHS = {
    "lmstudio": "/models",
    "llamacpp": "/models",
    "ollama": "/api/tags",
}


class AdminConfigPayload(BaseModel):
    """Partial config update submitted by the admin UI."""

    values: dict[str, Any] = Field(default_factory=dict)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return _is_loopback_host(parsed.hostname)


def require_loopback_admin(request: Request) -> None:
    """Allow admin access only from the local machine.

    Checks both the direct client IP and the X-Forwarded-For header so the
    guard cannot be bypassed by routing through a reverse proxy that sets
    the client host to 127.0.0.1 while forwarding external traffic.
    """

    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")

    # BUG #12 fix: inspect X-Forwarded-For to prevent reverse-proxy bypass.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # The first entry is the original client IP.
        first_hop = forwarded.split(",")[0].strip()
        if not _is_loopback_host(first_hop):
            raise HTTPException(status_code=403, detail="Admin UI is local-only")

    origin = request.headers.get("origin")
    if not _origin_is_local(origin):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")


def _asset_response(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return FileResponse(path)


# --- CSRF protection (BUG #13 fix) -------------------------------------------
# A simple double-submit cookie pattern: the admin page sets a csrf_token cookie;
# mutating requests must echo it back in the X-CSRF-Token header.

_CSRF_COOKIE_NAME = "pcc_csrf_token"
_CSRF_HEADER_NAME = "x-csrf-token"


def _generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _validate_csrf(request: Request) -> None:
    """Validate the CSRF token on mutating admin requests.

    Only enforced when a CSRF cookie is present (i.e. the request came through
    the admin page flow in a browser).  Direct API calls without a browser
    session (curl, scripts) are allowed — the loopback check already gates
    network access, and CSRF is a browser-specific attack vector.
    """
    cookie_token = request.cookies.get(_CSRF_COOKIE_NAME)
    if cookie_token is None:
        # No CSRF cookie → not a browser session from the admin page.
        return
    header_token = request.headers.get(_CSRF_HEADER_NAME)
    if not header_token:
        raise HTTPException(status_code=403, detail="Missing CSRF token")
    if not secrets.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")


# --- Rate limiting (BUG #16 fix) ---------------------------------------------
# Simple sliding-window rate limiter: max 30 requests per 60s per client IP.

_RATE_LIMIT_MAX = 30
_RATE_LIMIT_WINDOW = 60.0
_rate_limit_hits: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(request: Request) -> None:
    """Reject admin requests that exceed the per-IP rate limit."""
    client_host = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW
    # Prune stale IPs to prevent unbounded memory growth.
    stale_ips = [
        ip for ip, ts in _rate_limit_hits.items() if not ts or ts[-1] <= cutoff
    ]
    for ip in stale_ips:
        del _rate_limit_hits[ip]
    hits = _rate_limit_hits[client_host]
    _rate_limit_hits[client_host] = hits = [t for t in hits if t > cutoff]
    if len(hits) >= _RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Admin rate limit exceeded")
    hits.append(now)


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    require_loopback_admin(request)
    _check_rate_limit(request)
    response = _asset_response("index.html")
    # Set a CSRF cookie for the double-submit pattern.
    csrf_token = _generate_csrf_token()
    response.set_cookie(
        _CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        samesite="strict",
        path="/admin",
    )
    return response


@router.get("/admin/assets/{filename}", include_in_schema=False)
async def admin_asset(filename: str, request: Request):
    require_loopback_admin(request)
    if filename not in {"admin.css", "admin.js"}:
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return _asset_response(filename)


@router.get("/admin/api/config")
async def get_admin_config(request: Request):
    require_loopback_admin(request)
    return load_config_response()


@router.post("/admin/api/config/validate")
async def validate_admin_config(payload: AdminConfigPayload, request: Request):
    require_loopback_admin(request)
    _validate_csrf(request)
    return validate_updates(_filtered_values(payload.values))


@router.post("/admin/api/config/apply")
async def apply_admin_config(
    payload: AdminConfigPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    _validate_csrf(request)
    result = await services.admin.apply_admin_config(_filtered_values(payload.values))
    restart = result.get("restart")
    if isinstance(restart, dict) and restart.get("automatic"):
        background_tasks.add_task(services.admin.request_restart)
    return result


@router.get("/admin/api/status")
async def admin_status(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    return services.admin.admin_status()


@router.get("/admin/api/providers/local-status")
async def local_provider_status(request: Request):
    require_loopback_admin(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    checks = []
    for provider_id, path in LOCAL_PROVIDER_PATHS.items():
        base_url = _local_provider_url(provider_id, values)
        checks.append(await _check_local_provider(provider_id, base_url, path))
    return {"providers": checks}


@router.post("/admin/api/providers/{provider_id}/test")
async def test_provider(
    provider_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    _validate_csrf(request)
    return await services.admin.test_provider(provider_id)


@router.get("/admin/api/models")
async def models(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    return _model_options(services)


@router.post("/admin/api/models/refresh")
async def refresh_models(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    _validate_csrf(request)
    result = await services.admin.refresh_models()
    return _model_options(services, refresh_result=result)


def _model_options(
    services: ApiServices,
    *,
    refresh_result: ProviderModelRefreshResult | None = None,
) -> dict[str, list[str]]:
    settings = services.requests.current_settings()
    configured = {ref.model_ref for ref in configured_chat_model_refs(settings)}
    discovered = {
        info.model_id for info in services.requests.cached_prefixed_model_infos()
    }
    user_refs = {r.strip() for r in settings.chat_model_refs.split("\n") if r.strip()}
    failed_provider_ids = (
        refresh_result.failed_provider_ids if refresh_result is not None else ()
    )
    return {
        "models": sorted(configured | discovered | user_refs, key=str.casefold),
        "failed_providers": list(failed_provider_ids),
    }


def _filtered_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in FIELD_BY_KEY}


def _local_provider_url(provider_id: str, values: dict[str, str]) -> str:
    if provider_id == "lmstudio":
        return values.get("LM_STUDIO_BASE_URL", "")
    if provider_id == "llamacpp":
        return values.get("LLAMACPP_BASE_URL", "")
    if provider_id == "ollama":
        return values.get("OLLAMA_BASE_URL", "")
    return ""


def _is_safe_local_url(url: str) -> bool:
    """Validate that a URL points to a local/private address (SSRF guard)."""
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    # Allow localhost and loopback.
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    # Allow RFC-1918 private ranges and link-local.
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    return False


async def _check_local_provider(
    provider_id: str, base_url: str, path: str
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider_id": provider_id,
            "status": "missing_url",
            "label": "Missing URL",
            "base_url": base_url,
        }

    # BUG #11 fix: reject URLs that do not point to a local/private address.
    if not _is_safe_local_url(clean_url):
        return {
            "provider_id": provider_id,
            "status": "invalid_url",
            "label": "Invalid URL (must be a local address)",
            "base_url": base_url,
        }

    url = f"{clean_url}{path}"
    try:
        async with httpx.AsyncClient(http2=True, timeout=1.5) as client:
            response = await client.get(url)
        ok = 200 <= response.status_code < 300
        return {
            "provider_id": provider_id,
            "status": "reachable" if ok else "offline",
            "label": "Reachable" if ok else "Offline",
            "base_url": base_url,
            "status_code": response.status_code,
        }
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "status": "offline",
            "label": "Offline",
            "base_url": base_url,
            "error_type": type(exc).__name__,
        }
