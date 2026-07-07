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


def _sum_usage(a: "TokenUsage", b: "TokenUsage") -> "TokenUsage":
    """Add two TokenUsage records (to accumulate across tool-loop + final call)."""
    return TokenUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        thinking_tokens=a.thinking_tokens + b.thinking_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        cost=a.cost + b.cost,
        cost_missing=a.cost_missing or b.cost_missing,
    )


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
    ):
        from openai import OpenAI

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
        self.client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            max_retries=self.max_retries,  # clamped; SDK retries 429/5xx/timeout, honoring Retry-After
            timeout=self.timeout,     # per-attempt; bounds a stuck upstream (C2)
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
        blocks, has_pdf = self._to_message_content(contents)
        messages = [{"role": "user", "content": blocks}]
        usage_total = TokenUsage()

        if tools and tool_runner:
            try:
                usage_total = _sum_usage(usage_total, self._gather_with_tools(
                    model, messages, tools, tool_runner, thinking_budget, has_pdf, max_tool_rounds,
                ))
                messages.append({
                    "role": "user",
                    "content": "Now provide your final answer as JSON matching the required schema, using the evidence gathered above.",
                })
            except Exception as e:
                logging.warning(f"Tool phase failed for '{model}'; continuing without tools: {e}")

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
                        usage_total = _sum_usage(usage_total, self._usage_from(truncated))
                    # Truncated before complete structured output. Retry with a
                    # larger cap so the review's findings aren't lost wholesale.
                    if attempt >= self.length_retry_attempts or max_tokens >= self.length_retry_max_tokens:
                        raise ValueError(
                            f"Model '{model}' hit the output token cap ({max_tokens}) before "
                            f"producing complete structured output, even after "
                            f"{self.length_retry_attempts} escalation(s); lower the thinking "
                            f"budget or split the input."
                        ) from e
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

        message = completion.choices[0].message
        parsed = getattr(message, "parsed", None)
        if parsed is None:
            # Structured parse missed (refusal, or a model whose JSON needed
            # healing the SDK didn't apply) — validate the raw content ourselves.
            content = message.content
            if not content:
                raise ValueError(
                    f"Model '{model}' returned no parseable structured output "
                    f"(finish_reason={completion.choices[0].finish_reason})"
                )
            parsed = schema.model_validate_json(content)

        return parsed, _sum_usage(usage_total, self._usage_from(completion))

    def _gather_with_tools(self, model, messages, tools, tool_runner, thinking_budget, has_pdf, max_rounds) -> TokenUsage:
        """Phase 1: let the model call tools to gather evidence. Appends the
        assistant/tool exchange to `messages` in place and returns accumulated
        TokenUsage. Bounded by `max_rounds` and MAX_TOTAL_TOOL_CALLS; tool errors
        are returned to the model as data rather than raised."""
        extra_body = self._build_extra_body(thinking_budget, has_pdf, healing=False)
        max_tokens = self._max_tokens_for(thinking_budget)
        usage_total = TokenUsage()
        total_calls = 0

        for _round in range(max_rounds):
            with self._api_semaphore:
                completion = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    extra_body=extra_body,
                )
            usage_total = _sum_usage(usage_total, self._usage_from(completion))
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

        return usage_total

    def generate_text(
        self,
        model: str,
        contents: List[ContentPart],
        thinking_budget: Optional[int] = None,
    ) -> Tuple[str, TokenUsage]:
        """Generate free-form text (no schema). Returns (text, TokenUsage)."""
        blocks, has_pdf = self._to_message_content(contents)
        messages = [{"role": "user", "content": blocks}]
        extra_body = self._build_extra_body(thinking_budget, has_pdf, healing=False)

        with self._api_semaphore:
            completion = self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=self._max_tokens_for(thinking_budget),
                extra_body=extra_body,
            )

        text = completion.choices[0].message.content or ""
        return text.strip(), self._usage_from(completion)

    def _parse(self, **kwargs):
        """Call the SDK's structured-output parse helper across SDK versions."""
        parse = getattr(self.client.chat.completions, "parse", None)
        if parse is None:  # older SDK: parse lives under .beta
            parse = self.client.beta.chat.completions.parse
        return parse(**kwargs)

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

    ``timeout`` and ``max_concurrency`` are operator-trusted configuration — do
    not source them from an untrusted PR checkout.
    """
    if not api_key:
        raise ValueError("An OpenRouter API key is required.")
    return OpenRouterProvider(api_key=api_key, **kwargs)
