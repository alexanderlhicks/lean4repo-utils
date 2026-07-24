# common — the shared `leanrepo-common` library

The shared library for the [lean4repo-utils](../README.md) tools. It provides
the single OpenRouter-backed LLM layer and the Lean 4 source utilities that the
[`summary`](../summary/) and [`review`](../review/) GitHub Actions and the
[`sorry-tracker`](../sorry-tracker/) CLI all build on, so there is one
implementation of each concern instead of three copies.

It is a workspace member, not a published package — the three tools depend on
it via `leanrepo-common = { workspace = true }`.

## What it provides

Three submodules under `leanrepo_common`:

| Module | Public surface | Responsibility |
| --- | --- | --- |
| `llm_provider` | `create_provider`, `OpenRouterProvider`, `ContentPart`, `TokenUsage`, `RunBudget`, `BudgetExceededError`, `is_hard_llm_failure`, `RunHealth`, `parse_run_budget`, `describe_exc`, `LLMResponseEnvelopeError` | The OpenRouter gateway. Every model (Claude, Gemini, GPT, DeepSeek, …) is reached through OpenRouter's OpenAI-compatible Chat Completions endpoint, selected purely by slug — no per-provider branching. Handles schema-validated structured output, free-form and tool-calling generation, PDF/multimodal content parts, prompt caching, reasoning budgets, token-usage accounting, and a per-run token/cost spend ceiling. |
| `lean_utils` | comment/string scrubbers (`is_in_comment`, `strip_comments`, `strip_comments_preserve_strings`, `scrub_line`); the canonical escape-hatch matcher (`KERNEL_BYPASS_KEYWORDS`, `keyword_pattern`, `keywords_pattern`, `find_keywords`); module↔file mapping (`detect_src_dir`, `file_path_to_module_name`, `import_search_dirs`, `resolve_import`); `resolve_confined_path`; `FileCache` | Lean 4 source utilities: comment- and string-aware line scanners (handling `--` line comments and nested `/- ... -/` block comments) and the single word-boundary keyword matcher used to detect `sorry`/`admit`/`native_decide`/… without false positives, bidirectional mapping between dotted module names and files on disk, `.lake` import resolution, PR-checkout path confinement, and a small read-once file cache. |
| `diff_utils` | `parse_git_diff_header`, `unquote_git_path` | Git unified-diff parsing shared by review and summary: parse a `diff --git` header (rename-aware) and decode git's C-quoted (non-ASCII) paths so such files are never silently dropped. |

## Usage

The library is consumed in-process by the sibling tools:

```python
from leanrepo_common.llm_provider import create_provider, ContentPart
from leanrepo_common.lean_utils import is_in_comment, file_path_to_module_name

provider = create_provider(api_key)                 # OPENROUTER_API_KEY
parsed, usage = provider.generate_structured(
    model, [ContentPart(type="text", data=prompt)], schema,
)
```

`ContentPart`'s payload field is `data` (not `text`). `generate_structured`
returns a validated Pydantic instance plus a `TokenUsage` — whose `cost` is the
real per-generation spend (OpenRouter returns it automatically) and whose
`cost_missing` flag is `True` whenever a completion returned but reported no usable
cost figure (an explicit `0.0` from a `:free` model is treated as known), so spend
control can fail closed instead of silently counting $0. The public surface
(`ContentPart`, `TokenUsage`, `OpenRouterProvider`, `create_provider`, plus the
per-run spend-control primitives `RunBudget`, `BudgetExceededError`,
`is_hard_llm_failure`, `RunHealth`, `parse_run_budget`, `describe_exc`) is kept
stable for callers.

`create_provider` also accepts three operator-trusted tuning knobs (**never source
them from an untrusted PR checkout**):

- `timeout` (seconds, default 180) — per-attempt request timeout. The worst-case
  time a call holds a concurrency slot is `timeout * (max_retries + 1)`.
- `max_concurrency` — cap on in-flight API calls for this provider. Omit it to
  share a **process-wide** default sized from the `LLM_MAX_CONCURRENCY` env var
  (read when the first *default* provider is constructed, default 5); pass a value
  to give the provider its own independent limit.
- `budget` (`RunBudget`) — a per-run token/cost ceiling. Omit for a no-op budget
  (behaviour unchanged; this is why existing callers need no changes). See below.

### Per-run spend control (`RunBudget`)

`RunBudget` bounds a *single run* so one huge or hostile PR can't drain a shared
key. It layers **on top of** OpenRouter's own hard account cap — per-key credit
limits and account balance are server-side and bug-proof (they return `402` when
depleted), so that stays the real ceiling; `RunBudget` only stops *this run*.

```python
from leanrepo_common.llm_provider import create_provider, RunBudget

provider = create_provider(api_key, budget=RunBudget(max_tokens=200_000, max_cost=1.0))
```

- **The token ceiling is authoritative.** A cost-only budget (`max_cost` without
  `max_tokens`) is rejected at construction: OpenRouter `cost` can be absent, and
  under **BYOK** it is only OpenRouter's fee (can read `0.0` while real upstream
  spend occurs), so cost alone is not a trustworthy bound. On any generation that
  reports no cost or is BYOK, `cost_reliable` flips `False` (a one-time warning);
  the token cap keeps enforcing, and the cost cap keeps enforcing against the
  *known* cost accumulated so far.
- **Graceful, not abrupt.** A run that is already over budget on a *fresh* call
  raises `BudgetExceededError` before spending anything. A trip that happens
  mid-run degrades instead: the tool-gathering loop breaks and the model still
  produces one final answer from the evidence gathered.
- **Honest soft bound.** The budget check is poll-then-act, not a reservation, and
  one budget is shared across a tool's worker pool. So when the ceiling is reached,
  every one of the up-to-`max_workers` calls already in flight can each still run a
  full generation (each up to `length_retry_max_tokens`) before the next check sees
  the trip — worst-case overshoot is ≈ `max_workers` maximal generations, not one.
  It also bounds one run, not aggregate spend across repeated runs — the OpenRouter
  key credit limit is that (bug-proof) layer. Tightening this to a hard reservation
  (using `RunBudget.record_and_check`'s atomic crossing signal to gate calls before
  they start) is possible future work; C3 deliberately ships the soft bound.

`is_hard_llm_failure(exc)` classifies whether an exception is a spend/auth/quota
failure (`401`/`402`, or an auth-shaped `403`) that must be surfaced loudly, versus
a transient one (`408`/`429`/`5xx`/timeout) or a moderation `403` (which is driven
by attacker-controllable PR input and so is *not* treated as a hard failure).

### Loud-on-failure (`RunHealth`, `_reraise_if_fatal`, `describe_exc`)

Before C3 the tools *failed open*: a `402`/auth/quota error was caught, swallowed,
and the job still exited `0` — a spend or outage looked like a clean pass. C3
inverts that default:

- `_reraise_if_fatal(exc)` is the first thing every LLM-touching `except` does:
  a `BudgetExceededError` or `is_hard_llm_failure(exc)` is re-raised into a
  top-level containment handler rather than degraded away. Everything else keeps
  its existing fail-soft behaviour.
- `RunHealth` is a thread-safe per-run tracker (hard-failure count, budget-trip
  flag, deterministic skipped-files list). Its `degraded` property is the single
  source of truth for the loud banner **and** for forcing a review's
  `review_incomplete` — so a total outage can never render an *Approved* verdict.
  It counts *fresh* (non-cache, non-fallback) successes separately, so a warm
  cache or an all-fallback run can neither suppress the banner (a real failure
  still shows) nor raise a false one.
- `describe_exc(exc)` renders an exception as `Class(status=NNN): truncated msg`
  for the (world-readable) Actions log. PR-visible comments never interpolate an
  exception body at all — model output and provider payloads stay out of the
  comment channel; only a fixed banner string (plus, at most, a status integer)
  is rendered.

The consuming tools (`review.py`, `summary.py`) read the ceiling from
`LLM_MAX_RUN_TOKENS` / `LLM_MAX_RUN_COST` (parsed by `parse_run_budget`, empty ==
disabled) and opt into a non-zero exit with `LLM_LOUD_EXIT`. The exit code is
applied only at the process entrypoint, after the comment is written — never in a
`finally`.

**Provider contract change (all consumers).** When a budget trip or hard failure
occurs *inside* the tool-gathering phase, the provider now re-raises it instead of
silently making a second, doomed tool-less call. Any caller of `create_provider`
(including `sorry-tracker`, which passes no budget and so is bounded only by the
OpenRouter key cap) sees this: a fatal spend/auth error propagates rather than
being retried into more spend.

**Caveats (verified against the OpenRouter docs on 2026-07-08 —
<https://openrouter.ai/docs/api/reference/overview> and its usage-accounting, errors,
and BYOK sub-pages; all statements paraphrased, none copied):**

- Per-run ceilings do **not** bound aggregate spend across repeated (attacker-
  triggered) runs — the per-key credit limit + concurrency settings are that layer.
- **BYOK**: `usage.cost` is fee-only and can be `0.0` while real upstream spend
  occurs; the true figure is `cost_details.upstream_inference_cost` (populated only
  for BYOK). The response `is_byok` flag deterministically flips `cost_reliable`, so
  the token ceiling stays authoritative. Shape-pinned to a live capture, not
  live-verified against a BYOK key (none available).
- **403 classification** is shape-pinned (auth-shaped vs moderation-shaped) via
  real-SDK-class tests + the documented error table; a live moderation `403` body
  was not captured (it requires deliberately flagged input).
- **Streaming (future)**: a mid-stream error arrives as an SSE
  `finish_reason == "error"` over HTTP `200`, invisible to status-code
  classification. The provider is currently non-streaming, so this is a doc caveat
  only.

## Development

This project uses [uv](https://docs.astral.sh/uv/):

```bash
uv run pytest tests/ -q   # uv resolves the env from uv.lock automatically
uv run ruff check .       # lint
```

`openai` is imported lazily (only inside `OpenRouterProvider.__init__`), so the
`lean_utils` scanners and most of the provider's pure helpers run without it
installed.

### Dependencies

Declared in `pyproject.toml` (pinned in the workspace `uv.lock`): `openai`
(pointed at OpenRouter) and `pydantic`.

## License

This project is licensed under the [Apache License 2.0](../LICENSE).
