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


# Cap on the number of concurrent in-flight API calls across all threads.
# OpenRouter rate-limits per account; the OpenAI SDK handles 429/Retry-After
# backoff, this just keeps us from stampeding it. Defaults to 5 to match
# review.py's default --max-workers, so per-file review threads aren't silently
# throttled below the pool size. Set LLM_MAX_CONCURRENCY to tune both together.
_api_semaphore = threading.Semaphore(_env_int("LLM_MAX_CONCURRENCY", 5))

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


def _sum_usage(a: "TokenUsage", b: "TokenUsage") -> "TokenUsage":
    """Add two TokenUsage records (to accumulate across tool-loop + final call)."""
    return TokenUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        thinking_tokens=a.thinking_tokens + b.thinking_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        cost=a.cost + b.cost,
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
        max_retries: int = 4,
        max_tokens: int = 16384,
        reasoning_default: Optional[dict] = None,
        require_parameters: bool = False,
        http_referer: Optional[str] = None,
        x_title: Optional[str] = None,
        length_retry_attempts: int = 2,
        length_retry_max_tokens: int = 65536,
        enable_web_search: bool = False,
    ):
        from openai import OpenAI

        self.max_tokens = max_tokens
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
            max_retries=max_retries,  # SDK retries 429/5xx, honoring Retry-After
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
            with _api_semaphore:
                try:
                    completion = self._parse(
                        model=model,
                        messages=messages,
                        response_format=schema,
                        max_tokens=max_tokens,
                        extra_body=extra_body,
                    )
                except _LENGTH_ERRORS as e:
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
            with _api_semaphore:
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

        with _api_semaphore:
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
        usage = getattr(completion, "usage", None)
        if usage is None:
            return TokenUsage()
        data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
        completion_details = data.get("completion_tokens_details") or {}
        prompt_details = data.get("prompt_tokens_details") or {}
        return TokenUsage(
            input_tokens=data.get("prompt_tokens", 0) or 0,
            output_tokens=data.get("completion_tokens", 0) or 0,
            thinking_tokens=completion_details.get("reasoning_tokens", 0) or 0,
            cached_tokens=prompt_details.get("cached_tokens", 0) or 0,
            cost=data.get("cost", 0.0) or 0.0,
        )


# Backwards-compatible alias: callers type-hint and import `LLMProvider`.
LLMProvider = OpenRouterProvider


def create_provider(api_key: str, **kwargs) -> OpenRouterProvider:
    """Create the OpenRouter-backed LLM provider."""
    if not api_key:
        raise ValueError("An OpenRouter API key is required.")
    return OpenRouterProvider(api_key=api_key, **kwargs)
