"""Per-key API-key rotation for rate-limited providers (NVIDIA NIM, OpenRouter).

A ``KeyPool`` spreads concurrent agent traffic across multiple API keys for one
provider so aggregate throughput scales with the key count instead of being
capped by a single key's per-minute quota.

Mechanism
---------
``OpenAIChatProvider`` already accepts an async ``api_key_provider`` callable
that the OpenAI SDK resolves fresh on every outgoing HTTP request. ``KeyPool``
implements that signature (``__call__``) and is passed as the credential
provider, so rotation is transparent to the streaming / SSE / request-shape
fallback logic — those layers are untouched. The lap loop sets a task-scoped
current key before each dispatch; the SDK reads it back through ``__call__``.

Design decisions (from the rotation spec)
------------------------------------------
* Per-key RPM window: each key owns its own ``StrictSlidingWindowLimiter`` — the
  exact primitive ``ProviderAdmissionController`` uses internally for RPM. A full
  per-key admission controller (recovery episodes + concurrency bulkhead) would be
  redundant and wrong-shaped: recovery is provider-wide, not per-key. Sharing one
  RPM budget across N keys would give zero extra throughput, so the per-key window
  is the unit of pacing.
* Warm rotation: the round-robin pointer advances on *every* dispatch (success or
  failure), spreading load before any key nears its ceiling so 429s are rare
  rather than routine.
* Strict round-robin: 1 -> 2 -> ... -> N -> 1, unconditional wraparound. No
  weighting / LRU (explicitly deferred).
* Predictive pacing: before dispatch we synchronously read a key's in-window
  count; a key at/near its ceiling is skipped like a cooldown (no await, no HTTP).
* Cooldown: only a 429 burns a key into cooldown (a monotonic next-available
  timestamp). 5xx / network / timeout are left to the existing admission /
  recovery machinery — never double-handled here.
* One full lap per request: try every loaded key once (skipping known-bad keys
  for free) before giving up. Scales with key count, not a fixed cap.
* Escalating backoff within a lap: 1st 429 -> rotate immediately (zero delay);
  2nd+ consecutive 429 -> short backoff (provider-wide saturation signal).
* Whole-pool exhaustion: raise ``KeyPoolExhaustedError`` naming the soonest
  recovery timestamp rather than blind-spinning or hanging.
* Concurrency safety: the currently-selected key is carried in a
  ``contextvars.ContextVar`` (task-scoped), so simultaneous in-flight requests
  through the same pool never clobber each other's credential. The pointer
  advance is fully synchronous (no await between read and write), so it is atomic
  under asyncio's single-threaded event loop without any lock.

Logging split: per-request key selection is DEBUG (noisy under heavy agent use);
cooldown / recovery state *changes* are INFO (what operators actually want).
Keys are masked in all logs (last 4 chars only).

Concurrency note
----------------
The OpenAI SDK resolves the credential provider per request via
``self.api_key = await self._api_key_provider()`` and then builds the
``Authorization`` header from the (shared) ``self.api_key`` attribute. Carrying
the selected key in a task-scoped ``ContextVar`` makes the provider call itself
concurrency-safe, and ``__call__`` is deliberately non-yielding so the key is
written atomically. There remains a narrow SDK-level window (between the key
refresh and header construction) in which two simultaneous in-flight requests
through the *same* shared client could exchange credentials. This is inherent to
the spec-mandated design of one shared client + ``api_key_provider`` (the same
pattern Vertex AI uses for token refresh). The consequence is bounded: a request
may occasionally carry a *sibling valid pool key* rather than the one its lap
selected — never an invalid key — so rotation, cooldown and pacing remain correct
in aggregate. Eliminating the window entirely would require injecting the
``Authorization`` header per request instead of via ``api_key_provider``.
"""

import asyncio
import contextvars
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

import openai
from loguru import logger

from free_claude_code.core.rate_limit import StrictSlidingWindowLimiter

T = TypeVar("T")

# --- Tunable defaults -------------------------------------------------------
# RPM-per-key is also exposed as an env setting (NVIDIA_NIM_RPM_PER_KEY /
# OPENROUTER_RPM_PER_KEY); these are the fallbacks. Cooldowns are code-level
# tunables per the spec ("exact value still tunable").
NVIDIA_NIM_RPM_PER_KEY_DEFAULT = 40
OPENROUTER_RPM_PER_KEY_DEFAULT = 20
NIM_KEY_COOLDOWN_SECONDS = 60.0  # spec: short/transient, ~30-60s
OPENROUTER_KEY_COOLDOWN_SECONDS = 3 * 3600.0  # flat 3h approximating the UTC-day cap
OVERLOAD_COOLDOWN_SECONDS = 30.0  # short cooldown for transient 5xx (502/503/529)
PACING_HEADROOM_DEFAULT = 2  # skip a key at (per_key_rpm - headroom); e.g. 38/40
ESCALATION_BACKOFF_SECONDS = 0.25  # 2nd+ consecutive 429 within a lap


def mask_key(key: str) -> str:
    """Return a short log-safe mask for an API key (never the full secret)."""
    if not key:
        return "<empty>"
    if len(key) <= 4:
        return "***"
    return f"***{key[-4:]}"


def parse_key_list(raw: str) -> list[str]:
    """Parse a JSON array of API keys; tolerate empty/blank input.

    Plural config is plain JSON (e.g. ``["k1","k2"]``). Invalid JSON or a
    non-array payload is treated as "no plural keys" (with a warning) so the
    singular fallback still applies. Blank entries are dropped.
    """
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "KEY_POOL: plural API-key config is not valid JSON ({}); ignoring it. "
            'Expected a JSON array like ["key1","key2"].',
            exc,
        )
        return []
    if not isinstance(parsed, list):
        logger.warning(
            "KEY_POOL: plural API-key config must be a JSON array; got {}",
            type(parsed).__name__,
        )
        return []
    return [item.strip() for item in parsed if isinstance(item, str) and item.strip()]


def resolve_keys(plural_raw: str, singular: str) -> list[str]:
    """Resolve the effective key list.

    Plural (``*_API_KEYS``) takes precedence when it yields at least one key;
    otherwise the singular (``*_API_KEY``) credential is used. The two are not
    merged (merging risks silent duplicates and surprising ordering).
    """
    keys = parse_key_list(plural_raw)
    if keys:
        return keys
    if not isinstance(singular, str):
        return []
    singular = singular.strip()
    return [singular] if singular else []


def _is_rate_limited(error: BaseException) -> bool:
    """Return whether an upstream error is an HTTP 429 (rate limit)."""
    return (
        isinstance(error, openai.RateLimitError)
        or getattr(error, "status_code", None) == 429
    )


def _is_overloaded(error: BaseException) -> bool:
    """Return whether an upstream error is a 5xx / overload / capacity error."""
    from free_claude_code.providers.failure_policy import is_transient_overload_error

    return is_transient_overload_error(error) or getattr(
        error, "status_code", None
    ) in (502, 503, 529)


class KeyPoolExhaustedError(RuntimeError):
    """Raised when every key in a pool is unavailable for a full lap.

    Carries the soonest monotonic recovery timestamp so callers/operators can see
    when the pool is expected to have a usable key again, instead of blind-spinning
    or hanging indefinitely.
    """

    def __init__(
        self,
        provider_name: str,
        soonest_recovery: float | None,
        last_error: Exception | None,
    ) -> None:
        self.provider_name = provider_name
        self.soonest_recovery = soonest_recovery
        self.last_error = last_error
        if soonest_recovery is not None:
            wait = max(0.0, soonest_recovery - time.monotonic())
            message = (
                f"{provider_name}: all API keys are rate-limited; "
                f"soonest recovery in ~{wait:.0f}s"
            )
        else:
            message = f"{provider_name}: all API keys are temporarily unavailable"
        if last_error is not None:
            message = f"{message} (last error: {type(last_error).__name__})"
        super().__init__(message)


@dataclass
class KeySlot:
    """One API key plus its per-key RPM window and cooldown state."""

    api_key: str
    limiter: StrictSlidingWindowLimiter
    cooldown_until: float = 0.0  # monotonic timestamp; 0 == never cooled down
    was_unavailable: bool = field(default=False, repr=False)  # recovery-log edge

    @property
    def fingerprint(self) -> str:
        """Short log-safe mask (never the full key)."""
        return mask_key(self.api_key)

    def in_cooldown(self, now: float) -> bool:
        return now < self.cooldown_until


class KeyPool:
    """Round-robin key rotation with per-key RPM pacing and 429 cooldowns.

    Implements the async credential-provider signature (``__call__``) expected by
    ``OpenAIChatProvider`` / the OpenAI SDK, and exposes ``create_with_rotation``
    which wraps one upstream create call with the per-key 429 lap.
    """

    def __init__(
        self,
        *,
        provider_name: str,
        keys: list[str],
        per_key_rpm: int,
        rate_window: float = 60.0,
        cooldown_seconds: float,
        pacing_headroom: int = PACING_HEADROOM_DEFAULT,
        escalation_backoff: float = ESCALATION_BACKOFF_SECONDS,
        overload_cooldown_seconds: float = OVERLOAD_COOLDOWN_SECONDS,
    ) -> None:
        if not keys:
            raise ValueError("KeyPool requires at least one API key")
        if per_key_rpm <= 0:
            raise ValueError("per_key_rpm must be > 0")
        if rate_window <= 0:
            raise ValueError("rate_window must be > 0")
        self._provider_name = provider_name
        self._per_key_rpm = int(per_key_rpm)
        self._rate_window = float(rate_window)
        self._cooldown_seconds = float(cooldown_seconds)
        # Skip a key once it reaches (per_key_rpm - headroom); floor at 1 so a
        # tiny quota still admits at least one in-flight request.
        self._pacing_threshold = max(
            1, self._per_key_rpm - max(0, int(pacing_headroom))
        )
        self._escalation_backoff = float(escalation_backoff)
        self._overload_cooldown_seconds = float(overload_cooldown_seconds)
        self._slots = [
            KeySlot(
                api_key=key,
                limiter=StrictSlidingWindowLimiter(
                    self._per_key_rpm, self._rate_window
                ),
            )
            for key in keys
        ]
        self._index = 0  # round-robin pointer
        self._select_lock = asyncio.Lock()  # guard index advancement under concurrency
        # Task-scoped current key => concurrency-safe across simultaneous requests.
        self._current_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"{provider_name}_current_key", default=None
        )
        logger.info(
            "KEY_POOL: {} initialized with {} keys ({} req/{}s per key, "
            "cooldown {:.0f}s, pacing threshold {}/{})",
            provider_name,
            len(self._slots),
            self._per_key_rpm,
            int(self._rate_window),
            self._cooldown_seconds,
            self._pacing_threshold,
            self._per_key_rpm,
        )

    # --- introspection -----------------------------------------------------
    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def key_count(self) -> int:
        return len(self._slots)

    @property
    def per_key_rpm(self) -> int:
        return self._per_key_rpm

    @property
    def total_rpm(self) -> int:
        """Aggregate per-window capacity across all keys.

        Used to size the provider's aggregate admission gate so it does not
        artificially cap the pool below its real combined throughput.
        """
        return self._per_key_rpm * len(self._slots)

    def cooldown_remaining(self) -> dict[str, float]:
        """Return seconds of cooldown remaining per (masked) key, for diagnostics."""
        now = time.monotonic()
        return {
            slot.fingerprint: max(0.0, slot.cooldown_until - now)
            for slot in self._slots
            if slot.in_cooldown(now)
        }

    # --- credential provider (OpenAI SDK calls this per outgoing request) ---
    async def __call__(self) -> str:
        """Return the credential for the next outgoing HTTP request.

        Inside ``create_with_rotation`` the lap loop sets the task-scoped current
        key before each dispatch and we return it here. Outside the lap (e.g.
        ``models.list``) we fall back to a best-effort warm-rotation pick so a
        valid key is always returned.
        """
        key = self._current_key.get()
        if key is not None:
            return key
        async with self._select_lock:
            slot = self._select_eligible()
            if slot is None:
                # Everything is momentarily blocked; still hand back the next key in
                # the ring rather than failing a non-streaming listing call.
                slot = self._slots[self._index % len(self._slots)]
            return slot.api_key

    # --- rotation core -----------------------------------------------------
    def _is_eligible(self, slot: KeySlot, now: float) -> bool:
        """Synchronous local-state check: cooldown OR near-ceiling => ineligible."""
        if slot.in_cooldown(now):
            return False
        return slot.limiter.count_in_window() < self._pacing_threshold

    def _select_eligible(self, tried: set[int] | None = None) -> KeySlot | None:
        """Advance the pointer (synchronously) to the next eligible key.

        Returns None only if a full scan of the ring finds no key that is both
        eligible and not already tried this lap. Ineligible / already-tried keys
        are skipped for free (no HTTP call). The pointer advance has no await
        between read and write, so it is atomic under asyncio without a lock.
        """
        now = time.monotonic()
        n = len(self._slots)
        for _ in range(n):
            slot = self._slots[self._index]
            self._index = (self._index + 1) % n
            if tried is not None and id(slot) in tried:
                continue
            if self._is_eligible(slot, now):
                self._note_recovery(slot, now)
                return slot
        return None

    def _note_recovery(self, slot: KeySlot, now: float) -> None:
        """Log INFO once when a key transitions from cooldown back into rotation."""
        if slot.was_unavailable and not slot.in_cooldown(now):
            logger.info(
                "KEY_POOL: {} key {} recovered and re-entered rotation",
                self._provider_name,
                slot.fingerprint,
            )
        slot.was_unavailable = False

    def _enter_cooldown(
        self, slot: KeySlot, *, cooldown_override: float | None = None
    ) -> None:
        """Put a key into cooldown after a 429 or 5xx and log the state change."""
        now = time.monotonic()
        cooldown = (
            cooldown_override
            if cooldown_override is not None
            else self._cooldown_seconds
        )
        slot.cooldown_until = now + cooldown
        slot.was_unavailable = True
        logger.info(
            "KEY_POOL: {} key {} cooled down for {:.0f}s",
            self._provider_name,
            slot.fingerprint,
            cooldown,
        )

    def _soonest_recovery(self) -> float | None:
        """Return the soonest monotonic timestamp at which a cooled key frees up."""
        now = time.monotonic()
        blocked = [slot.cooldown_until for slot in self._slots if slot.in_cooldown(now)]
        return min(blocked) if blocked else None

    async def create_with_rotation(
        self,
        create_fn: Callable[[], Awaitable[T]],
    ) -> T:
        """Run one upstream create call with per-key 429 rotation.

        Tries each loaded key at most once (one full lap). Only HTTP 429 triggers
        cooldown + rotation; any other error propagates to the caller's existing
        admission / recovery handling (5xx / network / timeout are not key-specific
        and must not be double-handled here). Escalating backoff: the first 429
        rotates immediately (zero delay); the 2nd+ consecutive 429 waits a short
        beat before the next key (a provider-wide saturation signal).
        """
        n = len(self._slots)
        tried: set[int] = set()
        consecutive_429 = 0
        last_error: Exception | None = None

        for attempt in range(1, n + 1):
            slot = self._select_eligible(tried)
            if slot is None:
                break  # every key is cooldown/pacing-blocked (or already tried)
            tried.add(id(slot))
            # Escalating backoff: zero delay after the 1st 429, short beat after
            # the 2nd+ consecutive 429 within this lap.
            if consecutive_429 >= 2:
                await asyncio.sleep(self._escalation_backoff)
            slot.limiter.record_dispatch()
            token = self._current_key.set(slot.api_key)
            logger.debug(
                "KEY_POOL: {} dispatching via key {} (lap attempt {}/{})",
                self._provider_name,
                slot.fingerprint,
                attempt,
                n,
            )
            try:
                return await create_fn()
            except Exception as error:
                if _is_rate_limited(error):
                    last_error = error
                    consecutive_429 += 1
                    self._enter_cooldown(slot)
                elif _is_overloaded(error):
                    last_error = error
                    self._enter_cooldown(
                        slot, cooldown_override=self._overload_cooldown_seconds
                    )
                else:
                    raise
            finally:
                self._current_key.reset(token)

        raise KeyPoolExhaustedError(
            self._provider_name, self._soonest_recovery(), last_error
        )
