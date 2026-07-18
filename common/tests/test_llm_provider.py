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
    # __init__ also seeds the per-run budget; __new__-built instances need it or the
    # generation/usage paths would AttributeError. Default is a no-op budget.
    p.budget = llm_provider.RunBudget.disabled()
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

    def test_pdf_url_passthrough(self):
        url = "https://eprint.iacr.org/2025/536.pdf"
        blocks, has_pdf = self.p._to_message_content([ContentPart("pdf", url)])
        self.assertTrue(has_pdf)
        self.assertEqual(blocks[0]["file"]["file_data"], url)

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


class ResponseEnvelopeTests(unittest.TestCase):
    """The envelope is validated before any choices[0] access: OpenRouter can
    return HTTP 200 with an in-body error object, and a degenerate response can
    carry empty choices. Both must raise the typed error AND keep the billed
    generation inside the fail-closed spend accounting."""

    def _provider_with_create(self, completion):
        p = _bare_provider()
        create = mock.Mock(return_value=completion)
        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )
        return p

    def test_empty_choices_raises_typed_error_not_indexerror(self):
        completion = types.SimpleNamespace(choices=[], usage=None)
        p = self._provider_with_create(completion)
        with self.assertRaises(llm_provider.LLMResponseEnvelopeError):
            p.generate_text("m", [ContentPart("text", "hi")])
        # The unusable-but-possibly-billed call must not escape spend
        # accounting: absent usage is recorded fail-closed (cost_missing),
        # which stickily flips cost-cap trust off.
        self.assertFalse(p.budget.cost_reliable)

    def test_missing_choices_attribute_raises_typed_error(self):
        completion = types.SimpleNamespace(usage=None)
        p = self._provider_with_create(completion)
        with self.assertRaises(llm_provider.LLMResponseEnvelopeError):
            p.generate_text("m", [ContentPart("text", "hi")])

    def test_in_body_402_error_is_hard_failure(self):
        completion = types.SimpleNamespace(
            choices=[], usage=None,
            error={"code": 402, "message": "Insufficient credits"},
        )
        p = self._provider_with_create(completion)
        with self.assertRaises(llm_provider.LLMResponseEnvelopeError) as ctx:
            p.generate_text("m", [ContentPart("text", "hi")])
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertTrue(llm_provider.is_hard_llm_failure(ctx.exception))
        self.assertIn("402", str(ctx.exception))

    def test_in_body_5xx_error_is_soft(self):
        completion = types.SimpleNamespace(
            choices=[], usage=None,
            error={"code": 502, "message": "Provider returned error"},
        )
        p = self._provider_with_create(completion)
        with self.assertRaises(llm_provider.LLMResponseEnvelopeError) as ctx:
            p.generate_text("m", [ContentPart("text", "hi")])
        self.assertFalse(llm_provider.is_hard_llm_failure(ctx.exception))

    def test_statusless_in_body_error_is_soft(self):
        completion = types.SimpleNamespace(
            choices=[], usage=None,
            error={"message": "insufficient credit"},  # billing words in the
            # message must NOT make a statusless envelope error hard: the text
            # is provider/content-influenced (same policy as ValueError).
        )
        p = self._provider_with_create(completion)
        with self.assertRaises(llm_provider.LLMResponseEnvelopeError) as ctx:
            p.generate_text("m", [ContentPart("text", "hi")])
        self.assertIsNone(ctx.exception.status_code)
        self.assertFalse(llm_provider.is_hard_llm_failure(ctx.exception))

    def test_structured_path_validates_envelope(self):
        completion = types.SimpleNamespace(choices=[], usage=None)
        p = _bare_provider()
        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(parse=mock.Mock(return_value=completion))
            )
        )
        with self.assertRaises(llm_provider.LLMResponseEnvelopeError):
            p.generate_structured("m", [ContentPart("text", "hi")], _Schema)


class GenerateTextLengthTests(unittest.TestCase):
    """finish_reason parity for the free-form path: a "length" finish escalates
    the cap like the structured path; exhaustion returns the partial text (prose
    stays useful truncated, unlike JSON) with every attempt's usage summed."""

    def _provider_with_side_effect(self, completions, **kwargs):
        p = _bare_provider(**kwargs)
        create = mock.Mock(side_effect=completions)
        p.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )
        return p, create

    def test_length_finish_escalates_and_returns_complete_text(self):
        usage = _FakeUsage({"prompt_tokens": 10, "completion_tokens": 16384})
        truncated = _FakeCompletion(_FakeMessage(content="partial"), usage=usage,
                                    finish_reason="length")
        complete = _FakeCompletion(_FakeMessage(content="full answer"), usage=usage,
                                   finish_reason="stop")
        p, create = self._provider_with_side_effect([truncated, complete])

        text, tokens = p.generate_text("m", [ContentPart("text", "hi")])

        self.assertEqual(text, "full answer")
        self.assertEqual(create.call_count, 2)
        first_cap = create.call_args_list[0].kwargs["max_tokens"]
        second_cap = create.call_args_list[1].kwargs["max_tokens"]
        self.assertEqual(second_cap, first_cap * 2)
        # Both attempts were billed; both must be in the returned total.
        self.assertEqual(tokens.output_tokens, 2 * 16384)

    def test_length_exhaustion_returns_partial_text(self):
        usage = _FakeUsage({"prompt_tokens": 10, "completion_tokens": 16384})
        truncated = _FakeCompletion(_FakeMessage(content="partial"), usage=usage,
                                    finish_reason="length")
        p, create = self._provider_with_side_effect(
            [truncated, truncated], length_retry_attempts=1)

        text, _ = p.generate_text("m", [ContentPart("text", "hi")])

        self.assertEqual(text, "partial")
        self.assertEqual(create.call_count, 2)


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


@unittest.skipUnless(
    os.environ.get("OPENROUTER_API_KEY"),
    "live C3 checks: set OPENROUTER_API_KEY (real round-trips prove the per-run budget "
    "and hard-failure classification against the live API — no mock can)",
)
class LiveC3Tests(unittest.TestCase):
    """C3 STEP 9 GO-condition: real-wire verification. Close-out BLOCKS if these skip."""

    def _key(self):
        return os.environ["OPENROUTER_API_KEY"]

    def _model(self):
        return os.environ.get("OPENROUTER_TEST_MODEL", "openai/gpt-4o-mini")

    def test_real_call_records_tokens_and_cost_in_budget(self):
        budget = RunBudget(max_tokens=100_000)
        provider = create_provider(self._key(), budget=budget)
        text, usage = provider.generate_text(
            self._model(), [ContentPart("text", "Reply with the single word: ok")])
        self.assertTrue(text)
        snap = budget.snapshot()
        # The one authoritative sink recorded exactly this call's usage.
        self.assertEqual(snap.input_tokens + snap.output_tokens,
                         usage.input_tokens + usage.output_tokens)
        self.assertGreater(snap.input_tokens + snap.output_tokens, 0)
        # is_byok is read from the REAL response shape — on a normal (non-BYOK) key it
        # is a bool and False; this pins the field location the R8 cost branch rests on.
        self.assertIsInstance(usage.byok, bool)
        print(f"\n[live] budget recorded tokens={snap.input_tokens + snap.output_tokens} "
              f"cost=${snap.cost:.6f} byok={usage.byok} cost_missing={usage.cost_missing}")

    def test_ceiling_trips_second_fresh_entry(self):
        # max_tokens=1: the first real call blows past it, so the SECOND fresh entry
        # deterministically raises before making any call.
        budget = RunBudget(max_tokens=1)
        provider = create_provider(self._key(), budget=budget)
        provider.generate_text(self._model(), [ContentPart("text", "Reply: ok")])
        self.assertTrue(budget.exceeded)
        with self.assertRaises(BudgetExceededError):
            provider.generate_text(self._model(), [ContentPart("text", "Reply: ok again")])

    def test_invalid_key_401_is_hard(self):
        provider = create_provider("sk-or-deadbeef-invalid-key-000")
        try:
            provider.generate_text(self._model(), [ContentPart("text", "hi")])
            self.fail("expected an auth error from a bogus key")
        except Exception as e:  # noqa: BLE001 — we classify, then assert
            self.assertTrue(is_hard_llm_failure(e),
                            f"a real invalid-key error must classify as hard: {describe_exc(e)}")
            print(f"\n[live] invalid-key classified hard: {describe_exc(e)}")

    def test_reasoning_tokens_are_subset_of_output(self):
        # RunBudget._count treats reasoning as a SUBSET of output tokens; if that were
        # false the ceiling would undercount exactly the expensive calls. Assert the
        # relation on a real thinking-enabled response.
        model = os.environ.get("OPENROUTER_TEST_REASONING_MODEL", "openai/o3-mini")
        provider = create_provider(self._key())
        try:
            _, usage = provider.generate_text(
                model, [ContentPart("text", "Think, then reply with the word: ok")],
                thinking_budget=1024)
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"reasoning model {model} unavailable on this key: {describe_exc(e)}")
        self.assertGreaterEqual(usage.output_tokens, usage.thinking_tokens)
        print(f"\n[live] output_tokens={usage.output_tokens} >= reasoning_tokens={usage.thinking_tokens}")


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


# ======================= C3 — per-run spend control =========================

import httpx  # noqa: E402  (openai's own transport dep; used to build real SDK errors)

from leanrepo_common.llm_provider import (  # noqa: E402
    BudgetExceededError,
    RunBudget,
    RunHealth,
    _reraise_if_fatal,
    describe_exc,
    is_hard_llm_failure,
    parse_run_budget,
)


def _http_response(status: int) -> httpx.Response:
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.Response(status, request=req)


def _status_error(status: int, cls=None, message: str = "error"):
    """Build a REAL openai SDK status error (clean-room — no fixture copied from
    openai-python's Apache-2.0 test suite)."""
    from openai import APIStatusError
    cls = cls or APIStatusError
    return cls(message, response=_http_response(status), body=None)


class TokenUsageByokTests(unittest.TestCase):
    def test_byok_defaults_false(self):
        self.assertFalse(TokenUsage().byok)

    def test_sum_propagates_byok_independently_of_cost_missing(self):
        a = TokenUsage(byok=True, cost_missing=False)
        b = TokenUsage(byok=False, cost_missing=True)
        s = _sum_usage(a, b)
        self.assertTrue(s.byok)          # OR-propagated
        self.assertTrue(s.cost_missing)  # independent flag


class IsHardLLMFailureTests(unittest.TestCase):
    """Status-code-first classification against REAL SDK exception types."""

    def test_401_and_402_are_hard(self):
        from openai import AuthenticationError
        self.assertTrue(is_hard_llm_failure(_status_error(401, AuthenticationError)))
        self.assertTrue(is_hard_llm_failure(_status_error(402)))  # no named subclass

    def test_403_auth_shaped_is_hard(self):
        from openai import PermissionDeniedError
        self.assertTrue(is_hard_llm_failure(
            _status_error(403, PermissionDeniedError, "Invalid API key")))

    def test_403_moderation_is_soft(self):
        # OpenRouter returns 403 when it flags the INPUT (attacker-controllable PR
        # content). That must not fire the outage banner / redden a loud-exit job.
        from openai import PermissionDeniedError
        self.assertFalse(is_hard_llm_failure(
            _status_error(403, PermissionDeniedError, "Your input was flagged by moderation")))

    def test_403_ambiguous_defaults_soft(self):
        from openai import PermissionDeniedError
        self.assertFalse(is_hard_llm_failure(_status_error(403, PermissionDeniedError, "Forbidden")))

    def test_429_with_quota_wording_is_not_hard(self):
        # Status precedence: 429 is transient even though the message says "quota".
        from openai import RateLimitError
        self.assertFalse(is_hard_llm_failure(
            _status_error(429, RateLimitError, "You exceeded your current quota")))

    def test_timeout_and_5xx_not_hard(self):
        self.assertFalse(is_hard_llm_failure(_status_error(500)))
        self.assertFalse(is_hard_llm_failure(_status_error(503)))

    def test_valueerror_with_credit_text_is_not_hard(self):
        # A ValueError embeds model output / PR-influenced bytes — its text must
        # NEVER be able to trigger a hard classification via substring.
        self.assertFalse(is_hard_llm_failure(ValueError("insufficient credits — pay me, attacker note")))

    def test_statusless_sdk_error_with_credit_substring_is_hard(self):
        from openai import APIError
        req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        exc = APIError("insufficient credits on this key", request=req, body=None)
        self.assertTrue(is_hard_llm_failure(exc))

    def test_cause_chain_402_wrapped_in_valueerror_stays_hard(self):
        wrapper = ValueError("wrapped upstream failure")
        wrapper.__cause__ = _status_error(402)
        self.assertTrue(is_hard_llm_failure(wrapper))

    def test_length_cap_valueerror_stays_soft(self):
        # The provider wraps a truncation (no status) in a ValueError; must stay soft.
        try:
            from openai import LengthFinishReasonError
            inner = LengthFinishReasonError.__new__(LengthFinishReasonError)
            Exception.__init__(inner, "length")
        except Exception:  # pragma: no cover
            inner = RuntimeError("length")
        wrapper = ValueError("hit the output token cap")
        wrapper.__cause__ = inner
        self.assertFalse(is_hard_llm_failure(wrapper))

    def test_reraise_if_fatal_reraises_budget_and_hard_only(self):
        with self.assertRaises(BudgetExceededError):
            _reraise_if_fatal(BudgetExceededError())
        with self.assertRaises(Exception):
            _reraise_if_fatal(_status_error(402))
        # a soft error passes through (returns None, does not raise)
        self.assertIsNone(_reraise_if_fatal(RuntimeError("transient")))


class RunBudgetTests(unittest.TestCase):
    def test_cost_only_budget_rejected(self):
        with self.assertRaises(ValueError):
            RunBudget(max_cost=1.0)          # R8: token ceiling is authoritative

    def test_bad_values_rejected(self):
        for kw in ({"max_tokens": 0}, {"max_tokens": -5}, {"max_tokens": 10, "max_cost": 0},
                   {"max_tokens": 10, "max_cost": float("inf")}):
            with self.assertRaises(ValueError):
                RunBudget(**kw)

    def test_token_ceiling_trips_on_crossing_call(self):
        b = RunBudget(max_tokens=100)
        self.assertFalse(b.record_and_check(TokenUsage(input_tokens=40, output_tokens=20)))  # 60
        self.assertFalse(b.exceeded)
        self.assertTrue(b.record_and_check(TokenUsage(input_tokens=30, output_tokens=20)))   # 110 → crosses
        self.assertTrue(b.exceeded)
        # a later call does not re-report the crossing
        self.assertFalse(b.record_and_check(TokenUsage(input_tokens=1)))

    def test_cost_ceiling_trips(self):
        b = RunBudget(max_tokens=10**9, max_cost=0.01)
        self.assertFalse(b.record_and_check(TokenUsage(cost=0.006)))
        self.assertTrue(b.record_and_check(TokenUsage(cost=0.006)))  # 0.012 → crosses

    def test_disabled_never_trips(self):
        b = RunBudget.disabled()
        self.assertFalse(b.enabled)
        self.assertFalse(b.record_and_check(TokenUsage(input_tokens=10**9, cost=10**9)))
        self.assertFalse(b.exceeded)
        b.raise_if_exceeded()  # no-op

    def test_reasoning_and_cached_not_double_counted(self):
        b = RunBudget(max_tokens=100)
        # thinking/cached are subsets of output/input and must not add on top.
        self.assertFalse(b.record_and_check(
            TokenUsage(input_tokens=50, output_tokens=40, thinking_tokens=30, cached_tokens=20)))  # 90

    def test_cost_reliability_flips_on_cost_missing_and_byok(self):
        b = RunBudget(max_tokens=100, max_cost=1.0)
        self.assertTrue(b.cost_reliable)
        with self.assertLogs(level="WARNING"):
            b.record_and_check(TokenUsage(input_tokens=1, cost_missing=True))
        self.assertFalse(b.cost_reliable)
        b2 = RunBudget(max_tokens=100, max_cost=1.0)
        with self.assertLogs(level="WARNING"):
            b2.record_and_check(TokenUsage(input_tokens=1, byok=True))
        self.assertFalse(b2.cost_reliable)

    def test_cost_cap_keeps_enforcing_after_reliability_lost(self):
        b = RunBudget(max_tokens=10**9, max_cost=0.01)
        b.record_and_check(TokenUsage(cost=0.006))                     # known
        b.record_and_check(TokenUsage(cost=0.0, cost_missing=True))    # flips reliability, adds 0
        self.assertFalse(b.cost_reliable)
        self.assertTrue(b.record_and_check(TokenUsage(cost=0.005)))    # known 0.011 ≥ 0.01 → still trips
        self.assertTrue(b.exceeded)

    def test_raise_if_exceeded_carries_usage_snapshot(self):
        b = RunBudget(max_tokens=10)
        b.record_and_check(TokenUsage(input_tokens=20, output_tokens=5, cost=0.03))
        with self.assertRaises(BudgetExceededError) as ctx:
            b.raise_if_exceeded()
        self.assertEqual(ctx.exception.usage.input_tokens, 20)
        self.assertAlmostEqual(ctx.exception.usage.cost, 0.03)

    def test_thread_safe_single_crossing(self):
        import threading
        b = RunBudget(max_tokens=100)
        crossings = []
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            if b.record_and_check(TokenUsage(input_tokens=10)):
                crossings.append(1)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        # A deadlock in record_and_check would hang a join while sum(crossings) could
        # still be 1 (the crossing is recorded before any later hang) — so assert the
        # threads actually finished, not just the crossing count.
        self.assertFalse(any(t.is_alive() for t in threads), "worker thread hung — lock regression")
        self.assertEqual(sum(crossings), 1)  # exactly one call observes the crossing


class DescribeExcTests(unittest.TestCase):
    def test_includes_class_and_status(self):
        s = describe_exc(_status_error(402, message="insufficient credits"))
        self.assertIn("status=402", s)
        self.assertIn("insufficient credits", s)

    def test_collapses_and_truncates(self):
        s = describe_exc(ValueError("a\nb   c" + "x" * 500), max_len=20)
        self.assertNotIn("\n", s)
        self.assertTrue(s.endswith("…"))
        self.assertLessEqual(len(s), len("ValueError: ") + 21)

    def test_no_status_when_absent(self):
        s = describe_exc(ValueError("plain"))
        self.assertNotIn("status=", s)
        self.assertIn("ValueError", s)


class ParseRunBudgetTests(unittest.TestCase):
    def test_both_unset_is_none(self):
        self.assertIsNone(parse_run_budget(None, None))
        self.assertIsNone(parse_run_budget("", ""))

    def test_whitespace_only_is_unset(self):
        # The value every default action run sends once the inputs exist.
        self.assertIsNone(parse_run_budget("  ", "\t"))

    def test_token_only_builds_budget(self):
        b = parse_run_budget("5000", "")
        self.assertEqual(b.max_tokens, 5000)
        self.assertIsNone(b.max_cost)

    def test_both_builds_budget(self):
        b = parse_run_budget("5000", "0.50")
        self.assertEqual(b.max_tokens, 5000)
        self.assertAlmostEqual(b.max_cost, 0.50)

    def test_cost_only_rejected(self):
        with self.assertRaises(ValueError):
            parse_run_budget("", "0.50")          # R8: token ceiling authoritative

    def test_non_numeric_rejected(self):
        with self.assertRaises(ValueError):
            parse_run_budget("lots", "")
        with self.assertRaises(ValueError):
            parse_run_budget("5000", "cheap")

    def test_zero_and_negative_rejected(self):
        for bad in ("0", "-5"):
            with self.assertRaises(ValueError):
                parse_run_budget(bad, "")

    def test_float_tokens_rejected(self):
        with self.assertRaises(ValueError):
            parse_run_budget("10.5", "")


class RunHealthTests(unittest.TestCase):
    def test_clean_run_is_not_degraded(self):
        h = RunHealth()
        h.record_fresh_success()
        h.record_fresh_success()
        self.assertFalse(h.degraded)          # no false alarm

    def test_hard_failure_degrades(self):
        h = RunHealth()
        h.record_fresh_success()
        h.record_hard_failure()
        self.assertTrue(h.degraded)

    def test_budget_trip_degrades_and_is_idempotent(self):
        h = RunHealth()
        h.record_budget_trip("b.lean", "a.lean")
        h.record_budget_trip("a.lean", "c.lean")  # burst; dedup, keep order
        self.assertTrue(h.degraded)
        self.assertTrue(h.budget_exceeded)
        self.assertEqual(h.skipped_files, ["b.lean", "a.lean", "c.lean"])

    def test_fresh_successes_do_not_suppress_banner_R5(self):
        # A warm-cache/all-fallback run has fresh successes yet a hard failure must
        # still fire the banner — degraded is independent of fresh_successes.
        h = RunHealth()
        for _ in range(5):
            h.record_fresh_success()
        h.record_hard_failure()
        self.assertTrue(h.degraded)

    def test_thread_safe_counts(self):
        import threading
        h = RunHealth()
        barrier = threading.Barrier(30)

        def worker(i):
            barrier.wait()
            if i % 2 == 0:
                h.record_fresh_success()
            else:
                h.record_hard_failure()
            h.record_budget_trip(f"f{i}.lean")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertFalse(any(t.is_alive() for t in threads), "worker hung — lock regression")
        self.assertEqual(h.fresh_successes, 15)
        self.assertEqual(h.hard_failures, 15)
        self.assertEqual(len(h.skipped_files), 30)   # all distinct, none lost
        self.assertTrue(h.degraded)


class BudgetIntegrationTests(unittest.TestCase):
    """The budget wired through the real generation paths."""

    def _client(self, create=None, parse=None):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create, parse=parse)))

    def test_fresh_entry_over_budget_raises_before_any_call(self):
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=10)
        p.budget.record_and_check(TokenUsage(input_tokens=20))  # already over
        create = mock.Mock()
        parse = mock.Mock()
        p.client = self._client(create, parse)
        with self.assertRaises(BudgetExceededError):
            p.generate_text("m", [ContentPart("text", "hi")])
        with self.assertRaises(BudgetExceededError):
            p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        create.assert_not_called()
        parse.assert_not_called()

    def test_per_call_usage_equals_budget_delta(self):
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=10**9)
        completion = _FakeCompletion(_FakeMessage(parsed=_Schema(x=1)),
                                     usage=_FakeUsage({"prompt_tokens": 12, "completion_tokens": 5, "cost": 0.002}))
        p.client = self._client(parse=mock.Mock(return_value=completion))
        _, usage = p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        snap = p.budget.snapshot()
        self.assertEqual(usage.input_tokens, snap.input_tokens)
        self.assertEqual(usage.output_tokens, snap.output_tokens)
        self.assertAlmostEqual(usage.cost, snap.cost)

    def test_usage_from_reads_is_byok(self):
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=10**9, max_cost=1.0)
        completion = _FakeCompletion(_FakeMessage(content="ok"),
                                     usage=_FakeUsage({"prompt_tokens": 3, "completion_tokens": 1,
                                                       "cost": 0.0, "is_byok": True}))
        p.client = self._client(create=mock.Mock(return_value=completion))
        with self.assertLogs(level="WARNING"):
            _, usage = p.generate_text("m", [ContentPart("text", "hi")])
        self.assertTrue(usage.byok)
        self.assertFalse(p.budget.cost_reliable)

    def test_mid_loop_trip_breaks_then_one_phase2_answer(self):
        # A tool round crosses the ceiling → stop gathering, produce exactly one
        # structured answer from the evidence so far (graceful degradation).
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=50)
        tc = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="t", arguments="{}"))
        r1 = _FakeCompletion(types.SimpleNamespace(content=None, tool_calls=[tc]),
                             usage=_FakeUsage({"prompt_tokens": 40, "completion_tokens": 20}))  # 60 → over
        create = mock.Mock(side_effect=[r1])   # round 2 must never be requested
        parse = mock.Mock(return_value=_FakeCompletion(_FakeMessage(parsed=_Schema(x=9)),
                                                       usage=_FakeUsage({"prompt_tokens": 1, "completion_tokens": 1})))
        p.client = self._client(create, parse)
        parsed, _ = p.generate_structured(
            "m", [ContentPart("text", "hi")], _Schema,
            tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
            tool_runner=lambda n, a: "r")
        self.assertEqual(parsed.x, 9)
        self.assertEqual(create.call_count, 1)   # broke before a 2nd tool round
        self.assertEqual(parse.call_count, 1)    # exactly one final answer

    def test_completed_round_usage_survives_a_later_fatal_round(self):
        # Acceptance #2 + R3: round 1 usage is recorded; round 2 raises a hard 402
        # which must re-raise (not fall back), while round 1's usage is retained.
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=10**9)
        tc = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="t", arguments="{}"))
        r1 = _FakeCompletion(types.SimpleNamespace(content=None, tool_calls=[tc]),
                             usage=_FakeUsage({"prompt_tokens": 11, "completion_tokens": 7, "cost": 0.004}))
        create = mock.Mock(side_effect=[r1, _status_error(402)])
        parse = mock.Mock(return_value=_FakeCompletion(_FakeMessage(parsed=_Schema(x=1))))
        p.client = self._client(create, parse)
        with self.assertRaises(Exception) as ctx:
            p.generate_structured(
                "m", [ContentPart("text", "hi")], _Schema,
                tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
                tool_runner=lambda n, a: "r")
        self.assertTrue(is_hard_llm_failure(ctx.exception))   # fatal propagated, not swallowed
        parse.assert_not_called()                             # did NOT fall back to a plain call
        snap = p.budget.snapshot()
        self.assertEqual(snap.input_tokens, 11)              # round 1 usage retained
        self.assertAlmostEqual(snap.cost, 0.004)

    def test_soft_gather_error_retains_round_usage_in_returned_total(self):
        # A SOFT error in a later tool round → fall through to phase-2, and the
        # completed round's usage must survive in the RETURNED total (not just the
        # budget). Distinct from the fatal case above, where the total is discarded.
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=10**9)
        tc = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="t", arguments="{}"))
        r1 = _FakeCompletion(types.SimpleNamespace(content=None, tool_calls=[tc]),
                             usage=_FakeUsage({"prompt_tokens": 9, "completion_tokens": 4, "cost": 0.001}))
        create = mock.Mock(side_effect=[r1, RuntimeError("transient blip")])  # round 2 soft-fails
        parse = mock.Mock(return_value=_FakeCompletion(
            _FakeMessage(parsed=_Schema(x=3)),
            usage=_FakeUsage({"prompt_tokens": 20, "completion_tokens": 6, "cost": 0.003})))
        p.client = self._client(create, parse)
        parsed, usage = p.generate_structured(
            "m", [ContentPart("text", "hi")], _Schema,
            tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
            tool_runner=lambda n, a: "r")
        self.assertEqual(parsed.x, 3)                 # fell back to a structured answer
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(usage.input_tokens, 9 + 20)  # round-1 usage retained in the RETURN
        self.assertEqual(usage.output_tokens, 4 + 6)
        self.assertAlmostEqual(usage.cost, 0.001 + 0.003)

    def test_multiround_success_returned_usage_equals_budget_delta(self):
        # The double-count surface: rounds_usage folded in `finally` PLUS the phase-2
        # _record_usage. On a clean multi-round run the returned usage must equal the
        # budget delta exactly (nothing recorded twice, nothing dropped).
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=10**9)
        tc = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="t", arguments="{}"))
        r1 = _FakeCompletion(types.SimpleNamespace(content=None, tool_calls=[tc]),
                             usage=_FakeUsage({"prompt_tokens": 7, "completion_tokens": 2, "cost": 0.001}))
        r2 = _FakeCompletion(types.SimpleNamespace(content="stop", tool_calls=None),
                             usage=_FakeUsage({"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.001}))
        create = mock.Mock(side_effect=[r1, r2])
        parse = mock.Mock(return_value=_FakeCompletion(
            _FakeMessage(parsed=_Schema(x=8)),
            usage=_FakeUsage({"prompt_tokens": 30, "completion_tokens": 9, "cost": 0.005})))
        p.client = self._client(create, parse)
        _, usage = p.generate_structured(
            "m", [ContentPart("text", "hi")], _Schema,
            tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
            tool_runner=lambda n, a: "r")
        snap = p.budget.snapshot()
        self.assertEqual(usage.input_tokens, snap.input_tokens)
        self.assertEqual(usage.output_tokens, snap.output_tokens)
        self.assertAlmostEqual(usage.cost, snap.cost)
        self.assertEqual(usage.input_tokens, 7 + 5 + 30)   # and no round dropped/doubled
        self.assertAlmostEqual(usage.cost, 0.001 + 0.001 + 0.005)


class LengthRetryBudgetTests(unittest.TestCase):
    """The length-retry (truncated structured output) path under the budget."""

    def _length_error(self, completion):
        from openai import LengthFinishReasonError
        try:
            return LengthFinishReasonError(completion=completion)
        except Exception:  # pragma: no cover - signature guard across SDK versions
            e = LengthFinishReasonError.__new__(LengthFinishReasonError)
            Exception.__init__(e, "length")
            e.completion = completion
            return e

    def _client(self, parse):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(parse=parse)))

    def test_truncated_usage_recorded_once_then_escalates_and_succeeds(self):
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=10**9)
        trunc = _FakeCompletion(_FakeMessage(content=None),
                                usage=_FakeUsage({"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.01}))
        good = _FakeCompletion(_FakeMessage(parsed=_Schema(x=5)),
                               usage=_FakeUsage({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.002}))
        parse = mock.Mock(side_effect=[self._length_error(trunc), good])
        p.client = self._client(parse)
        _, usage = p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        self.assertEqual(parse.call_count, 2)                     # escalated once
        # truncated attempt counted exactly once, alongside the successful retry
        self.assertEqual(usage.input_tokens, 100 + 10)
        self.assertEqual(usage.output_tokens, 50 + 5)
        snap = p.budget.snapshot()
        self.assertEqual(snap.input_tokens, 100 + 10)            # budget delta == returned
        self.assertAlmostEqual(snap.cost, 0.012)

    def test_over_budget_stops_escalation(self):
        # Decision 3: once the truncated attempt pushes the run over budget, do NOT
        # escalate (each escalation is another billed generation) — fail out instead.
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=120)
        trunc = _FakeCompletion(_FakeMessage(content=None),
                                usage=_FakeUsage({"prompt_tokens": 100, "completion_tokens": 50}))  # 150 > 120
        parse = mock.Mock(side_effect=[self._length_error(trunc), _FakeCompletion(_FakeMessage(parsed=_Schema(x=1)))])
        p.client = self._client(parse)
        with self.assertRaises(ValueError):
            p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        self.assertEqual(parse.call_count, 1)                    # did NOT escalate
        self.assertEqual(p.budget.snapshot().input_tokens, 100)  # truncated usage still counted

    def test_length_error_without_completion_marks_cost_unreliable(self):
        # Fail closed: no completion → can't count the billed tokens, so at least
        # flip cost reliability rather than silently trust the running total.
        p = _bare_provider()
        p.budget = RunBudget(max_tokens=10**9, max_cost=1.0)
        err = self._length_error(None)
        good = _FakeCompletion(_FakeMessage(parsed=_Schema(x=2)),
                               usage=_FakeUsage({"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}))
        parse = mock.Mock(side_effect=[err, good])
        p.client = self._client(parse)
        with self.assertLogs(level="WARNING"):
            p.generate_structured("m", [ContentPart("text", "hi")], _Schema)
        self.assertFalse(p.budget.cost_reliable)


if __name__ == "__main__":
    unittest.main()
