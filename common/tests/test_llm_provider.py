"""Tests for the OpenRouter-backed llm_provider.

The provider imports the `openai` SDK lazily inside ``__init__``; these tests
avoid that import by building instances via ``__new__`` for the pure-helper
checks, and by injecting a fake ``openai`` module for the one test that
exercises real client construction.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

from pydantic import BaseModel

from leanrepo_common import llm_provider
from leanrepo_common.llm_provider import (
    ContentPart,
    OpenRouterProvider,
    TokenUsage,
    _data_url,
    _sum_usage,
    create_provider,
)


class _Schema(BaseModel):
    x: int


def _bare_provider(max_tokens=16384, reasoning_default=None, require_parameters=False,
                   length_retry_attempts=2, length_retry_max_tokens=65536,
                   max_concurrency=4):
    """An OpenRouterProvider instance without running __init__ (no openai import).

    __init__ resolves the concurrency semaphore, so instances built via __new__
    must be seeded with one or the generation paths would AttributeError. A
    dedicated (non-shared) semaphore keeps these unit tests independent of the
    process-wide default and of each other.
    """
    import threading

    p = OpenRouterProvider.__new__(OpenRouterProvider)
    p.max_tokens = max_tokens
    p.reasoning_default = reasoning_default
    p.require_parameters = require_parameters
    p.length_retry_attempts = length_retry_attempts
    p.length_retry_max_tokens = length_retry_max_tokens
    p._max_concurrency = max_concurrency
    p._api_semaphore = threading.Semaphore(max_concurrency)
    return p


class _FakeUsage:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


class _FakeMessage:
    def __init__(self, parsed=None, content=None):
        self.parsed = parsed
        self.content = content


class _FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeCompletion:
    def __init__(self, message, usage=None, finish_reason="stop"):
        self.choices = [_FakeChoice(message, finish_reason)]
        self.usage = usage


class DataUrlTests(unittest.TestCase):
    def test_bytes_become_base64_data_url(self):
        url = _data_url(b"hello", "application/pdf")
        self.assertTrue(url.startswith("data:application/pdf;base64,"))

    def test_str_passes_through(self):
        self.assertEqual(_data_url("https://x/y.png", "image/png"), "https://x/y.png")


class MessageContentTests(unittest.TestCase):
    def setUp(self):
        self.p = _bare_provider()

    def test_text_block(self):
        blocks, has_pdf = self.p._to_message_content([ContentPart("text", "hi")])
        self.assertEqual(blocks, [{"type": "text", "text": "hi"}])
        self.assertFalse(has_pdf)

    def test_cached_text_gets_cache_control(self):
        blocks, _ = self.p._to_message_content([ContentPart("text", "ctx", cache=True)])
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})

    def test_image_bytes(self):
        blocks, _ = self.p._to_message_content(
            [ContentPart("image", b"\x89PNG", mime_type="image/png")]
        )
        self.assertEqual(blocks[0]["type"], "image_url")
        self.assertTrue(blocks[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_image_url_passthrough(self):
        blocks, _ = self.p._to_message_content([ContentPart("image", "https://x/i.png")])
        self.assertEqual(blocks[0]["image_url"]["url"], "https://x/i.png")

    def test_pdf_sets_has_pdf_and_file_block(self):
        blocks, has_pdf = self.p._to_message_content([ContentPart("pdf", b"%PDF-1.4")])
        self.assertTrue(has_pdf)
        self.assertEqual(blocks[0]["type"], "file")
        self.assertEqual(blocks[0]["file"]["filename"], "document.pdf")
        self.assertTrue(blocks[0]["file"]["file_data"].startswith("data:application/pdf;base64,"))
        self.assertNotIn("cache_control", blocks[0])

    def test_cache_control_honored_on_pdf_and_image(self):
        # Regression: a trailing PDF/image reference must still carry the cache
        # breakpoint, not just text parts.
        pdf_blocks, _ = self.p._to_message_content([ContentPart("pdf", b"%PDF", cache=True)])
        self.assertEqual(pdf_blocks[0]["cache_control"], {"type": "ephemeral"})
        img_blocks, _ = self.p._to_message_content([ContentPart("image", b"\x89PNG", cache=True)])
        self.assertEqual(img_blocks[0]["cache_control"], {"type": "ephemeral"})

    def test_unknown_type_skipped(self):
        with self.assertLogs(level="WARNING"):
            blocks, _ = self.p._to_message_content([ContentPart("video", b"x")])
        self.assertEqual(blocks, [])


class ExtraBodyTests(unittest.TestCase):
    def test_require_parameters_off_by_default(self):
        body = _bare_provider()._build_extra_body(None, has_pdf=False)
        self.assertNotIn("provider", body)

    def test_require_parameters_set_when_enabled(self):
        body = _bare_provider(require_parameters=True)._build_extra_body(None, has_pdf=False)
        self.assertTrue(body["provider"]["require_parameters"])

    def test_response_healing_plugin_present(self):
        body = _bare_provider()._build_extra_body(None, has_pdf=False)
        self.assertIn({"id": "response-healing"}, body["plugins"])
        self.assertNotIn("file-parser", [p["id"] for p in body["plugins"]])

    def test_file_parser_plugin_added_for_pdf(self):
        body = _bare_provider()._build_extra_body(None, has_pdf=True)
        ids = [p["id"] for p in body["plugins"]]
        self.assertIn("file-parser", ids)

    def test_web_plugin_absent_by_default(self):
        body = _bare_provider()._build_extra_body(None, has_pdf=False)
        self.assertNotIn("web", [p["id"] for p in body["plugins"]])

    def test_web_plugin_added_when_enabled(self):
        p = _bare_provider()
        p.enable_web_search = True
        body = p._build_extra_body(None, has_pdf=False)
        self.assertIn("web", [pl["id"] for pl in body["plugins"]])

    def test_reasoning_from_budget(self):
        body = _bare_provider()._build_extra_body(4096, has_pdf=False)
        self.assertEqual(body["reasoning"], {"max_tokens": 4096})

    def test_reasoning_default_used_when_no_budget(self):
        body = _bare_provider(reasoning_default={"effort": "high"})._build_extra_body(None, False)
        self.assertEqual(body["reasoning"], {"effort": "high"})

    def test_no_reasoning_when_none(self):
        body = _bare_provider()._build_extra_body(None, has_pdf=False)
        self.assertNotIn("reasoning", body)

    def test_zero_budget_disables_reasoning(self):
        body = _bare_provider()._build_extra_body(0, has_pdf=False)
        self.assertNotIn("reasoning", body)


class MaxTokensTests(unittest.TestCase):
    def test_default_without_budget(self):
        self.assertEqual(_bare_provider(max_tokens=16384)._max_tokens_for(None), 16384)

    def test_reserves_full_base_above_budget(self):
        # answer headroom = full base, on top of the thinking budget
        self.assertEqual(_bare_provider(max_tokens=16384)._max_tokens_for(20000), 36384)
        self.assertEqual(_bare_provider(max_tokens=16384)._max_tokens_for(10240), 26624)

    def test_zero_budget_is_base(self):
        self.assertEqual(_bare_provider(max_tokens=65536)._max_tokens_for(0), 65536)


class GenerateStructuredTests(unittest.TestCase):
    def _provider_with_parse(self, completion):
        p = _bare_provider()
        parse = mock.Mock(return_value=completion)
        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(parse=parse))
        )
        return p, parse

    def test_returns_parsed_and_usage(self):
        usage = _FakeUsage({
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 7},
            "prompt_tokens_details": {"cached_tokens": 50},
            "cost": 0.012,
        })
        completion = _FakeCompletion(_FakeMessage(parsed=_Schema(x=5)), usage=usage)
        p, parse = self._provider_with_parse(completion)

        parsed, tokens = p.generate_structured(
            "anthropic/claude-opus-4.8", [ContentPart("text", "hi")], _Schema, thinking_budget=4096
        )

        self.assertEqual(parsed.x, 5)
        self.assertEqual(tokens.input_tokens, 100)
        self.assertEqual(tokens.output_tokens, 20)
        self.assertEqual(tokens.thinking_tokens, 7)
        self.assertEqual(tokens.cached_tokens, 50)
        self.assertAlmostEqual(tokens.cost, 0.012)

        # Wiring: schema passed as response_format, reasoning + provider in extra_body.
        _, kwargs = parse.call_args
        self.assertIs(kwargs["response_format"], _Schema)
        self.assertEqual(kwargs["model"], "anthropic/claude-opus-4.8")
        self.assertEqual(kwargs["extra_body"]["reasoning"], {"max_tokens": 4096})
        self.assertNotIn("provider", kwargs["extra_body"])  # require_parameters off by default
        self.assertEqual(kwargs["messages"][0]["role"], "user")

    def test_falls_back_to_validate_json(self):
        completion = _FakeCompletion(_FakeMessage(parsed=None, content='{"x": 9}'))
        p, _ = self._provider_with_parse(completion)
        parsed, _ = p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        self.assertEqual(parsed.x, 9)

    def test_raises_when_no_output(self):
        completion = _FakeCompletion(_FakeMessage(parsed=None, content=None), finish_reason="length")
        p, _ = self._provider_with_parse(completion)
        with self.assertRaises(ValueError):
            p.generate_structured("m", [ContentPart("text", "hi")], _Schema)

    def test_usage_absent_yields_zero_cost_but_flags_missing(self):
        # A completion with no usage block: zero token counts, but the cost is
        # UNKNOWN (a real generation happened), so it must fail closed for C3.
        completion = _FakeCompletion(_FakeMessage(parsed=_Schema(x=1)), usage=None)
        p, _ = self._provider_with_parse(completion)
        with self.assertLogs(level="WARNING"):
            _, tokens = p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        self.assertEqual(tokens, TokenUsage(cost_missing=True))

    @unittest.skipUnless(
        importlib.util.find_spec("openai") is not None,
        "openai SDK not installed; LengthFinishReasonError handling can't be exercised",
    )
    def test_length_finish_reason_becomes_clear_error(self):
        from openai import LengthFinishReasonError
        p = _bare_provider()
        def raise_length(**kwargs):
            raise LengthFinishReasonError(completion=_FakeCompletion(_FakeMessage(content="{")))
        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(parse=raise_length))
        )
        with self.assertRaises(ValueError) as cm:
            p.generate_structured("m", [ContentPart("text", "hi")], _Schema, thinking_budget=4096)
        self.assertIn("output token cap", str(cm.exception))

    @unittest.skipUnless(
        importlib.util.find_spec("openai") is not None,
        "openai SDK not installed; LengthFinishReasonError handling can't be exercised",
    )
    def test_length_error_retries_with_larger_cap_then_succeeds(self):
        from openai import LengthFinishReasonError
        p = _bare_provider(max_tokens=16384, length_retry_attempts=2)
        seen_max_tokens = []

        def parse(**kwargs):
            seen_max_tokens.append(kwargs["max_tokens"])
            if len(seen_max_tokens) == 1:
                raise LengthFinishReasonError(completion=_FakeCompletion(_FakeMessage(content="{")))
            return _FakeCompletion(_FakeMessage(parsed=_Schema(x=3)))

        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(parse=parse))
        )
        parsed, _ = p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        self.assertEqual(parsed.x, 3)
        # Second attempt used a strictly larger output cap than the first.
        self.assertEqual(len(seen_max_tokens), 2)
        self.assertGreater(seen_max_tokens[1], seen_max_tokens[0])

    @unittest.skipUnless(
        importlib.util.find_spec("openai") is not None,
        "openai SDK not installed; LengthFinishReasonError handling can't be exercised",
    )
    def test_length_retry_counts_truncated_attempt_spend(self):
        # A truncated attempt is a real billed generation; its usage/cost must be
        # accumulated, not silently dropped in favor of only the final call.
        from openai import LengthFinishReasonError
        p = _bare_provider(max_tokens=16384, length_retry_attempts=2)
        trunc_usage = _FakeUsage({"prompt_tokens": 100, "completion_tokens": 16384, "cost": 0.02})
        final_usage = _FakeUsage({"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001})
        calls = []

        def parse(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise LengthFinishReasonError(
                    completion=_FakeCompletion(_FakeMessage(content="{"), usage=trunc_usage)
                )
            return _FakeCompletion(_FakeMessage(parsed=_Schema(x=3)), usage=final_usage)

        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(parse=parse))
        )
        parsed, usage = p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        self.assertEqual(parsed.x, 3)
        self.assertEqual(usage.input_tokens, 200)          # both attempts
        self.assertEqual(usage.output_tokens, 16384 + 50)  # truncated attempt not dropped
        self.assertAlmostEqual(usage.cost, 0.021)
        self.assertFalse(usage.cost_missing)


class ToolLoopTests(unittest.TestCase):
    def _client(self, create, parse):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create, parse=parse))
        )

    def _tool_call(self, call_id, name, arguments):
        return types.SimpleNamespace(id=call_id, function=types.SimpleNamespace(name=name, arguments=arguments))

    def test_gathers_via_tools_then_returns_structured(self):
        p = _bare_provider()
        tc = self._tool_call("c1", "lean_check", '{"expr": "List.map"}')
        round1 = _FakeCompletion(types.SimpleNamespace(content=None, tool_calls=[tc]))
        round2 = _FakeCompletion(types.SimpleNamespace(content="done", tool_calls=None))
        create = mock.Mock(side_effect=[round1, round2])
        parse = mock.Mock(return_value=_FakeCompletion(_FakeMessage(parsed=_Schema(x=7))))
        p.client = self._client(create, parse)

        ran = []

        def runner(name, args):
            ran.append((name, args))
            return "List.map : (α → β) → List α → List β"

        parsed, _usage = p.generate_structured(
            "m", [ContentPart("text", "hi")], _Schema,
            tools=[{"type": "function", "function": {"name": "lean_check", "parameters": {}}}],
            tool_runner=runner,
        )
        self.assertEqual(parsed.x, 7)
        self.assertEqual(ran, [("lean_check", {"expr": "List.map"})])
        self.assertEqual(create.call_count, 2)     # tool round + the round that stopped
        self.assertEqual(parse.call_count, 1)      # final structured call

    def test_tool_loop_usage_accumulates_into_final_total(self):
        # Every completed tool round's usage (and its cost_missing) must flow into
        # the returned total alongside the final structured call — not be dropped.
        p = _bare_provider()
        tc = self._tool_call("c1", "lean_check", '{"expr": "x"}')
        r1 = _FakeCompletion(types.SimpleNamespace(content=None, tool_calls=[tc]),
                             usage=_FakeUsage({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}))
        r2 = _FakeCompletion(types.SimpleNamespace(content="done", tool_calls=None),
                             usage=_FakeUsage({"prompt_tokens": 8, "completion_tokens": 3}))  # no cost
        create = mock.Mock(side_effect=[r1, r2])
        parse = mock.Mock(return_value=_FakeCompletion(
            _FakeMessage(parsed=_Schema(x=7)),
            usage=_FakeUsage({"prompt_tokens": 20, "completion_tokens": 10, "cost": 0.005})))
        p.client = self._client(create, parse)

        parsed, usage = p.generate_structured(
            "m", [ContentPart("text", "hi")], _Schema,
            tools=[{"type": "function", "function": {"name": "lean_check", "parameters": {}}}],
            tool_runner=lambda n, a: "ok",
        )
        self.assertEqual(parsed.x, 7)
        self.assertEqual(usage.input_tokens, 10 + 8 + 20)
        self.assertEqual(usage.output_tokens, 5 + 3 + 10)
        self.assertAlmostEqual(usage.cost, 0.001 + 0.005)  # r2 carried no cost
        self.assertTrue(usage.cost_missing)                # r2's unknown cost OR-propagates

    def test_tool_phase_failure_falls_back_to_plain(self):
        p = _bare_provider()
        create = mock.Mock(side_effect=RuntimeError("model has no tools"))
        parse = mock.Mock(return_value=_FakeCompletion(_FakeMessage(parsed=_Schema(x=1))))
        p.client = self._client(create, parse)
        parsed, _ = p.generate_structured(
            "m", [ContentPart("text", "hi")], _Schema,
            tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
            tool_runner=lambda n, a: "",
        )
        self.assertEqual(parsed.x, 1)              # fell back to structured generation
        self.assertTrue(parse.called)

    def test_tool_error_returned_as_data_not_raised(self):
        p = _bare_provider()
        tc = self._tool_call("c1", "lean_check", "{}")
        round1 = _FakeCompletion(types.SimpleNamespace(content=None, tool_calls=[tc]))
        round2 = _FakeCompletion(types.SimpleNamespace(content="ok", tool_calls=None))
        create = mock.Mock(side_effect=[round1, round2])
        parse = mock.Mock(return_value=_FakeCompletion(_FakeMessage(parsed=_Schema(x=2))))
        p.client = self._client(create, parse)

        def boom(name, args):
            raise RuntimeError("lake missing")

        parsed, _ = p.generate_structured(
            "m", [ContentPart("text", "hi")], _Schema,
            tools=[{"type": "function", "function": {"name": "lean_check", "parameters": {}}}],
            tool_runner=boom,
        )
        self.assertEqual(parsed.x, 2)              # tool error didn't abort; structured answer produced

    def test_tool_call_budget_caps_executions(self):
        with mock.patch.object(llm_provider, "MAX_TOTAL_TOOL_CALLS", 1):
            p = _bare_provider()
            # Each round offers two tool calls; the budget should stop us fast.
            tcs = [self._tool_call("a", "lean_check", "{}"), self._tool_call("b", "lean_check", "{}")]
            looping = _FakeCompletion(types.SimpleNamespace(content=None, tool_calls=tcs))
            create = mock.Mock(return_value=looping)
            parse = mock.Mock(return_value=_FakeCompletion(_FakeMessage(parsed=_Schema(x=3))))
            p.client = self._client(create, parse)

            calls = []
            parsed, _ = p.generate_structured(
                "m", [ContentPart("text", "hi")], _Schema,
                tools=[{"type": "function", "function": {"name": "lean_check", "parameters": {}}}],
                tool_runner=lambda n, a: calls.append(1) or "r",
            )
            self.assertEqual(parsed.x, 3)
            self.assertEqual(len(calls), 1)        # only the 1st tool call ran; 2nd hit the budget
            self.assertEqual(create.call_count, 1) # broke out after the budget was exceeded


class GenerateTextTests(unittest.TestCase):
    def test_returns_text_and_usage_no_healing(self):
        usage = _FakeUsage({"prompt_tokens": 30, "completion_tokens": 12,
                            "completion_tokens_details": {"reasoning_tokens": 3}})
        message = _FakeMessage(content="## analysis\nfree text")
        completion = _FakeCompletion(message, usage=usage)
        p = _bare_provider()
        create = mock.Mock(return_value=completion)
        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )

        text, tokens = p.generate_text("openai/gpt-5", [ContentPart("text", "hi")], thinking_budget=2048)

        self.assertEqual(text, "## analysis\nfree text")
        self.assertEqual(tokens.input_tokens, 30)
        self.assertEqual(tokens.thinking_tokens, 3)
        _, kwargs = create.call_args
        # Free-form text must NOT request the structured-JSON healing plugin.
        plugins = kwargs["extra_body"].get("plugins", [])
        self.assertNotIn("response-healing", [pl["id"] for pl in plugins])
        self.assertNotIn("response_format", kwargs)

    def test_none_content_yields_empty_string(self):
        completion = _FakeCompletion(_FakeMessage(content=None))
        p = _bare_provider()
        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=mock.Mock(return_value=completion))
            )
        )
        text, _ = p.generate_text("m", [ContentPart("text", "hi")])
        self.assertEqual(text, "")


class FactoryTests(unittest.TestCase):
    def test_create_provider_requires_key(self):
        with self.assertRaises(ValueError):
            create_provider("")

    def test_create_provider_builds_client_with_openrouter_base_url(self):
        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = FakeOpenAI
        with mock.patch.dict(sys.modules, {"openai": fake_openai}):
            provider = create_provider("sk-or-test", max_retries=4)

        self.assertEqual(provider.name, "openrouter")
        self.assertEqual(captured["base_url"], llm_provider.OPENROUTER_BASE_URL)
        self.assertEqual(captured["api_key"], "sk-or-test")
        self.assertEqual(captured["max_retries"], 4)
        self.assertIn("HTTP-Referer", captured["default_headers"])
        self.assertIn("X-Title", captured["default_headers"])


# --- shared test helper: construct a provider with a fake `openai` SDK ---------
# Runs the REAL __init__ (so _resolve_concurrency / timeout wiring is exercised)
# without importing the real SDK. Returns (provider, captured OpenAI(**kwargs)).
def _construct_with_fake_openai(**kwargs):
    captured = {}

    def _fake_create(**_k):
        return _FakeCompletion(_FakeMessage(content="ok"), usage=None)

    class FakeOpenAI:
        def __init__(self, **k):
            captured.update(k)
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=_fake_create)
            )

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI
    with mock.patch.dict(sys.modules, {"openai": fake_openai}):
        provider = create_provider("sk-or-test", **kwargs)
    return provider, captured


def _reset_shared_semaphore():
    llm_provider._default_semaphore = None
    llm_provider._default_semaphore_n = None


# ============================ C1 — cost accounting ============================
class CostAccountingTests(unittest.TestCase):
    """C1: real cost must round-trip from the response, independent of the
    (now deprecated, no-op) usage:{include} request flag."""

    def _provider_with_parse(self, completion):
        p = _bare_provider()
        parse = mock.Mock(return_value=completion)
        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(parse=parse))
        )
        return p, parse

    def test_cost_round_trips_through_real_sdk_usage_model(self):
        # Round-trip through the ACTUAL SDK usage model (not a hand-built dict):
        # the extra `cost`/`cost_details` fields OpenRouter adds must survive
        # model_dump so _usage_from can read them. Provenance of this payload:
        # OpenRouter usage-accounting docs (fetched 2026-07-06).
        try:
            from openai.types import CompletionUsage
        except Exception:  # pragma: no cover - openai always present in prod
            self.skipTest("openai SDK not importable")
        usage = CompletionUsage.model_validate({
            "prompt_tokens": 30,
            "completion_tokens": 12,
            "total_tokens": 42,
            "cost": 0.012,
            "completion_tokens_details": {"reasoning_tokens": 4},
            "prompt_tokens_details": {"cached_tokens": 7},
        })
        completion = _FakeCompletion(_FakeMessage(parsed=_Schema(x=1)), usage=usage)
        p, _ = self._provider_with_parse(completion)

        _, tokens = p.generate_structured("m", [ContentPart("text", "hi")], _Schema)

        # Only assert fields TokenUsage actually carries.
        self.assertAlmostEqual(tokens.cost, 0.012)
        self.assertEqual(tokens.input_tokens, 30)
        self.assertEqual(tokens.output_tokens, 12)
        self.assertEqual(tokens.thinking_tokens, 4)
        self.assertEqual(tokens.cached_tokens, 7)
        self.assertFalse(tokens.cost_missing)

    def test_usage_include_flag_present_on_all_paths(self):
        # The flag is pinned on every call path (structured + text + tool loop
        # all go through _build_extra_body), independent of healing/pdf.
        p = _bare_provider()
        for healing in (True, False):
            for has_pdf in (True, False):
                body = p._build_extra_body(None, has_pdf=has_pdf, healing=healing)
                self.assertEqual(body["usage"], {"include": True})

    def test_structured_request_carries_usage_flag(self):
        completion = _FakeCompletion(_FakeMessage(parsed=_Schema(x=1)), usage=None)
        p, parse = self._provider_with_parse(completion)
        p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        _, kwargs = parse.call_args
        self.assertEqual(kwargs["extra_body"]["usage"], {"include": True})

    def test_cost_missing_flagged_when_tokens_but_no_cost(self):
        usage = _FakeUsage({"prompt_tokens": 10, "completion_tokens": 5})  # no cost key
        with self.assertLogs(level="WARNING"):
            tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=usage))
        self.assertEqual(tokens.cost, 0.0)
        self.assertTrue(tokens.cost_missing)

    def test_cost_present_not_flagged(self):
        usage = _FakeUsage({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001})
        tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=usage))
        self.assertFalse(tokens.cost_missing)

    def test_free_model_explicit_zero_cost_is_known_not_flagged(self):
        # A `:free` slug legitimately returns tokens>0 with an explicit cost 0.0.
        # That is KNOWN-free, not unknown — must not be flagged and must not warn.
        usage = _FakeUsage({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0})
        tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=usage))
        self.assertEqual(tokens.cost, 0.0)
        self.assertFalse(tokens.cost_missing)

    def test_absent_usage_flagged_fail_closed(self):
        # A completion returned but carried NO usage block: a real generation with
        # unknown cost. Must fail CLOSED (cost_missing=True) and warn — recording it
        # as a known $0 would let hidden spend slip past the C3 cap.
        with self.assertLogs(level="WARNING"):
            tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=None))
        self.assertEqual(tokens.cost, 0.0)
        self.assertTrue(tokens.cost_missing)

    def test_negative_cost_clamped_and_flagged_unknown(self):
        # A negative cost is malformed: clamp the magnitude to 0 (never reduce the
        # aggregate) AND fail closed — it is not a trustworthy $0.
        usage = _FakeUsage({"prompt_tokens": 10, "completion_tokens": 5, "cost": -0.5})
        with self.assertLogs(level="WARNING"):
            tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=usage))
        self.assertEqual(tokens.cost, 0.0)
        self.assertTrue(tokens.cost_missing)

    def test_nan_cost_flagged_unknown(self):
        usage = _FakeUsage({"prompt_tokens": 10, "completion_tokens": 5, "cost": float("nan")})
        with self.assertLogs(level="WARNING"):
            tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=usage))
        self.assertEqual(tokens.cost, 0.0)
        self.assertTrue(tokens.cost_missing)

    def test_bool_cost_flagged_unknown(self):
        # bool is an int subclass; True must not be read as a cost figure.
        usage = _FakeUsage({"prompt_tokens": 10, "completion_tokens": 5, "cost": True})
        with self.assertLogs(level="WARNING"):
            tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=usage))
        self.assertEqual(tokens.cost, 0.0)
        self.assertTrue(tokens.cost_missing)

    def test_degenerate_usage_block_flagged_unknown(self):
        # A usage block present but carrying no cost figure (here also no tokens)
        # must fail closed, symmetric with the usage-None path — not read as $0.
        with self.assertLogs(level="WARNING"):
            tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=_FakeUsage({})))
        self.assertEqual(tokens.cost, 0.0)
        self.assertTrue(tokens.cost_missing)

    def test_zero_token_zero_cost_not_flagged(self):
        usage = _FakeUsage({"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0})
        tokens = OpenRouterProvider._usage_from(_FakeCompletion(_FakeMessage(), usage=usage))
        self.assertFalse(tokens.cost_missing)

    def test_sum_usage_ors_cost_missing(self):
        a = TokenUsage(input_tokens=1, cost=0.0, cost_missing=True)
        b = TokenUsage(input_tokens=1, cost=0.01, cost_missing=False)
        self.assertTrue(_sum_usage(a, b).cost_missing)
        self.assertFalse(_sum_usage(b, b).cost_missing)


@unittest.skipUnless(
    os.environ.get("OPENROUTER_API_KEY"),
    "live cost check: set OPENROUTER_API_KEY to run (the usage flag is a no-op, "
    "so only a real API round-trip can prove production cost is non-zero)",
)
class LiveCostTests(unittest.TestCase):
    def test_real_completion_reports_nonzero_cost(self):
        # The ONLY test that proves cost genuinely flows in production. Keeps the
        # api_key out of the assertion output — only the numeric cost is surfaced.
        model = os.environ.get("OPENROUTER_TEST_MODEL", "openai/gpt-4o-mini")
        provider = create_provider(os.environ["OPENROUTER_API_KEY"])
        text, usage = provider.generate_text(
            model, [ContentPart("text", "Reply with the single word: ok")]
        )
        self.assertTrue(text)
        self.assertGreater(usage.cost, 0.0, "expected non-zero cost from a real call")
        self.assertFalse(usage.cost_missing)
        print(f"\n[live] observed cost for {model}: ${usage.cost:.6f}")


# ================= C2 — timeout + configurable concurrency ===================
class TimeoutTests(unittest.TestCase):
    def test_default_timeout_wired_to_client(self):
        _, captured = _construct_with_fake_openai()
        self.assertEqual(captured["timeout"], llm_provider.DEFAULT_REQUEST_TIMEOUT)

    def test_timeout_override(self):
        provider, captured = _construct_with_fake_openai(timeout=42.0)
        self.assertEqual(captured["timeout"], 42.0)
        self.assertEqual(provider.timeout, 42.0)

    def test_default_max_retries_is_two(self):
        # The retry budget is load-bearing: worst-case slot-hold is
        # timeout * (max_retries + 1). Default must be the reduced budget.
        _, captured = _construct_with_fake_openai()
        self.assertEqual(captured["max_retries"], 2)

    def test_slot_hold_bound_meets_need(self):
        # Encodes C2's actual Need (~10 min), not merely that a timeout was set.
        provider, _ = _construct_with_fake_openai()
        self.assertLessEqual(provider.timeout * (provider.max_retries + 1), 600.0)

    def test_max_retries_clamped(self):
        # max_retries is the co-equal factor in the slot-hold bound; an oversized or
        # negative operator value must be clamped so the bound can't be defeated.
        hi, captured = _construct_with_fake_openai(max_retries=10000)
        self.assertEqual(hi.max_retries, llm_provider._MAX_RETRIES_CEILING)
        self.assertEqual(captured["max_retries"], llm_provider._MAX_RETRIES_CEILING)
        lo, _ = _construct_with_fake_openai(max_retries=-3)
        self.assertEqual(lo.max_retries, 0)

    def test_invalid_timeout_falls_back_to_default(self):
        # timeout=0 -> httpx immediate expiry would brick every call; inf/nan would
        # silently defeat the slot-hold bound; a bool/non-numeric is malformed. All
        # must fall back to the default instead.
        for bad in (0, -5, float("inf"), float("nan"), True, "abc"):
            provider, captured = _construct_with_fake_openai(timeout=bad)
            self.assertEqual(provider.timeout, llm_provider.DEFAULT_REQUEST_TIMEOUT)
            self.assertEqual(captured["timeout"], llm_provider.DEFAULT_REQUEST_TIMEOUT)


class ConcurrencyConfigTests(unittest.TestCase):
    def setUp(self):
        _reset_shared_semaphore()

    def tearDown(self):
        _reset_shared_semaphore()

    def test_default_concurrency_is_five_env_isolated(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            provider, _ = _construct_with_fake_openai()
        self.assertEqual(provider._max_concurrency, 5)

    def test_env_read_at_construction_not_import(self):
        # Proves the import-time-global bug is fixed: exporting the env AFTER
        # import still takes effect at construction.
        with mock.patch.dict(os.environ, {"LLM_MAX_CONCURRENCY": "3"}, clear=True):
            provider, _ = _construct_with_fake_openai()
        self.assertEqual(provider._max_concurrency, 3)

    def test_explicit_override(self):
        provider, _ = _construct_with_fake_openai(max_concurrency=2)
        self.assertEqual(provider._max_concurrency, 2)

    def test_env_path_is_clamped_to_ceiling(self):
        # The env var is the primary tuning channel; an oversized value must be
        # clamped too (not just the explicit-override path).
        with mock.patch.dict(os.environ, {"LLM_MAX_CONCURRENCY": "99999"}, clear=True):
            provider, _ = _construct_with_fake_openai()
        self.assertEqual(provider._max_concurrency, llm_provider._MAX_CONCURRENCY_CEILING)

    def test_default_providers_share_one_process_wide_semaphore(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            p1, _ = _construct_with_fake_openai()
            p2, _ = _construct_with_fake_openai()
        self.assertIs(p1._api_semaphore, p2._api_semaphore)
        self.assertEqual(p2._max_concurrency, 5)  # not multiplied per instance

    def test_overrides_get_distinct_semaphores(self):
        p1, _ = _construct_with_fake_openai(max_concurrency=2)
        p2, _ = _construct_with_fake_openai(max_concurrency=2)
        self.assertIsNot(p1._api_semaphore, p2._api_semaphore)

    def test_clamp_lower_bound(self):
        for bad in (0, -3):
            provider, _ = _construct_with_fake_openai(max_concurrency=bad)
            self.assertEqual(provider._max_concurrency, 1)

    def test_clamp_upper_bound(self):
        provider, _ = _construct_with_fake_openai(max_concurrency=9999)
        self.assertEqual(provider._max_concurrency, llm_provider._MAX_CONCURRENCY_CEILING)

    def test_float_is_truncated(self):
        provider, _ = _construct_with_fake_openai(max_concurrency=2.9)
        self.assertEqual(provider._max_concurrency, 2)

    def test_non_numeric_raises(self):
        with self.assertRaises((ValueError, TypeError)):
            _construct_with_fake_openai(max_concurrency="abc")


class ConcurrencyGateTests(unittest.TestCase):
    """Behavioral proof that the semaphore actually bounds in-flight calls."""

    def _drive(self, limit, fired):
        import threading
        import time

        p = _bare_provider(max_concurrency=limit)
        lock = threading.Lock()
        release = threading.Event()
        state = {"in_flight": 0, "peak": 0}

        def fake_create(**_k):
            with lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            # Hold the slot until the test releases, so concurrent callers pile up.
            if not release.wait(timeout=5):
                raise AssertionError("release never set — semaphore likely deadlocked")
            with lock:
                state["in_flight"] -= 1
            return _FakeCompletion(_FakeMessage(content="ok"), usage=None)

        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
        )

        errors = []

        def worker():
            try:
                p.generate_text("m", [ContentPart("text", "hi")])
            except Exception as e:  # pragma: no cover - surfaced via `errors`
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(fired)]
        for t in threads:
            t.start()
        # Wait until the cap is actually reached (fail loudly if it never is).
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with lock:
                if state["peak"] >= limit:
                    break
            time.sleep(0.01)
        with lock:
            peak_at_cap = state["peak"]
        release.set()
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "worker thread hung — semaphore deadlock")
        self.assertFalse(errors, f"workers raised: {errors}")
        return peak_at_cap

    def test_cap_reached_but_never_exceeded(self):
        # Fire more than the limit; peak must land exactly at the limit.
        peak = self._drive(limit=2, fired=5)
        self.assertEqual(peak, 2)

    def test_limit_one_serializes(self):
        peak = self._drive(limit=1, fired=3)
        self.assertEqual(peak, 1)


class InitWiringTests(unittest.TestCase):
    def test_real_init_generate_text_path(self):
        # Guards against a typo in the init→semaphore→call path (every other
        # generate_* test fabricates the semaphore via _bare_provider). Also the
        # end-to-end cost_missing check: the fake completion has no usage block, so
        # a real generate_text call must surface cost_missing=True to the caller.
        provider, _ = _construct_with_fake_openai()
        with self.assertLogs(level="WARNING"):
            text, usage = provider.generate_text("m", [ContentPart("text", "hi")])
        self.assertEqual(text, "ok")
        self.assertEqual(usage.cost, 0.0)
        self.assertTrue(usage.cost_missing)


if __name__ == "__main__":
    unittest.main()
