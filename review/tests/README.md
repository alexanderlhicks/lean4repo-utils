# Tests

Unit tests for the modules that power the GitHub Action. The suite is pure
Python — no network, no Lean toolchain, no real API keys — so it runs in a few
seconds locally and in CI.

## Running

```bash
# From the repository root, via uv (https://docs.astral.sh/uv/):
uv run pytest tests/ -q                      # full suite
uv run pytest tests/test_llm_provider.py -q  # one file
uv run pytest tests/ -k "reasoning" -q       # filter by name
uv run pytest tests/ -x --tb=short           # stop on first failure
```

`uv run` resolves the environment from `uv.lock` automatically. The LLM
provider imports the `openai` SDK lazily, and the provider tests avoid that
import entirely (see below), so they also run under a bare interpreter without
`openai` installed (the one exception, an openai-gated length-error test, skips
in that case).

## Layout

| File | Module under test | Focus |
|------|-------------------|-------|
| [`test_llm_provider.py`](./test_llm_provider.py) | `llm_provider.py` | The OpenRouter-backed client: request construction, structured-output wiring, usage extraction. |
| [`test_review.py`](./test_review.py) | `review.py` | Diff parsing, mechanical pre-checks, SSRF protection, Pydantic schemas, the orchestration entrypoint. |
| [`test_lean_utils.py`](./test_lean_utils.py) | `lean_utils.py` | Module-name resolution, comment detection (including nested block comments), the file-content cache, src-dir detection via `lakefile.{toml,lean}`. |
| [`test_lean_info_extractor.py`](./test_lean_info_extractor.py) | `lean_info_extractor.py` | Lean declaration extraction, `sorry`/axiom reporting, diagnostics formatting, GitHub Actions output formatting. |
| [`test_discover_files.py`](./test_discover_files.py) | `discover_files.py` | Dependency graph traversal (forward and reverse, transitive by depth), file-index construction. |

## Coverage details

### `test_llm_provider.py`

The provider is a single OpenRouter-backed client — the model slug carries the
upstream provider, so there is no per-provider branching to test. The tests
cover the seams that matter:

- `DataUrlTests` — bytes become `data:` URLs; existing URLs pass through.
- `MessageContentTests` — `ContentPart`s convert to OpenAI/OpenRouter content
  blocks: text, cache-marked text (`cache_control` breakpoint), image (data URL
  vs. passthrough URL), PDF (`file` block + `has_pdf`), and a `caplog`-asserted
  warning when an unknown part type is skipped.
- `ExtraBodyTests` — `provider.require_parameters` is always set; the
  `response-healing` plugin is always present and `file-parser` is added only
  when a PDF is present; `reasoning` is built from the thinking budget, falls
  back to the provider's `reasoning_default`, and is omitted when there's no
  budget (or a zero budget).
- `MaxTokensTests` — top-level `max_tokens` is lifted above the reasoning
  budget so there's room for the final answer.
- `GenerateStructuredTests` — drive `generate_structured` against a fake
  `chat.completions.parse`: returns the parsed object + a `TokenUsage`; falls
  back to `model_validate_json` when `.parsed` is missing; raises when there's
  no output; zero usage when the response carries none. Also asserts the wiring
  (schema as `response_format`, `reasoning`/`provider` in `extra_body`).
- `FactoryTests` — `create_provider` rejects an empty key and constructs the
  client against `OPENROUTER_BASE_URL` with the attribution headers.

The provider imports `openai` only inside `__init__`. Pure-helper tests build an
instance via `OpenRouterProvider.__new__` (no client, no import); the factory
test injects a fake `openai` module into `sys.modules`.

### `test_review.py`

- **Diff parsing** (`TestSplitDiffIntoFiles`, `TestExtractAddedLines`,
  `TestGetDiffLines`) — unified-diff split into per-file chunks, extraction
  of added lines, rename handling, non-Lean files preserved.
- **Prompt-size budget** (`TestFitPromptToBudget`) — the helper that trims
  `REPO_CONTEXT` / `DEPENDENCY_CONTEXT` when the assembled prompt would
  exceed the per-call character budget (`MAX_PROMPT_CHARS`, default
  2.5M ≈ 830K tokens); keeps the file under review, diff, and spec checklist
  intact.
- **`REPO_CONTEXT` rendering and filtering** (`TestFormatRepoFiles`) — renders
  the discovered-files dict into the prompt block format and drops sibling
  changed files from per-file reviews.
- **Lean-aware text handling** (`TestIsInComment`, `TestIsInString`) —
  single-line and block comments (including nested), string-literal recognition.
- **Mechanical pre-checks** (`TestMechanicalPrechecks`) — `sorry`/axiom
  scanning on diffs, comment-only matches ignored, non-Lean files skipped,
  large-file warnings.
- **SSRF protection** (`TestValidateUrl`, `TestCheckIpSafe`,
  `TestResolveAndValidate`, `TestExternalFetch`) — the external-reference
  fetcher rejects localhost, RFC1918 ranges, cloud metadata endpoints,
  non-`http(s)` schemes, and DNS names that resolve to private IPs. Redirects
  are re-validated before being followed.
- **Schema validation** (`TestPydanticSchemas`, `TestStructuredSynthesisInput`)
  — the Pydantic models that shape each agent's structured output
  (`FileReview`, `TriageResult`, `SpecChecklist`, `CrossFileAnalysis`,
  `SynthesisSummary`) accept expected fields and reject missing required ones.
- **Main-flow smoke test** (`TestMainFlow`) — early exit when no Lean files
  changed.

### `test_lean_utils.py`

- `TestFilePathToModuleName` — path → module mapping with `src/`, `lib/`,
  `Mathlib/` prefixes and explicit `src_dir` overrides.
- `TestIsInComment` — comprehensive nested-block-comment state machine.
- `TestFileCache` — the read-once/read-lines cache.
- `TestDetectSrcDir` — reading `lakefile.toml` / `lakefile.lean` to pick the
  source root; `toml` takes precedence.

### `test_lean_info_extractor.py`

- `TestGetLeanDeclarations` — parses `def`, `theorem`, `lemma`, `structure`,
  `noncomputable def` from a real on-disk Lean file.
- `TestExtractSorryWarnings` — finds `sorry` tokens but ignores comments and
  tolerates nested block comments.
- `TestExtractDiagnostics`, `TestExtractInfoForFiles`, `TestExtractLightInfo` —
  the wrapper that assembles per-file info; gracefully degrades when `lake`
  isn't on PATH.
- `TestFormatForReview` — formatted output with/without diagnostics, sorries,
  and axioms.
- `TestGitHubOutputFormatting` — multi-line values use heredoc syntax.

### `test_discover_files.py`

- `TestGetLeanModuleName` — the `discover_files.py` convenience wrapper.
- `TestGetDependentLeanFiles`, `TestGetDependencyLeanFiles` — forward and
  reverse edges off a parsed `lake exe deps` graph.
- `TestTransitiveDependencies` — BFS by depth; cycle-safe; correct depth tags;
  excludes seed modules.
- `TestBuildLeanFileIndex` — on-disk traversal; skips `.git/` and `.lake/`.
- `TestPartitionContextTiers` — the full-context / summary-context split.

## Patterns and conventions

**No-import provider tests.** The provider's only heavy dependency (`openai`)
is imported lazily in `__init__`, so most provider tests construct an instance
with `OpenRouterProvider.__new__` and exercise the pure helpers directly. The
one test that needs real client construction injects a fake `openai` module via
`mock.patch.dict(sys.modules, ...)`.

**Fake completion objects.** `generate_structured` is driven against small
hand-rolled fakes (`_FakeCompletion` / `_FakeMessage` / `_FakeUsage`) and a
`Mock` `parse` so the exact request kwargs can be asserted without a network
call or an SDK mock framework.

**`caplog` for warning-log assertions.** `_to_message_content` silently skips
unknown `ContentPart.type`s; the test asserts (at `WARNING` level) that the
skip is accompanied by a log line, so a refactor can't turn it into a silent
drop.

## Adding a test

1. Pick the file whose module you're testing; create `tests/test_<module>.py`
   for a new module using the existing files as templates.
2. Group related assertions into a `TestXyz` class — pytest auto-discovers
   `test_*` methods on any class whose name starts with `Test`.
3. If your change touches the request payload sent to OpenRouter, extend
   `ExtraBodyTests` / `MessageContentTests` / `GenerateStructuredTests` — those
   are the guard against drift in how requests are built.
