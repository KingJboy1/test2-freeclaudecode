"""Provider construction from declarative profiles and exceptional adapters."""

from collections.abc import Callable

from free_claude_code.application.errors import UnknownProviderError
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.providers.admission import ProviderAdmissionController
from free_claude_code.providers.base import BaseProvider, ProviderConfig
from free_claude_code.providers.key_pool import (
    NIM_KEY_COOLDOWN_SECONDS,
    OPENROUTER_KEY_COOLDOWN_SECONDS,
    KeyPool,
    resolve_keys,
)
from free_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)

from .config import build_provider_config

ProviderFactory = Callable[
    [ProviderConfig, Settings, ProviderAdmissionController], BaseProvider
]


def _build_key_pool(
    provider_name: str,
    plural_raw: str,
    singular: str,
    per_key_rpm: int,
    rate_window: float,
    cooldown_seconds: float,
) -> KeyPool | None:
    """Build a KeyPool when 2+ keys resolve; else None (single-key path)."""
    keys = resolve_keys(plural_raw, singular)
    if len(keys) < 2:
        return None
    return KeyPool(
        provider_name=provider_name,
        keys=keys,
        per_key_rpm=per_key_rpm,
        rate_window=rate_window,
        cooldown_seconds=cooldown_seconds,
    )


def _pool_admission(
    provider_name: str, pool: KeyPool, config: ProviderConfig
) -> ProviderAdmissionController:
    """Size the aggregate admission gate to the pool's combined capacity."""
    return ProviderAdmissionController(
        provider_name=provider_name,
        rate_limit=pool.total_rpm,
        rate_window=config.rate_window or 60.0,
        max_concurrency=config.max_concurrency,
    )


def _create_nvidia_nim(
    config: ProviderConfig,
    settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.nvidia_nim import NvidiaNimProvider

    pool = _build_key_pool(
        provider_name="nvidia_nim",
        plural_raw=settings.nvidia_nim_api_keys,
        singular=config.api_key,
        per_key_rpm=settings.nvidia_nim_rpm_per_key,
        rate_window=config.rate_window or 60.0,
        cooldown_seconds=NIM_KEY_COOLDOWN_SECONDS,
    )
    if pool is not None:
        admission = _pool_admission("nvidia_nim", pool, config)
    return NvidiaNimProvider(
        config,
        nim_settings=settings.nim,
        admission=admission,
        key_pool=pool,
    )


def _create_open_router(
    config: ProviderConfig,
    settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.open_router import OpenRouterProvider

    pool = _build_key_pool(
        provider_name="open_router",
        plural_raw=settings.open_router_api_keys,
        singular=config.api_key,
        per_key_rpm=settings.open_router_rpm_per_key,
        rate_window=config.rate_window or 60.0,
        cooldown_seconds=OPENROUTER_KEY_COOLDOWN_SECONDS,
    )
    if pool is not None:
        admission = _pool_admission("open_router", pool, config)
    return OpenRouterProvider(config, admission=admission, key_pool=pool)


def _create_mistral(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.mistral import MistralProvider

    return MistralProvider(config, admission=admission)


def _create_deepseek(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.deepseek import DeepSeekProvider

    return DeepSeekProvider(config, admission=admission)


def _create_lmstudio(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.lmstudio import LMStudioProvider

    return LMStudioProvider(config, admission=admission)


def _create_cloudflare(
    config: ProviderConfig,
    settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.cloudflare import CloudflareProvider

    return CloudflareProvider(
        config,
        account_id=settings.cloudflare_account_id,
        admission=admission,
    )


def _create_gemini(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.gemini import GeminiProvider

    return GeminiProvider(config, admission=admission)


def _create_vertex(
    config: ProviderConfig,
    settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.vertex import VertexProvider

    return VertexProvider(
        config,
        project_id=settings.vertex_project_id,
        location=settings.vertex_location,
        admission=admission,
    )


def _create_github_models(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.github_models import GitHubModelsProvider

    return GitHubModelsProvider(config, admission=admission)


_SPECIAL_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "nvidia_nim": _create_nvidia_nim,
    "open_router": _create_open_router,
    "mistral": _create_mistral,
    "deepseek": _create_deepseek,
    "lmstudio": _create_lmstudio,
    "cloudflare": _create_cloudflare,
    "gemini": _create_gemini,
    "vertex": _create_vertex,
    "github_models": _create_github_models,
}

_profiled_ids = set(OPENAI_CHAT_PROFILES)
_special_ids = set(_SPECIAL_PROVIDER_FACTORIES)
if _profiled_ids & _special_ids or _profiled_ids | _special_ids != set(
    PROVIDER_CATALOG
):
    raise AssertionError(
        "Every provider must have exactly one construction owner: "
        f"profiles={_profiled_ids!r} special={_special_ids!r} "
        f"catalog={set(PROVIDER_CATALOG)!r}"
    )


def create_provider(provider_id: str, settings: Settings) -> BaseProvider:
    """Create a provider instance for a supported provider id."""
    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is None:
        raise UnknownProviderError.for_provider(provider_id, PROVIDER_CATALOG)

    config = build_provider_config(descriptor, settings)
    admission = ProviderAdmissionController(
        provider_name=provider_id,
        rate_limit=config.rate_limit or 40,
        rate_window=config.rate_window or 60.0,
        max_concurrency=config.max_concurrency,
    )
    factory = _SPECIAL_PROVIDER_FACTORIES.get(provider_id)
    if factory is not None:
        return factory(config, settings, admission)
    return create_openai_chat_provider(provider_id, config, admission)
