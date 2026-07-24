"""Contracts for per-key API-key rotation (KeyPool) and its config/factory wiring."""

import asyncio
import time

import httpx
import openai
import pytest

from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.rate_limit import StrictSlidingWindowLimiter
from free_claude_code.providers.key_pool import (
    ESCALATION_BACKOFF_SECONDS,
    KeyPool,
    KeyPoolExhaustedError,
    mask_key,
    parse_key_list,
    resolve_keys,
)
from free_claude_code.providers.runtime.config import (
    has_provider_configuration,
    provider_credential,
)
from free_claude_code.providers.runtime.factory import create_provider


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _http_error(status: int) -> Exception:
    request = httpx.Request("POST", "https://x/v1/chat/completions")
    response = httpx.Response(status, request=request)
    if status == 429:
        return openai.RateLimitError("rate limited", response=response, body=None)
    if 500 <= status <= 599:
        return openai.InternalServerError("server error", response=response, body=None)
    return openai.APIStatusError("api error", response=response, body=None)


def _pool(keys, *, per_key_rpm=1000, cooldown=60.0, headroom=2, escalation=0.25):
    return KeyPool(
        provider_name="TEST",
        keys=list(keys),
        per_key_rpm=per_key_rpm,
        rate_window=60.0,
        cooldown_seconds=cooldown,
        pacing_headroom=headroom,
        escalation_backoff=escalation,
    )


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #
def test_parse_key_list_json_array():
    assert parse_key_list('["a","b","c"]') == ["a", "b", "c"]


def test_parse_key_list_strips_blank_entries():
    assert parse_key_list('["a", "  ", "", "b"]') == ["a", "b"]


def test_parse_key_list_empty_and_blank():
    assert parse_key_list("") == []
    assert parse_key_list("   ") == []


def test_parse_key_list_invalid_json_is_empty():
    assert parse_key_list("not json") == []
    assert parse_key_list("a,b,c") == []


def test_parse_key_list_non_array_is_empty():
    assert parse_key_list('{"a": 1}') == []
    assert parse_key_list('"just a string"') == []


def test_resolve_keys_plural_precedence():
    assert resolve_keys('["p1","p2"]', "single") == ["p1", "p2"]


def test_resolve_keys_singular_fallback():
    assert resolve_keys("", "single") == ["single"]
    assert resolve_keys("[]", "single") == ["single"]
    assert resolve_keys("bad json", "single") == ["single"]


def test_resolve_keys_none_when_both_empty():
    assert resolve_keys("", "") == []
    assert resolve_keys("[]", "  ") == []


def test_mask_key():
    assert mask_key("sk-abcdef1234") == "***1234"
    assert mask_key("abcd") == "***"
    assert mask_key("") == "<empty>"


# --------------------------------------------------------------------------- #
# limiter synchronous accessors (predictive pacing primitives)
# --------------------------------------------------------------------------- #
def test_limiter_count_in_window_and_record_dispatch():
    limiter = StrictSlidingWindowLimiter(5, 60.0)
    assert limiter.count_in_window() == 0
    limiter.record_dispatch()
    limiter.record_dispatch()
    assert limiter.count_in_window() == 2


def test_limiter_count_in_window_prunes_expired():
    limiter = StrictSlidingWindowLimiter(5, 0.05)
    limiter.record_dispatch()
    assert limiter.count_in_window() == 1
    time.sleep(0.08)
    assert limiter.count_in_window() == 0  # expired entry pruned synchronously


# --------------------------------------------------------------------------- #
# rotation behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_with_rotation_returns_value_on_success():
    pool = _pool(["k0", "k1"])

    async def create():
        return "RESULT:" + await pool()

    result = await pool.create_with_rotation(create)
    assert result.startswith("RESULT:")


@pytest.mark.asyncio
async def test_rotates_to_next_key_on_429():
    pool = _pool(["k0", "k1", "k2"])
    seen = []

    async def create():
        key = await pool()
        seen.append(key)
        if len(seen) == 1:
            raise _http_error(429)
        return key

    result = await pool.create_with_rotation(create)
    assert len(seen) == 2
    assert seen[0] != seen[1], "should rotate to a different key after 429"
    assert result == seen[1]


@pytest.mark.asyncio
async def test_warm_rotation_advances_pointer_across_requests():
    pool = _pool(["k0", "k1", "k2"])
    first_keys = []

    for _ in range(3):

        async def create():
            return await pool()

        first_keys.append(await pool.create_with_rotation(create))

    # Strict round-robin: successive requests start where the previous left off.
    assert first_keys == ["k0", "k1", "k2"]


@pytest.mark.asyncio
async def test_one_full_lap_tries_each_key_exactly_once():
    pool = _pool(["a", "b", "c", "d"])
    seen = []

    async def create():
        key = await pool()
        seen.append(key)
        raise _http_error(429)

    with pytest.raises(KeyPoolExhaustedError):
        await pool.create_with_rotation(create)

    assert len(seen) == 4
    assert len(set(seen)) == 4, "each key tried exactly once per lap"


@pytest.mark.asyncio
async def test_cooldown_skips_key_until_expiry_then_recovers():
    pool = _pool(["k0", "k1"], cooldown=0.15)

    # First request: k0 429s -> cooldown; succeeds on k1.
    calls = []

    async def create_first():
        key = await pool()
        calls.append(key)
        if key == "k0":
            raise _http_error(429)
        return key

    await pool.create_with_rotation(create_first)
    assert "k0" in calls

    # Immediately after, k0 is in cooldown -> next request must use k1 only.
    async def create_second():
        return await pool()

    second = await pool.create_with_rotation(create_second)
    assert second == "k1", "k0 should be cooled down and skipped"

    # After the cooldown expires, k0 is eligible again.
    await asyncio.sleep(0.2)
    # Force the pointer onto k0 to prove it is eligible again.
    pool._index = 0

    async def create_third():
        return await pool()

    third = await pool.create_with_rotation(create_third)
    assert third == "k0", "k0 should recover after cooldown expiry"


@pytest.mark.asyncio
async def test_predictive_pacing_skips_near_ceiling_key():
    # per_key_rpm=5, headroom=2 -> threshold=3; a key with 3 in-window dispatches
    # is treated like cooldown and skipped.
    pool = _pool(["k0", "k1"], per_key_rpm=5, headroom=2)
    for _ in range(3):
        pool._slots[0].limiter.record_dispatch()
    pool._index = 0  # start at the saturated key

    async def create():
        return await pool()

    chosen = await pool.create_with_rotation(create)
    assert chosen == "k1", "saturated key should be skipped via pacing"


@pytest.mark.asyncio
async def test_escalating_backoff_first_429_is_immediate():
    # 2 keys: k0 429s, k1 succeeds. 1st 429 -> rotate with zero delay.
    pool = _pool(["k0", "k1"], escalation=0.25)
    seen = []

    async def create():
        key = await pool()
        seen.append(key)
        if key == "k0":
            raise _http_error(429)
        return key

    start = time.monotonic()
    await pool.create_with_rotation(create)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"first 429 rotation should be immediate, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_escalating_backoff_second_consecutive_429_waits():
    # 3 keys all 429: the 3rd dispatch follows the 2nd consecutive 429 -> one
    # escalation backoff (~0.25s) is incurred before it.
    pool = _pool(["k0", "k1", "k2"], escalation=0.25)

    async def create():
        await pool()
        raise _http_error(429)

    start = time.monotonic()
    with pytest.raises(KeyPoolExhaustedError):
        await pool.create_with_rotation(create)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2, f"2nd+ consecutive 429 should back off, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_pool_exhausted_names_soonest_recovery():
    pool = _pool(["k0", "k1"], cooldown=60.0)

    async def create():
        await pool()
        raise _http_error(429)

    with pytest.raises(KeyPoolExhaustedError) as excinfo:
        await pool.create_with_rotation(create)

    err = excinfo.value
    assert err.soonest_recovery is not None
    assert err.soonest_recovery > time.monotonic()
    assert "rate-limited" in str(err)
    assert isinstance(err.last_error, openai.RateLimitError)


@pytest.mark.asyncio
async def test_non_429_error_propagates_without_rotation():
    pool = _pool(["k0", "k1", "k2"])
    seen = []

    async def create():
        seen.append(await pool())
        raise _http_error(500)  # 5xx is admission/recovery territory, not key-specific

    with pytest.raises(openai.InternalServerError):
        await pool.create_with_rotation(create)

    assert len(seen) == 1, "5xx must propagate immediately, not rotate keys"


@pytest.mark.asyncio
async def test_call_fallback_without_lap_context():
    # models.list path: no current key set -> best-effort warm pick.
    pool = _pool(["k0", "k1"])
    key = await pool()
    assert key in {"k0", "k1"}


# --------------------------------------------------------------------------- #
# concurrency safety (contextvar isolation)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_contextvar_isolates_concurrent_current_keys():
    pool = _pool(["k0", "k1"])

    async def task(value: str) -> str:
        pool._current_key.set(value)
        await asyncio.sleep(0.02)  # let the other task run and set its own value
        return await pool()

    a, b = await asyncio.gather(task("keyA"), task("keyB"))
    assert a == "keyA" and b == "keyB", (
        "concurrent tasks must see their own current key"
    )


@pytest.mark.asyncio
async def test_concurrent_requests_all_use_valid_pool_keys():
    pool = _pool(["k0", "k1", "k2"])
    valid = {"k0", "k1", "k2"}

    async def one_request():
        async def create():
            return await pool()

        return await pool.create_with_rotation(create)

    results = await asyncio.gather(*(one_request() for _ in range(12)))
    assert all(r in valid for r in results)
    assert set(results) == valid, (
        "warm rotation should spread concurrent load across keys"
    )


# --------------------------------------------------------------------------- #
# factory + config integration
# --------------------------------------------------------------------------- #
def test_factory_builds_pool_for_plural_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", '["or1","or2","or3"]')
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings()
    provider = create_provider("open_router", settings)
    try:
        assert provider._key_pool is not None
        assert provider._key_pool.key_count == 3
        # aggregate admission gate sized to combined capacity (3 * 20 rpm)
        assert provider._admission._proactive_limiter._rate_limit == 60
        assert provider._client._api_key_provider is provider._key_pool
        assert provider._http_client is not None  # shared HTTP/2 client
    finally:
        # cleanup is async; drop the reference without awaiting in a sync test
        pass


def test_factory_single_key_builds_no_pool(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "solo")
    settings = Settings()
    provider = create_provider("open_router", settings)
    assert provider._key_pool is None
    assert provider._client.api_key == "solo"  # unchanged single-key behaviour
    assert provider._http_client is None  # default internal client


def test_factory_nim_plural_uses_nim_rpm(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_API_KEYS", '["nv1","nv2"]')
    monkeypatch.setenv("NVIDIA_NIM_RPM_PER_KEY", "30")
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    settings = Settings()
    provider = create_provider("nvidia_nim", settings)
    assert provider._key_pool is not None
    assert provider._key_pool.key_count == 2
    assert provider._admission._proactive_limiter._rate_limit == 60  # 2 * 30


def test_has_provider_configuration_plural_only(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", '["a","b"]')
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings()
    assert has_provider_configuration(PROVIDER_CATALOG["open_router"], settings)


def test_has_provider_configuration_neither_set(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings()
    assert not has_provider_configuration(PROVIDER_CATALOG["open_router"], settings)


def test_provider_credential_plural_fallback(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", '["first","second"]')
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings()
    assert provider_credential(PROVIDER_CATALOG["open_router"], settings) == "first"


def test_provider_credential_singular_wins(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", '["first","second"]')
    monkeypatch.setenv("OPENROUTER_API_KEY", "singular")
    settings = Settings()
    assert provider_credential(PROVIDER_CATALOG["open_router"], settings) == "singular"


def test_escalation_default_constant():
    assert ESCALATION_BACKOFF_SECONDS == 0.25
