"""LLM provider — a thin, OpenRouter-backed unified gateway.

Every model (Claude, Gemini, GPT, …) is reached through OpenRouter's
OpenAI-compatible Chat Completions endpoint, so there is a single provider
implementation and no per-provider branching. The model is selected by its
OpenRouter slug, e.g.:

    anthropic/claude-opus-4.8
    google/gemini-3-pro-preview
    openai/gpt-5

The slug carries the upstream provider, so adding or changing models never
requires touching this file.

Public surface (kept stable for callers):

    ContentPart, TokenUsage, LLMProvider, create_provider
    RunBudget, BudgetExceededError, is_hard_llm_failure   # per-run spend control (C3)
    RunHealth, _reraise_if_fatal                          # loud-on-failure (C3)

    provider = create_provider(api_key)
    parsed, usage = provider.generate_structured(model, contents, schema,
                                                 thinking_budget=None)

`generate_structured` returns a validated Pydantic instance plus a TokenUsage.
"""

import base64
import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple, Type, Union

import httpx
from pydantic import BaseModel

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# The SDK's structured-output parse helper raises these before returning when a
# response is truncated or content-filtered; we catch them to emit a clear
# error. Imported defensively so the module stays importable without `openai`.
try:
    from openai import ContentFilterFinishReasonError, LengthFinishReasonError
    _LENGTH_ERRORS = (LengthFinishReasonError,)
    _FILTER_ERRORS = (ContentFilterFinishReasonError,)
except ImportError:  # openai is always present in production (it's the client)
    _LENGTH_ERRORS = _FILTER_ERRORS = ()

# The base SDK error type, used to gate the statusless credit/billing substring
# branch of is_hard_llm_failure so it can NEVER fire on a ValueError / pydantic
# ValidationError (those embed model output, i.e. PR-influenced bytes). Imported
# defensively so the module stays importable without `openai`.
try:
    from openai import APIError as _OpenAIAPIError
    _API_ERRORS = (_OpenAIAPIError,)
except ImportError:
    _API_ERRORS = ()

def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on bad/missing values."""
    try:
        return max(1, int(os.environ[name]))
    except (KeyError, TypeError, ValueError):
        return default


# Per-attempt request timeout (seconds) applied to the OpenAI/OpenRouter client.
# OpenRouter documents no server-side timeout, so an unresponsive upstream would
# otherwise hold a concurrency slot for the SDK default (~10 min). The SDK retries
# a timed-out attempt up to `max_retries` times *while the slot is held*, so the
# worst-case slot-hold is timeout * (max_retries + 1). With the defaults below
# (180 * (2 + 1)) that is ~9 min — bounded, yet generous enough for the slowest
# legitimate call (large escalated structured outputs, web-search grounding).
DEFAULT_REQUEST_TIMEOUT = 180.0

# Connect-phase timeout, split out from the (long) read timeout. A blackholed
# TCP connect would otherwise hold a concurrency slot for the FULL request
# timeout (~180s) before failing — starving the pool on a dead upstream — even
# though establishing a connection should take under a second (C7). Bounded well
# under the read timeout so a genuine connect failure recycles the slot fast,
# while a legitimately slow model response still gets the full read window.
CONNECT_TIMEOUT = 10.0

# Ceiling on the SDK retry budget. max_retries is the co-equal factor in the
# worst-case slot-hold (timeout * (max_retries + 1)), so an oversized value would
# silently defeat the bound the timeout guard protects. Operator-trusted config.
_MAX_RETRIES_CEILING = 8

# Upper ceiling on any single provider's configured concurrency, so one oversized
# value can't build a thousand-slot semaphore. NOTE this bounds each provider, not
# the aggregate: N explicitly-overridden providers can still reach N * ceiling
# in-flight. timeout/max_concurrency are operator-trusted config only — re-examine
# this bound before wiring max_concurrency from a leanrepo.toml PR checkout (X2).
_MAX_CONCURRENCY_CEILING = 32

# Cap on the number of concurrent in-flight API calls. OpenRouter rate-limits per
# account; the OpenAI SDK handles 429/Retry-After backoff, this just keeps us from
# stampeding it. Providers that don't pass an explicit `max_concurrency` share one
# process-wide default semaphore (preserving the historical global cap), sized from
# LLM_MAX_CONCURRENCY read at *construction* — not at import, so callers that set
# the env in main() before creating a provider are honored. Default 5 matches
# review.py's --max-workers. An explicit `max_concurrency` gets its own semaphore.
_default_semaphore_lock = threading.Lock()
_default_semaphore = None       # created on first default-provider construction
_default_semaphore_n = None     # the size the shared default was fixed at


def _clamp_concurrency(n) -> int:
    """Coerce a concurrency value into [1, _MAX_CONCURRENCY_CEILING].

    A floor of 1 is critical: Semaphore(0) would deadlock every API call across
    every tool. int() truncates a float and raises on a non-numeric value (a
    hard error is the right response to a malformed operator config).
    """
    return max(1, min(int(n), _MAX_CONCURRENCY_CEILING))


def _resolve_concurrency(max_concurrency):
    """Return (resolved_int, semaphore) for a provider.

    Explicit override → its own semaphore. Otherwise → the shared process-wide
    default, sized from LLM_MAX_CONCURRENCY at the first default-provider
    construction (subsequent default providers reuse that same object, so the
    global cap is preserved rather than multiplied per instance).
    """
    global _default_semaphore, _default_semaphore_n
    if max_concurrency is not None:
        n = _clamp_concurrency(max_concurrency)
        return n, threading.Semaphore(n)
    n = _clamp_concurrency(_env_int("LLM_MAX_CONCURRENCY", 5))
    with _default_semaphore_lock:
        if _default_semaphore is None:
            _default_semaphore = threading.Semaphore(n)
            _default_semaphore_n = n
        return _default_semaphore_n, _default_semaphore

# Tool-calling loop bounds (used only when an agent is given tools).
DEFAULT_MAX_TOOL_ROUNDS = 4      # model↔tool exchanges before forcing the answer
MAX_TOTAL_TOOL_CALLS = 12        # hard cap on tool executions per generation
MAX_TOOL_RESULT_CHARS = 4000     # truncate each tool result fed back to the model


@dataclass
class ContentPart:
    """Provider-agnostic content part.

    `cache=True` marks the part as a prompt-cache breakpoint. For caching to
    help, cacheable (stable, reused) parts should appear *before* volatile,
    per-request content — caching is a prefix match.
    """
    type: str          # "text", "pdf", "image"
    data: Union[str, bytes]
    mime_type: str = ""
    cache: bool = False


@dataclass
class TokenUsage:
    """Provider-agnostic token usage (as reported by OpenRouter)."""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    # Fail-CLOSED integrity signal for spend control (C3): True whenever a
    # completion returned but reported no usable cost figure (absent usage block,
    # or a missing/non-numeric/negative/NaN cost) — independent of token count. An
    # explicit finite 0.0 (a `:free` model) is KNOWN and does not set this. Cost
    # alone can't distinguish "genuinely free" from "cost unknown", so downstream
    # aggregate caps must be able to tell the difference rather than silently
    # under-counting to $0 on exactly the untrusted PRs the spend cap exists to bound.
    cost_missing: bool = False
    # True when OpenRouter reports this generation ran under BYOK (bring-your-own-key).
    # Verified 2026-07-08 against https://openrouter.ai/docs/use-cases/byok: under BYOK
    # `cost` is only OpenRouter's 5% fee (and can be 0.0 while real upstream spend
    # occurs), so a cost cap cannot be trusted — the token ceiling is authoritative
    # (R8). Distinct from cost_missing: the fee IS a known figure, the semantics
    # differ. OR-propagated in _sum_usage independently of cost_missing.
    byok: bool = False


def _sum_usage(a: "TokenUsage", b: "TokenUsage") -> "TokenUsage":
    """Add two TokenUsage records (to accumulate across tool-loop + final call)."""
    return TokenUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        thinking_tokens=a.thinking_tokens + b.thinking_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        cost=a.cost + b.cost,
        cost_missing=a.cost_missing or b.cost_missing,
        byok=a.byok or b.byok,
    )


# ---- per-run spend control (C3) -------------------------------------------

class BudgetExceededError(Exception):
    """Raised when a run's per-run token/cost ceiling (RunBudget) is exceeded.

    Carries `.usage` — a TokenUsage snapshot of the run totals at raise time — so
    the orchestration layer can report what was spent. The snapshot deliberately
    carries NO configured limit, so nothing renders the operator's ceiling into a
    PR-visible comment.
    """

    def __init__(self, message: str = "per-run LLM budget exceeded", *, usage: Optional[TokenUsage] = None):
        super().__init__(message)
        self.usage = usage if usage is not None else TokenUsage()


class LLMResponseEnvelopeError(RuntimeError):
    """A completion arrived structurally unusable: an OpenRouter in-body error
    object (HTTP 200 + ``{"error": ...}``) or an empty/missing ``choices`` list.

    Carries ``.status_code`` when the in-body error names one, so
    :func:`is_hard_llm_failure` applies its normal status-first policy — an
    in-body 402 is as hard as an HTTP 402; an in-body 429/5xx stays soft. A
    statusless envelope error is soft (RuntimeError is not in _API_ERRORS, so
    the statusless billing markers — which the error message, being provider/
    content-influenced, must never reach — are not consulted).
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code  # _status_code_of ignores a None
        self.call_usage: Optional["TokenUsage"] = None  # per-call billed usage (C7)


def _with_call_usage(exc: Exception, usage: "TokenUsage") -> Exception:
    """Attach the per-call billed usage to a raising exception so a caller's
    advisory accounting can record spend the failed call still incurred (C7).
    Distinct from BudgetExceededError.usage (run totals): call_usage is the delta
    for this single call, safe to record exactly once with no double-count."""
    exc.call_usage = usage
    return exc


# 403 disambiguation. OpenRouter returns 403 for BOTH an auth/permission failure
# and a *moderation-flagged input* (docs/api/reference/errors, verified 2026-07-08).
# The input is attacker-controllable PR content, so a bare 403 must NOT be treated
# as a hard account failure (it would let any PR fire the outage banner / redden a
# loud-exit job — content-dependent griefing). Only an auth-shaped 403 is hard.
_MODERATION_403_MARKERS = ("moderat", "flagged")
_AUTH_403_MARKERS = (
    "api key", "invalid key", "unauthorized", "not authorized",
    "authentication", "no auth credentials", "permission denied",
)
# Statusless credit/billing markers. Only consulted for genuine SDK/HTTP-layer
# errors (see _API_ERRORS gate) — NEVER for a ValueError/ValidationError, whose
# text can contain PR-influenced model output. "quota" is intentionally excluded:
# OpenAI 429s say "exceeded your current quota" and 429 is a soft/transient status.
_HARD_STATUSLESS_MARKERS = (
    "insufficient credit", "insufficient_quota", "payment required",
    "out of credits", "billing hard limit",
)


def _status_code_of(exc) -> Optional[int]:
    """Best-effort HTTP status for an SDK exception, across SDK versions."""
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int) and not isinstance(sc, bool):
        return sc
    resp = getattr(exc, "response", None)
    rc = getattr(resp, "status_code", None)
    return rc if isinstance(rc, int) and not isinstance(rc, bool) else None


def _is_auth_shaped_403(exc) -> bool:
    msg = str(exc).lower()
    if any(m in msg for m in _MODERATION_403_MARKERS):
        return False  # moderation-flagged input — attacker-controllable, not hard
    return any(a in msg for a in _AUTH_403_MARKERS)


def is_hard_llm_failure(exc: BaseException, _depth: int = 0) -> bool:
    """True for a spend/auth/quota failure that must be surfaced loudly, not swallowed.

    Status-code FIRST (the trustworthy signal): 401 (invalid key) and 402
    (insufficient credits / per-key limit) are hard; 403 is hard only when
    auth-shaped (moderation-403 is attacker-controllable PR content, so soft);
    408/429/5xx are NEVER hard even if their message mentions "quota" (status
    precedence). Only for a genuine SDK error carrying no status is a NARROW
    credit/billing substring set consulted — never for a ValueError/ValidationError
    (those embed model output). The `__cause__` chain is walked up to 2 levels so
    `raise ValueError(...) from <402>` stays fatal while the provider's own
    length-cap ValueError (wrapping LengthFinishReasonError, no status) stays soft.
    """
    status = _status_code_of(exc)
    if status is not None:
        if status in (401, 402):
            return True
        if status == 403:
            return _is_auth_shaped_403(exc)
        return False  # 400/404/408/409/422/429/5xx — soft/transient
    if _API_ERRORS and isinstance(exc, _API_ERRORS):
        msg = str(exc).lower()
        if any(m in msg for m in _HARD_STATUSLESS_MARKERS):
            return True
    if _depth < 2:
        cause = getattr(exc, "__cause__", None)
        if cause is not None and cause is not exc:
            return is_hard_llm_failure(cause, _depth + 1)
    return False


def _reraise_if_fatal(exc: BaseException) -> None:
    """First line of every LLM-touching broad ``except`` (R3): re-raise a budget
    trip or a hard LLM failure so it reaches the orchestration containment layer
    instead of being swallowed into a green check. Everything else is left for the
    caller's existing fail-soft handling."""
    if isinstance(exc, BudgetExceededError) or is_hard_llm_failure(exc):
        raise exc


class RunBudget:
    """Thread-safe per-run token/cost ceiling (C3).

    The TOKEN ceiling is authoritative (R8): OpenRouter `cost` can be unreliable
    (absent, or BYOK fee-only), so a cost-only budget is rejected at construction.
    All spend paths funnel usage through :meth:`record_and_check`; the provider
    consults :attr:`exceeded` before each extra call and :meth:`raise_if_exceeded`
    at fresh entry. Enforcement is conservative: the cost cap keeps enforcing
    against accumulated KNOWN cost even after cost reliability is lost.
    """

    def __init__(self, max_tokens: Optional[int] = None, max_cost: Optional[float] = None):
        if max_cost is not None and max_tokens is None:
            raise ValueError(
                "RunBudget rejects a cost-only budget: set max_tokens too. The token "
                "ceiling is the authoritative bound because OpenRouter cost can be "
                "absent or (under BYOK) fee-only."
            )
        if max_tokens is not None and not (
            isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0
        ):
            raise ValueError("max_tokens must be a positive int or None")
        if max_cost is not None and not (
            isinstance(max_cost, (int, float)) and not isinstance(max_cost, bool)
            and math.isfinite(max_cost) and max_cost > 0
        ):
            raise ValueError("max_cost must be a positive, finite number or None")
        self.max_tokens = max_tokens
        self.max_cost = float(max_cost) if max_cost is not None else None
        self._lock = threading.Lock()
        self._usage = TokenUsage()
        # Sticky: flips False on the first cost_missing/BYOK generation and never
        # flips back. The token cap is unaffected; only cost-cap trust is lost.
        self.cost_reliable = True
        self._warned_unreliable = False

    @classmethod
    def disabled(cls) -> "RunBudget":
        """A no-op budget (no ceilings). Still accumulates usage harmlessly."""
        return cls(None, None)

    @property
    def enabled(self) -> bool:
        return self.max_tokens is not None or self.max_cost is not None

    @staticmethod
    def _count(u: TokenUsage) -> int:
        # Authoritative token count = input + output. reasoning_tokens are a SUBSET
        # of output (completion) tokens and cached_tokens a subset of input, per the
        # OpenRouter usage schema (verified 2026-07-08), so they must not be re-added.
        return (u.input_tokens or 0) + (u.output_tokens or 0)

    def _over_locked(self) -> bool:
        if self.max_tokens is not None and self._count(self._usage) >= self.max_tokens:
            return True
        if self.max_cost is not None and self._usage.cost >= self.max_cost:
            return True
        return False

    def record_and_check(self, usage: TokenUsage) -> bool:
        """Accumulate `usage`; return True iff THIS call is the one that crossed a
        ceiling (atomic and observable — this is the signal the race test asserts on)."""
        with self._lock:
            was_over = self._over_locked()
            self._usage = _sum_usage(self._usage, usage)
            if (usage.cost_missing or usage.byok) and self.cost_reliable:
                self.cost_reliable = False
                if self.max_cost is not None and not self._warned_unreliable:
                    self._warned_unreliable = True
                    logging.warning(
                        "RunBudget: cost is no longer reliable (%s); the token ceiling "
                        "is authoritative for the rest of this run.",
                        "BYOK fee-only cost" if usage.byok else "a generation reported no cost",
                    )
            return (not was_over) and self._over_locked()

    @property
    def exceeded(self) -> bool:
        with self._lock:
            return self._over_locked()

    def raise_if_exceeded(self) -> None:
        with self._lock:
            if self._over_locked():
                raise BudgetExceededError(usage=self._snapshot_locked())

    def snapshot(self) -> TokenUsage:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> TokenUsage:
        u = self._usage
        return TokenUsage(
            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            thinking_tokens=u.thinking_tokens, cached_tokens=u.cached_tokens,
            cost=u.cost, cost_missing=u.cost_missing, byok=u.byok,
        )


class RunHealth:
    """Per-run health tracker (C3, R5/R9): did this run degrade, and how?

    The orchestration layer records into it from worker threads, so it is
    thread-safe (one Lock). It counts three things:

    - ``hard_failures``: how many LLM calls failed for a spend/auth/quota reason
      (classified by :func:`is_hard_llm_failure` at each fail-soft ``except`` site
      BEFORE re-raising, and at the top-level containment catch).
    - ``budget_exceeded`` + ``skipped_files``: whether the per-run :class:`RunBudget`
      tripped, and which files were skipped because of it (deterministic order,
      de-duped — pure orchestration state, so a model cannot forge it in its output).
    - ``fresh_successes``: genuinely fresh (non-cache, non-fallback) successful
      generations. This is bookkeeping for R5 tests; the loud-failure decision does
      NOT depend on it — :attr:`degraded` is driven only by hard failures / budget,
      so a warm cache or an all-fallback run can never SUPPRESS the banner (R5), and
      a clean run with zero hard failures never RAISES it (no false alarm).

    :attr:`degraded` is both the loud-banner trigger and what the orchestration ORs
    into ``review_incomplete`` (R9), so an all-402 run can never render Approved.
    Deliberately stores NO exception text — Actions logs are public on public repos,
    and nothing here is allowed to carry a PR-influenced message into a comment.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.fresh_successes = 0
        self.hard_failures = 0
        self.budget_exceeded = False
        self.skipped_files: List[str] = []

    def record_fresh_success(self) -> None:
        with self._lock:
            self.fresh_successes += 1

    def record_hard_failure(self) -> None:
        with self._lock:
            self.hard_failures += 1

    def record_budget_trip(self, *files: str) -> None:
        """Idempotent: flag the budget trip and append any skipped files (dedup,
        insertion order). A burst of N fresh-entry BudgetExceededError raises from
        already-queued workers is ONE budget event, not N failures."""
        with self._lock:
            self.budget_exceeded = True
            for f in files:
                if f and f not in self.skipped_files:
                    self.skipped_files.append(f)

    @property
    def degraded(self) -> bool:
        """True iff the run did not complete normally: a hard LLM failure occurred
        or the per-run budget tripped. The single source of truth for the loud
        banner and for forcing ``review_incomplete``."""
        with self._lock:
            return self.hard_failures > 0 or self.budget_exceeded


def parse_run_budget(max_tokens_raw: Optional[str], max_cost_raw: Optional[str]) -> Optional[RunBudget]:
    """Build a :class:`RunBudget` from raw env-string values (C3, STEP 3).

    Empty or whitespace-only == UNSET — this is the value EVERY default GitHub
    Action run sends once the ``LLM_MAX_RUN_*`` inputs exist (action inputs default
    to ``''``), so it must map to "no budget", not to an error. If BOTH are unset,
    returns ``None`` (disabled; ``create_provider`` then uses a no-op budget and
    existing callers are behaviourally unchanged).

    A NON-empty but invalid value ('0', negative, non-numeric) or a cost-only budget
    raises :class:`ValueError`, so the entrypoint can fail fast at startup — before
    any LLM call — rather than shipping the feature dark or silently unbounded.
    """
    t_raw = (max_tokens_raw or "").strip()
    c_raw = (max_cost_raw or "").strip()
    if not t_raw and not c_raw:
        return None
    max_tokens: Optional[int] = None
    if t_raw:
        try:
            max_tokens = int(t_raw)
        except ValueError:
            raise ValueError(f"max-run-tokens must be a positive integer, got {t_raw!r}")
    max_cost: Optional[float] = None
    if c_raw:
        try:
            max_cost = float(c_raw)
        except ValueError:
            raise ValueError(f"max-run-cost must be a positive number, got {c_raw!r}")
    # RunBudget enforces the rest: >0, finite, and cost-only rejection (R8).
    return RunBudget(max_tokens=max_tokens, max_cost=max_cost)


def describe_exc(exc: BaseException, max_len: int = 200) -> str:
    """A safe, single-line description of an exception for a PUBLIC log (C3, R6).

    GitHub Actions logs are world-readable on public repos, and exception messages
    can embed model output or provider payloads (potentially PR-influenced). This
    returns the exception CLASS, its HTTP status (if any), and a whitespace-collapsed,
    truncated message — enough to debug a spend/quota failure without dumping a full
    body. For PR-VISIBLE text (comments/annotations) do not use this at all: render a
    fixed generic label instead, so nothing dynamic reaches the comment channel.
    """
    status = _status_code_of(exc)
    msg = " ".join(str(exc).split())
    if len(msg) > max_len:
        msg = msg[: max_len] + "…"
    prefix = type(exc).__name__ + (f"(status={status})" if status is not None else "")
    return f"{prefix}: {msg}" if msg else prefix


def _data_url(data: Union[str, bytes], mime_type: str) -> str:
    """Return a data: URL for raw bytes, or pass through an existing URL/data URL."""
    if isinstance(data, str):
        return data  # already a URL or data: URL
    b64 = base64.standard_b64encode(data).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"


class OpenRouterProvider:
    """Single LLM provider backed by OpenRouter's OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 2,
        max_tokens: int = 16384,
        reasoning_default: Optional[dict] = None,
        require_parameters: bool = False,
        http_referer: Optional[str] = None,
        x_title: Optional[str] = None,
        length_retry_attempts: int = 2,
        length_retry_max_tokens: int = 65536,
        enable_web_search: bool = False,
        timeout: Optional[float] = None,
        max_concurrency: Optional[int] = None,
        budget: Optional["RunBudget"] = None,
    ):
        from openai import OpenAI

        # Per-run spend ceiling (C3). Default is a no-op budget so existing callers
        # (including sorry-tracker) are behaviourally unchanged; pass a configured
        # RunBudget to bound a single run's tokens/cost. Every generation path funnels
        # its usage through self._record_usage, the ONE authoritative sink.
        self.budget = budget if budget is not None else RunBudget.disabled()
        self.max_tokens = max_tokens
        # Per-attempt request timeout (see DEFAULT_REQUEST_TIMEOUT). The worst-case
        # concurrency-slot hold is timeout * (max_retries + 1) because the SDK
        # retries a timed-out attempt while the semaphore is held; max_retries is
        # kept low (2) so that product stays ~9 min. `timeout` is operator-trusted
        # config only — never sourced from an untrusted PR checkout.
        # Only a finite, positive number is a usable timeout. A non-positive value
        # (0 → httpx immediate expiry) or a non-finite one (inf/NaN → silently
        # defeats the slot-hold bound) falls back to the default rather than let a
        # mistyped config value brick the provider or disable the DoS mitigation.
        self.timeout = (
            timeout
            if (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                and math.isfinite(timeout) and timeout > 0)
            else DEFAULT_REQUEST_TIMEOUT
        )
        # Clamp the retry budget: it is the co-equal factor in the worst-case
        # slot-hold, ~= self.timeout * (self.max_retries + 1) (the SDK retries a
        # timed-out attempt while the concurrency slot is held), so an oversized or
        # negative value must not silently defeat the bound the timeout guard
        # protects. The bound EXCLUDES inter-retry backoff / Retry-After sleeps
        # (also held) and httpx applies `timeout` per read, not per whole request,
        # so it is a rough upper bound.
        self.max_retries = max(0, min(int(max_retries), _MAX_RETRIES_CEILING))
        # Resolve concurrency at construction (not import): a shared process-wide
        # default sized from LLM_MAX_CONCURRENCY, or a dedicated semaphore for an
        # explicit override. self._max_concurrency is the introspectable size;
        # self._api_semaphore is what the generation paths acquire.
        self._max_concurrency, self._api_semaphore = _resolve_concurrency(max_concurrency)
        # Opt-in web-search grounding via OpenRouter's `web` plugin: lets agents
        # look up definitions/results they aren't otherwise given, and returns
        # citations. Off by default (adds cost).
        self.enable_web_search = enable_web_search
        # When a structured response is truncated (finish_reason "length"), retry
        # with a doubled output cap up to `length_retry_attempts` times, capped at
        # `length_retry_max_tokens`. This keeps finding-heavy reviews of large
        # files from failing outright instead of returning their findings.
        self.length_retry_attempts = length_retry_attempts
        self.length_retry_max_tokens = length_retry_max_tokens
        # When True, OpenRouter only routes to providers that honor every
        # request param. Off by default: a model that doesn't support an
        # *optional* param (e.g. reasoning) then degrades gracefully instead of
        # hard-failing with a 404 "no endpoints found". Turn on only when you
        # need to guarantee a provider honors response_format.
        self.require_parameters = require_parameters
        # Applied to calls that don't pass an explicit thinking_budget. Lets a
        # workflow opt every call into reasoning without threading a budget
        # through each call site (e.g. {"effort": "high"}). None = model default.
        self.reasoning_default = reasoning_default

        default_headers = {
            "HTTP-Referer": http_referer
            or os.environ.get("OPENROUTER_HTTP_REFERER", "https://github.com/lean-workflows"),
            "X-Title": x_title or os.environ.get("OPENROUTER_X_TITLE", "lean-workflow"),
        }
        # Split the per-attempt timeout into a short connect phase and the full
        # read/write/pool window (C7): a dead/blackholed upstream fails the
        # connect in CONNECT_TIMEOUT instead of holding a slot for the whole
        # read timeout, while a slow-but-live model still gets self.timeout to
        # respond. The connect bound is clamped below self.timeout so an operator
        # who lowers the overall timeout never ends up with connect > read.
        request_timeout = httpx.Timeout(
            self.timeout,  # default for read/write/pool
            connect=min(CONNECT_TIMEOUT, self.timeout),
        )
        self.client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            max_retries=self.max_retries,  # clamped; SDK retries 429/5xx/timeout, honoring Retry-After
            timeout=request_timeout,  # per-attempt; bounds a stuck upstream (C2/C7)
            default_headers=default_headers,
        )

    @property
    def name(self) -> str:
        return "openrouter"

    # -- request construction -------------------------------------------------

    def _to_message_content(self, contents: List[ContentPart]) -> Tuple[list, bool]:
        """Convert ContentParts to OpenAI/OpenRouter message content blocks.

        Returns (blocks, has_pdf). PDFs require the file-parser plugin.
        """
        blocks = []
        has_pdf = False
        for part in contents:
            if part.type == "text":
                block = {"type": "text", "text": part.data}
                if part.cache:
                    block["cache_control"] = {"type": "ephemeral"}
                blocks.append(block)
            elif part.type == "image":
                block = {
                    "type": "image_url",
                    "image_url": {"url": _data_url(part.data, part.mime_type or "image/png")},
                }
                if part.cache:
                    block["cache_control"] = {"type": "ephemeral"}
                blocks.append(block)
            elif part.type == "pdf":
                has_pdf = True
                block = {
                    "type": "file",
                    "file": {
                        "filename": "document.pdf",
                        "file_data": _data_url(part.data, part.mime_type or "application/pdf"),
                    },
                }
                if part.cache:
                    block["cache_control"] = {"type": "ephemeral"}
                blocks.append(block)
            else:
                logging.warning(f"Unknown ContentPart type '{part.type}' — skipping")
        return blocks, has_pdf

    def _build_extra_body(self, thinking_budget: Optional[int], has_pdf: bool, healing: bool = True) -> dict:
        extra_body: dict = {}
        # Request usage accounting. Per the OpenRouter usage-accounting docs
        # (https://openrouter.ai/docs/api/reference/overview, fetched 2026-07-06)
        # this flag is now DEPRECATED and a no-op: `cost` and the token details are
        # returned on every response automatically. We still set it for literal
        # forward/backward-compatibility, but it is NOT what makes cost non-zero —
        # `_usage_from` reading the response `cost` field is. Real cost is proven by
        # the OPENROUTER_API_KEY-gated live test, since no mock can validate this.
        extra_body["usage"] = {"include": True}
        if self.require_parameters:
            extra_body["provider"] = {"require_parameters": True}

        reasoning = self._reasoning_for(thinking_budget)
        if reasoning is not None:
            extra_body["reasoning"] = reasoning

        plugins = []
        if healing:
            plugins.append({"id": "response-healing"})  # repair malformed structured JSON
        if has_pdf:
            plugins.append({"id": "file-parser", "pdf": {"engine": "native"}})
        if getattr(self, "enable_web_search", False):
            plugins.append({"id": "web"})  # OpenRouter web-search grounding
        if plugins:
            extra_body["plugins"] = plugins
        return extra_body

    def _reasoning_for(self, thinking_budget: Optional[int]) -> Optional[dict]:
        if thinking_budget and thinking_budget > 0:
            return {"max_tokens": int(thinking_budget)}
        return self.reasoning_default

    def _max_tokens_for(self, thinking_budget: Optional[int]) -> int:
        # Top-level max_tokens covers reasoning + the visible answer, so reserve
        # the full configured budget for the answer *on top of* the thinking
        # budget — otherwise large structured outputs truncate (finish_reason
        # "length") once reasoning eats into the cap.
        if thinking_budget and thinking_budget > 0:
            return self.max_tokens + int(thinking_budget)
        return self.max_tokens

    # -- generation -----------------------------------------------------------

    def generate_structured(
        self,
        model: str,
        contents: List[ContentPart],
        schema: Type[BaseModel],
        thinking_budget: Optional[int] = None,
        tools: Optional[list] = None,
        tool_runner=None,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> Tuple[BaseModel, TokenUsage]:
        """Generate schema-validated structured output.

        Returns (validated Pydantic instance, TokenUsage). Raises on failure
        after the SDK's built-in retries are exhausted.

        If `tools` and `tool_runner` are given, first run a bounded tool-calling
        loop (phase 1) so the model can gather evidence — e.g. query the Lean
        toolchain — then produce the structured answer (phase 2). Fail-open: if
        the tool phase errors (e.g. the model can't do tool calls), it falls back
        to a plain structured generation.
        """
        # Fresh entry already over budget → raise before spending anything (R4).
        # A trip that happens mid-run degrades gracefully instead (see below).
        self.budget.raise_if_exceeded()

        blocks, has_pdf = self._to_message_content(contents)
        messages = [{"role": "user", "content": blocks}]
        usage_total = TokenUsage()

        if tools and tool_runner:
            # rounds_usage collects each completed tool round's usage as it happens,
            # so a later-round exception can't lose the usage from earlier rounds
            # (the mid-raise usage-loss fix): the `finally` folds whatever was
            # gathered into usage_total on every exit path. The budget itself is
            # already updated per round inside _gather_with_tools via _record_usage.
            rounds_usage: List[TokenUsage] = []
            try:
                self._gather_with_tools(
                    model, messages, tools, tool_runner, thinking_budget, has_pdf,
                    max_tool_rounds, rounds_usage,
                )
                messages.append({
                    "role": "user",
                    "content": "Now provide your final answer as JSON matching the required schema, using the evidence gathered above.",
                })
            except Exception as e:
                # R3: a budget trip or a hard LLM failure (402/auth/quota) must NOT be
                # swallowed into a fail-open tool-less retry — re-raise it to the
                # orchestration containment layer. Only genuinely soft failures (a
                # model that can't do tool calls, a transient blip) fall back.
                _reraise_if_fatal(e)
                logging.warning(f"Tool phase failed for '{model}'; continuing without tools: {e}")
            finally:
                for u in rounds_usage:
                    usage_total = _sum_usage(usage_total, u)

        extra_body = self._build_extra_body(thinking_budget, has_pdf)

        max_tokens = self._max_tokens_for(thinking_budget)
        completion = None
        for attempt in range(self.length_retry_attempts + 1):
            with self._api_semaphore:
                try:
                    completion = self._parse(
                        model=model,
                        messages=messages,
                        response_format=schema,
                        max_tokens=max_tokens,
                        extra_body=extra_body,
                    )
                except _LENGTH_ERRORS as e:
                    # A truncated attempt is still a real, billed generation (it
                    # produced a full max_tokens of output). Account for its usage
                    # before escalating so large-output spend isn't silently dropped
                    # from the returned total. The error carries the completion; if
                    # usage is absent, _usage_from fails closed (cost_missing).
                    truncated = getattr(e, "completion", None)
                    if truncated is not None:
                        usage_total = _sum_usage(usage_total, self._record_usage(truncated))
                    else:
                        # Fail closed (symmetry with _usage_from's absent-usage path):
                        # the attempt still burned a full max_tokens of billed output,
                        # but with no completion we can't count it — record an unknown
                        # marker so the cost cap stops trusting its running total.
                        self.budget.record_and_check(TokenUsage(cost_missing=True))
                    # Truncated before complete structured output. Normally retry with
                    # a larger cap so the review's findings aren't lost wholesale — but
                    # if the run is now over budget, do NOT escalate (each escalation is
                    # another billed generation up to length_retry_max_tokens); take the
                    # output-cap failure path so the overshoot stays bounded (decision 3).
                    if (attempt >= self.length_retry_attempts
                            or max_tokens >= self.length_retry_max_tokens
                            or self.budget.exceeded):
                        raise _with_call_usage(ValueError(
                            f"Model '{model}' hit the output token cap ({max_tokens}) before "
                            f"producing complete structured output, even after "
                            f"{self.length_retry_attempts} escalation(s); lower the thinking "
                            f"budget or split the input."
                        ), usage_total) from e
                    new_max = min(max_tokens * 2, self.length_retry_max_tokens)
                    logging.warning(
                        f"Model '{model}' output truncated at max_tokens={max_tokens}; "
                        f"retrying with max_tokens={new_max}."
                    )
                    max_tokens = new_max
                    continue
                except _FILTER_ERRORS as e:
                    raise ValueError(
                        f"Model '{model}' response was blocked by a content filter."
                    ) from e
            break

        completion = self._checked(completion, model)
        message = completion.choices[0].message
        parsed = getattr(message, "parsed", None)
        if parsed is None:
            # Structured parse missed (refusal, or a model whose JSON needed
            # healing the SDK didn't apply) — validate the raw content ourselves.
            content = message.content
            if not content:
                # Billed but unusable: record this completion's usage (once) and
                # attach it so the caller's advisory accounting sees the spend (C7).
                u = _sum_usage(usage_total, self._record_usage(completion))
                raise _with_call_usage(ValueError(
                    f"Model '{model}' returned no parseable structured output "
                    f"(finish_reason={completion.choices[0].finish_reason})"
                ), u)
            try:
                parsed = schema.model_validate_json(content)
            except Exception:
                # A non-empty but malformed fallback body is just as billed and
                # unusable as an empty one. Record it in the authoritative hard
                # budget, and expose the per-call delta so advisory callers and
                # their token/cost summaries account for it too. Do not include
                # raw model output or Pydantic's content-bearing error in the
                # public exception message.
                u = _sum_usage(usage_total, self._record_usage(completion))
                raise _with_call_usage(ValueError(
                    f"Model '{model}' returned malformed structured output."
                ), u) from None

        return parsed, _sum_usage(usage_total, self._record_usage(completion))

    def _gather_with_tools(self, model, messages, tools, tool_runner, thinking_budget,
                           has_pdf, max_rounds, rounds_usage: List[TokenUsage]) -> None:
        """Phase 1: let the model call tools to gather evidence. Appends the
        assistant/tool exchange to `messages` in place, and appends EACH completed
        round's usage to `rounds_usage` as it happens so the caller retains it even
        if a later round raises. Bounded by `max_rounds`, MAX_TOTAL_TOOL_CALLS, and
        the run budget (checked before each round — a mid-loop trip breaks the loop
        so exactly one phase-2 answer still runs); tool errors are returned to the
        model as data rather than raised."""
        extra_body = self._build_extra_body(thinking_budget, has_pdf, healing=False)
        max_tokens = self._max_tokens_for(thinking_budget)
        total_calls = 0

        for _round in range(max_rounds):
            # Budget trip mid-loop: stop gathering and let the caller produce one
            # final phase-2 answer from the evidence already collected (R4). We break
            # rather than raise — a fresh-entry raise is handled at the top of
            # generate_structured instead.
            if self.budget.exceeded:
                break
            with self._api_semaphore:
                completion = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    extra_body=extra_body,
                )
            completion = self._checked(completion, model)
            rounds_usage.append(self._record_usage(completion))
            message = completion.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                break

            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                total_calls += 1
                if total_calls > MAX_TOTAL_TOOL_CALLS:
                    result = "Tool-call budget exhausted; answer from the evidence already gathered."
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    try:
                        result = tool_runner(tc.function.name, args)
                    except Exception as e:
                        result = f"Tool error: {e}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result)[:MAX_TOOL_RESULT_CHARS],
                })
            if total_calls > MAX_TOTAL_TOOL_CALLS:
                break

    def generate_text(
        self,
        model: str,
        contents: List[ContentPart],
        thinking_budget: Optional[int] = None,
    ) -> Tuple[str, TokenUsage]:
        """Generate free-form text (no schema). Returns (text, TokenUsage)."""
        self.budget.raise_if_exceeded()  # fresh entry already over budget → raise (R4)

        blocks, has_pdf = self._to_message_content(contents)
        messages = [{"role": "user", "content": blocks}]
        extra_body = self._build_extra_body(thinking_budget, has_pdf, healing=False)

        # finish_reason parity with the structured path: a "length" finish is a
        # silently truncated answer, so escalate the cap and retry (each
        # truncated attempt is still billed and recorded). Unlike JSON, partial
        # prose is still usable, so exhausting the escalation returns the
        # truncated text with a warning instead of raising.
        max_tokens = self._max_tokens_for(thinking_budget)
        usage_total = TokenUsage()
        for attempt in range(self.length_retry_attempts + 1):
            with self._api_semaphore:
                completion = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    extra_body=extra_body,
                )
            completion = self._checked(completion, model)
            usage_total = _sum_usage(usage_total, self._record_usage(completion))
            choice = completion.choices[0]
            text = (choice.message.content or "").strip()
            if getattr(choice, "finish_reason", None) != "length":
                return text, usage_total
            if (attempt >= self.length_retry_attempts
                    or max_tokens >= self.length_retry_max_tokens
                    or self.budget.exceeded):
                logging.warning(
                    f"Model '{model}' text output truncated at max_tokens={max_tokens}; "
                    f"returning partial text (escalation exhausted or over budget)."
                )
                return text, usage_total
            new_max = min(max_tokens * 2, self.length_retry_max_tokens)
            logging.warning(
                f"Model '{model}' text output truncated at max_tokens={max_tokens}; "
                f"retrying with max_tokens={new_max}."
            )
            max_tokens = new_max

    def _parse(self, **kwargs):
        """Call the SDK's structured-output parse helper across SDK versions."""
        parse = getattr(self.client.chat.completions, "parse", None)
        if parse is None:  # older SDK: parse lives under .beta
            parse = self.client.beta.chat.completions.parse
        return parse(**kwargs)

    def _checked(self, completion, model: str):
        """Validate the response envelope before any field access.

        OpenRouter can return HTTP 200 with an in-body ``error`` object (e.g. a
        provider failure mid-generation), and a degenerate response can carry an
        empty ``choices`` list. Either way the generation may still have been
        billed, so its usage is recorded (fail-closed: absent usage becomes
        cost_missing) BEFORE raising — an unusable envelope must not also
        escape spend accounting. Callers that reach the normal path record
        usage themselves; _checked records only on the raising paths.
        """
        err = getattr(completion, "error", None)
        if err:
            if isinstance(err, dict):
                code, message = err.get("code"), str(err.get("message", ""))
            else:
                code = getattr(err, "code", None)
                message = str(getattr(err, "message", "") or err)
            u = self._record_usage(completion)
            code_int = code if isinstance(code, int) and not isinstance(code, bool) else None
            raise _with_call_usage(LLMResponseEnvelopeError(
                f"Model '{model}' returned an in-body error"
                + (f" (code {code})" if code is not None else "")
                + (f": {message[:200]}" if message else "."),
                status_code=code_int,
            ), u)
        if not getattr(completion, "choices", None):
            u = self._record_usage(completion)
            raise _with_call_usage(LLMResponseEnvelopeError(
                f"Model '{model}' returned a response with no choices."
            ), u)
        return completion

    def _record_usage(self, completion) -> TokenUsage:
        """THE authoritative usage sink (R2). Parse this completion's usage exactly
        once, record it into the run budget, and return that SAME TokenUsage for the
        caller to accumulate. Every real generation on every path — each tool round,
        the length-retry truncated completion, the phase-2 parse, generate_text —
        must go through here, so the returned per-call usage always equals the budget
        delta and nothing is double-recorded."""
        usage = self._usage_from(completion)
        self.budget.record_and_check(usage)
        return usage

    @staticmethod
    def _usage_from(completion) -> TokenUsage:
        # Only ever called on a completion that actually returned, so a real
        # generation always happened here. If no usage block came back at all, the
        # cost is *unknown* — fail CLOSED so the aggregate spend cap (C3) can't be
        # tricked into recording hidden spend as a known $0.
        usage = getattr(completion, "usage", None)
        if usage is None:
            logging.warning(
                "OpenRouter completion carried no usage block; treating its cost as "
                "unknown for spend control."
            )
            return TokenUsage(cost_missing=True)
        data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
        completion_details = data.get("completion_tokens_details") or {}
        prompt_details = data.get("prompt_tokens_details") or {}
        input_tokens = data.get("prompt_tokens", 0) or 0
        output_tokens = data.get("completion_tokens", 0) or 0
        # Cost is KNOWN only when a finite, non-negative numeric figure is present
        # (an explicit 0.0 — e.g. a `:free` model — is known-free). Everything else
        # on a real generation is UNKNOWN → fail closed: a missing, non-numeric,
        # negative, or NaN cost, and a degenerate/empty usage block, all read
        # cost_missing=True (symmetric with the usage-None path above). bool is
        # excluded (it is an int subclass). The magnitude is clamped to 0 so a bogus
        # negative can never reduce the running aggregate the spend cap depends on.
        raw_cost = data.get("cost")
        cost_present = (
            isinstance(raw_cost, (int, float))
            and not isinstance(raw_cost, bool)
            and math.isfinite(raw_cost)
            and raw_cost >= 0.0
        )
        cost = float(raw_cost) if cost_present and raw_cost > 0 else 0.0
        cost_missing = not cost_present
        # Under BYOK the reported `cost` is only OpenRouter's fee (can be 0.0 while
        # real upstream spend occurs), so a cost cap cannot be trusted — mark it and
        # let RunBudget fall back to the authoritative token ceiling (R8). Verified
        # 2026-07-08: https://openrouter.ai/docs/use-cases/byok.
        byok = bool(data.get("is_byok"))
        if cost_missing:
            logging.warning(
                "OpenRouter usage reported %d prompt + %d completion tokens but no usable "
                "cost figure; treating this generation's cost as unknown for spend control.",
                input_tokens, output_tokens,
            )
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=completion_details.get("reasoning_tokens", 0) or 0,
            cached_tokens=prompt_details.get("cached_tokens", 0) or 0,
            cost=cost,
            cost_missing=cost_missing,
            byok=byok,
        )


# Backwards-compatible alias: callers type-hint and import `LLMProvider`.
LLMProvider = OpenRouterProvider


def create_provider(api_key: str, **kwargs) -> OpenRouterProvider:
    """Create the OpenRouter-backed LLM provider.

    Extra keyword arguments are forwarded to ``OpenRouterProvider``. Notably:

    - ``timeout`` (float, seconds): per-attempt request timeout. Defaults to
      ``DEFAULT_REQUEST_TIMEOUT``. Worst-case slot-hold is ``timeout * (max_retries + 1)``.
    - ``max_concurrency`` (int): in-flight-call cap for this provider. Omit to
      share the process-wide default sized from ``LLM_MAX_CONCURRENCY`` (read at
      construction); pass a value to give this provider its own semaphore.
    - ``budget`` (RunBudget): per-run token/cost ceiling (C3). Omit for a no-op
      budget (unchanged behaviour). A configured budget stops further LLM calls
      once the ceiling is crossed and raises :class:`BudgetExceededError` on a
      fresh call made when already over. The token ceiling is authoritative; a
      cost-only budget is rejected at ``RunBudget`` construction.

    ``timeout``, ``max_concurrency`` and ``budget`` are operator-trusted
    configuration — do not source them from an untrusted PR checkout.
    """
    if not api_key:
        raise ValueError("An OpenRouter API key is required.")
    return OpenRouterProvider(api_key=api_key, **kwargs)
