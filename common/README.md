# common — the shared `leanrepo-common` library

The shared library for the [leanrepo-utils](../README.md) tools. It provides
the single OpenRouter-backed LLM layer and the Lean 4 source utilities that the
[`summary`](../summary/) and [`review`](../review/) GitHub Actions and the
[`sorry-tracker`](../sorry-tracker/) CLI all build on, so there is one
implementation of each concern instead of three copies.

It is a workspace member, not a published package — the three tools depend on
it via `leanrepo-common = { workspace = true }`.

## What it provides

Two submodules under `leanrepo_common`:

| Module | Public surface | Responsibility |
| --- | --- | --- |
| `llm_provider` | `create_provider`, `OpenRouterProvider`, `ContentPart`, `TokenUsage` | The OpenRouter gateway. Every model (Claude, Gemini, GPT, DeepSeek, …) is reached through OpenRouter's OpenAI-compatible Chat Completions endpoint, selected purely by slug — no per-provider branching. Handles schema-validated structured output, free-form and tool-calling generation, PDF/multimodal content parts, prompt caching, reasoning budgets, and token-usage accounting. |
| `lean_utils` | `is_in_comment`, `strip_comments`, `scrub_line`, `detect_src_dir`, `file_path_to_module_name`, `import_search_dirs`, `resolve_import`, `FileCache` | Lean 4 source utilities: comment- and string-aware line scanners (handling `--` line comments and nested `/- ... -/` block comments) used to detect `sorry`/`admit` without false positives, bidirectional mapping between dotted module names and files on disk, `.lake` import resolution, and a small read-once file cache. |

## Usage

The library is consumed in-process by the sibling tools:

```python
from leanrepo_common.llm_provider import create_provider, ContentPart
from leanrepo_common.lean_utils import is_in_comment, file_path_to_module_name

provider = create_provider(api_key)                 # OPENROUTER_API_KEY
parsed, usage = provider.generate_structured(
    model, [ContentPart(type="text", text=prompt)], schema,
)
```

`generate_structured` returns a validated Pydantic instance plus a `TokenUsage`.
The public surface (`ContentPart`, `TokenUsage`, `OpenRouterProvider`,
`create_provider`) is kept stable for callers.

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
