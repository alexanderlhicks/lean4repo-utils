# summary — AI-generated PR summaries for Lean 4 projects

This GitHub Action generates a concise, high-level summary for a pull request using an LLM, accessed through [OpenRouter](https://openrouter.ai). It analyzes the PR's title, body, and git diff to produce a structured summary, and includes special features for analyzing [Lean](https://lean-lang.org/) projects.

For pull requests with multiple file changes, the action employs a hierarchical approach: it first generates a summary for each individual file's changes and then synthesizes these into a comprehensive, high-level overview of the entire pull request. This ensures that even complex changes are accurately and clearly summarized.

## Usage

You need an [OpenRouter API key](https://openrouter.ai/keys) stored as an Actions secret named `OPENROUTER_API_KEY`. Then create a workflow file at `.github/workflows/pr_summary.yml`:

```yaml
name: 'PR Summary'

on:
  pull_request_target:
    types: [opened, synchronize, reopened]

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions:
  contents: read
  pull-requests: write
  issues: read

jobs:
  summarize:
    runs-on: ubuntu-latest
    steps:
      - name: Generate PR Summary
        uses: alexanderlhicks/lean4repo-utils/summary@0.2
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.OPENROUTER_API_KEY }}
          model: anthropic/claude-haiku-4.5  # any OpenRouter slug, e.g. google/gemini-3-flash-preview, openai/gpt-5-mini
          github_repository: ${{ github.repository }}
          pr_number: ${{ github.event.pull_request.number }}
          # Optional:
          # additional_instructions_path: 'CONTRIBUTING.md'
          # validate_title: 'true'
          # upstream_path: 'ToMathlib/'
```

> **Note on the trigger:** This example uses `pull_request_target` so the workflow also runs for PRs from forks (the `pull_request` event does not expose repository secrets to fork-triggered workflows, and its `GITHUB_TOKEN` is read-only). `pull_request_target` runs in the context of the base branch, so take care not to execute untrusted code from the fork. This action is safe under `pull_request_target` because it only reads the diff and posts a comment — it does not execute code from the PR branch. The checkout uses `pull_request.head.sha` to fetch the correct diff, while the workflow itself runs from the base branch. If your repository does not accept fork PRs, you can switch the trigger to `pull_request` without other changes.
>
> The `issues: read` permission is used to link affected sorries to open GitHub issues labeled `proof wanted`.

### Security & trust model

Summary is safe to run on **every** PR update (open + each new commit), including fork PRs, under `pull_request_target` — which is why the trigger above needs no author gate. Unlike the [review action](../review/README.md#security--trust-model), the summary path **never builds or executes PR code**: it reads the diff and committed source as data, and reads its policy file (`additional_instructions_path`, default `CONTRIBUTING.md`) from the **base ref** (S4), never the PR checkout, so a fork PR cannot rewrite the instructions that steer the model. See the review README's [Security & trust model](../review/README.md#security--trust-model) for the full event-vs-secret table and why *review* (which does build PR code) is restricted to member `/review` comments instead.

## Inputs

| Input | Description | Required | Default |
|---|---|---|---|
| `github_token` | GitHub token for API calls. Should be set to `${{ secrets.GITHUB_TOKEN }}`. | Yes | |
| `api_key` | OpenRouter API key. Store as a repository secret. | Yes | |
| `model` | OpenRouter model slug (e.g., `anthropic/claude-opus-4.8`, `google/gemini-3-pro-preview`, `openai/gpt-5`). Defaults to the open-weight `deepseek/deepseek-v4-flash` (1M context, flash-tier pricing). | No | `deepseek/deepseek-v4-flash` |
| `github_repository` | The GitHub repository in `owner/repo` format. | Yes | |
| `pr_number` | The pull request number. | Yes | |
| `lean_keywords` | Comma-separated list of Lean declaration keywords to track for sorry attribution. | No | `def,abbrev,example,theorem,opaque,lemma,instance,constant,axiom` |
| `additional_instructions_path` | Path to a file with deployment-supplied instructions for the analysis agent. Use it for style guides, progress trackers, framework cross-checks, doc/wiki references, or any project-specific guidance the LLM should apply to the PR diff. The instructions themselves tell the agent what to produce. **Trust model:** in a PR run the file is read from the PR's *base* ref (`github.event.pull_request.base.sha`) — maintainer-owned content — never from the checked-out PR head, so a (fork) PR cannot rewrite the instructions that steer the agent. If the file can't be loaded from the base ref, the analysis is skipped with a workflow warning; it never falls back to the PR checkout. Consequently the path must be a regular file **committed on the base branch** — untracked files, files generated by an earlier workflow step, absolute paths, and symlinks are not loadable from the base ref and skip the analysis. | No | `CONTRIBUTING.md` |
| `reasoning_effort` | Reasoning/thinking effort applied to every model call: `low`, `medium`, or `high`. Empty uses the model default. Ignored by models without reasoning support. | No | `` |
| `validate_title` | Validate PR title against conventional commit format: `type[(scope)]: subject`. | No | `false` |
| `apply_labels` | When `true`, reconcile deterministic PR labels from the diff signals: `sorry-added`, `native_decide`, `axiom-added`. The action manages **only** these labels (adds those that apply, removes the rest) and never touches others; missing labels are created. Requires the token to have `pull-requests: write` (or `issues: write`). **Note:** once enabled, those three names become action-owned — a manually-applied label of the same name is removed on any PR whose diff doesn't warrant it. | No | `false` |
| `upstream_path` | Path prefix for upstream-bound files. If changed files match, a reminder is shown. | No | |
| `max_summary_files` | Hard ceiling on files individually summarized by the LLM in one run. `0` = unlimited (quality>cost). When set, proof-signal files (`sorry`/`admit`/`native_decide`) are summarized first, then other high-priority files up to this count; the rest are listed as "not individually summarized" (still fully covered by the deterministic sorry/declaration/label tracking). Bounds worst-case LLM calls on a very large PR. | No | `0` |
| `max_file_diff_chars` | Max characters of a single file's diff sent to the summarizer before it is truncated at a hunk boundary. Rough guide: Lean averages ~50 chars/line (~4 chars/token), so `60000` ≈ ~1,200 lines ≈ ~15k tokens. Lower for a smaller-context model. | No | `60000` |
| `max_instructions_diff_chars` | Max characters of the whole-PR diff sent to the additional-instructions agent in one call; above this the analysis is skipped. Must fit the model's context alongside the instructions file and response. Rough guide: `400000` ≈ ~8,000 changed lines ≈ ~100k tokens (fits a ~128k-token model). Lower for a smaller-context model. | No | `400000` |
| `llm_max_run_tokens` | Per-run token ceiling. Once exceeded, no further LLM calls are made and the summary degrades gracefully (partial results + a loud banner) instead of draining a shared key. Empty = disabled. The token ceiling is authoritative. | No | `` |
| `llm_max_run_cost` | Per-run cost ceiling in OpenRouter credits. **Requires** `llm_max_run_tokens` (a cost-only budget is rejected — cost can be BYOK-fee-only or absent). Empty = disabled. | No | `` |
| `llm_loud_exit` | When `true`, the action exits **non-zero** if the run degraded (spend/quota/auth failure or budget exhausted), *after* posting the comment. Default `false` keeps the job green with a banner + `::error::` annotation. | No | `false` |

> **Per-run spend control (C3).** `llm_max_run_tokens` / `llm_max_run_cost` bound a
> single run so one large PR can't drain the shared key; they layer on top of
> OpenRouter's per-key credit limit (the hard cap — they do **not** bound aggregate
> spend across runs). A spend/quota/auth failure or budget trip is now **loud**: a
> `> [!CAUTION]` banner leads the posted comment and a `::error::` annotation is
> emitted, instead of failing open to a green check. **Do not mark this action as a
> required check with `llm_loud_exit` enabled** — a provider outage would then block
> every merge. These inputs are operator config (source from workflow/secrets, never
> PR content). Actions logs are public: exception detail goes to the log, never the
> PR comment.

## How it Works

1.  **Checkout & Setup:** Checks out the PR code with full Git history and installs [uv](https://docs.astral.sh/uv/) (which provisions Python and the dependencies).
2.  **Generate Diff:** Computes the merge base between the PR head and base branches, then generates the diff in a runner-temp artifact outside the PR checkout. The merge base SHA is exported for source-level lookups.
3.  **Analyze Diff:** The `DiffAnalyzer` parses the full diff to extract statistics, sorry tracking (with source-level declaration attribution), declaration changes, and quality signal warnings. Nested block comments are correctly handled.
4.  **Triage Files:** A Triage Agent reviews the file list and filters out noise. For large PRs (more than 50 files), files are classified into high/low priority tiers. Files containing proof-relevant signals are always promoted to high priority.
5.  **Parallel Summarization:** Each high-priority file's diff is summarized concurrently by a Summarizer Agent. Cached summaries from previous runs are reused when the diff hash matches. Large individual file diffs are truncated at hunk boundaries.
6.  **Additional-Instructions Analysis (optional):** If an instructions file is available and the diff is within the analysis size budget, an Additional-Instructions Agent applies those instructions (e.g. a style guide) to the diff concurrently with file summarization. In PR runs the instructions file is loaded from the trusted base ref, never the PR checkout (see the input's trust model above).
7.  **Synthesis:** The Synthesis Agent generates a structured, self-contained overview from per-file summaries, PR title, and body. For very large PRs (more than 40 summaries), uses two-stage synthesis: per-directory groups first, then global. Files triaged out entirely are still noted, so the file count reconciles and nothing is invisible.
8.  **Post Comment:** The final summary (including sorry delta, statistics, declaration changes, quality signals, coverage notes, additional analysis, and per-file summaries) is posted as a PR comment. Declaration and `sorry` listings are grouped by file, sorted deterministically, and capped with an overflow note on very large PRs. The comment body is kept under GitHub's size limit by shedding regenerable content (cache, then per-file summaries) if needed. Previous summary comments are found and updated.

## Features

*   **Any model via OpenRouter:** A single OpenRouter-backed client reaches Claude, Gemini, GPT, and others — the model is selected purely by its OpenRouter slug (e.g. `anthropic/claude-opus-4.8`), with no per-provider code. Structured (schema-validated) output, multimodal input, reasoning effort, and rate-limit/retry backoff are handled uniformly.
*   **Multi-Agent Pipeline:** Employs a pipeline of specialized AI agents (Triage, Summarizer, Synthesizer) that produce a reviewer-oriented overview. The summary is intended as an entry point: it describes the PR's scope, structure, and contents so a reviewer can orient before opening the diff (deep, suggestion-level review is a separate concern). Prompts favor breadth and a self-contained overview over terseness.
*   **Workflow boundary:** Use the summary action for orientation and deployment-supplied style/documentation/proof-progress reporting (via `additional_instructions_path`). The dedicated review action owns semantic correctness, paper fidelity, cross-file contracts, and actionable blocking findings; summary observations should not be treated as correctness verdicts.
*   **Parallel Execution:** Summarizes multiple files concurrently (up to 10 workers), with per-file diff caching to avoid re-summarizing unchanged files across PR updates.
*   **Smart Triage:** Automatically filters out noise (lockfiles, binaries, generated code) to focus the summary on meaningful changes. Files with proof-relevant Lean code signals (`sorry`, `admit`, `native_decide`) are always included regardless of triage decisions; occurrences only inside strings or line comments do not trigger promotion.
*   **Lean-Aware Analysis:**
    *   **Source-level declaration lookup:** Loads full source files (new from disk, old via `git show`) to build declaration indices. New-source paths must resolve to non-symlink regular files inside the checkout, so a PR-created Lean symlink cannot pull runner files into declaration context. Sorry/admit occurrences are attributed to their enclosing declaration even when only the proof body changed — not just when the declaration header appears in the diff.
    *   **Nested block comment awareness:** Uses Lean 4's `/- /- ... -/ -/` nested block comment parser to avoid false positives in sorry/quality signal detection.
    *   **Enclosing-declaration context for the summarizer:** each per-file summarize prompt includes the (multi-line, size-bounded) signatures of the declarations enclosing the changed regions, taken from the post-change source — so a body-only change is described against the right theorem or definition even when its header is nowhere in the diff. The context is treated as untrusted data (fenced, fence-escape-neutralized) and is part of the summary cache key.
    *   **Sorry delta:** Top-level summary shows net proof progress (sorries added vs. removed).
    *   **Declaration tracking:** Reports new, removed, and affected declarations.
    *   **Quality signals:** Warns on `admit`, `native_decide`, debug commands (`#check`/`#eval`), and `set_option autoImplicit true` in added lines.
    *   **Issue linking:** Links affected sorries to open GitHub issues labeled `proof wanted`.
*   **Large-PR Scaling:** For PRs with many files (more than 50), automatically switches to tiered triage (high/low priority) and two-stage synthesis (per-directory then global, above 40 summaries). Individual file diffs exceeding the per-file size budget are truncated at a hunk boundary where possible (otherwise at a line boundary), with a coverage note in the output. The additional-instructions analysis is skipped entirely when the overall diff exceeds its size budget, to avoid misleading partial results.
*   **Per-File Summary Caching:** Caches file summaries in a hidden HTML comment on the PR. On subsequent runs (e.g., `synchronize` events), only files whose diffs changed are re-summarized. Cache is invalidated when the model or prompt template changes, and is pruned each run to the files in the current diff so it cannot accumulate stale entries (e.g. from renamed/removed files) and bloat the comment.
*   **Authenticated cache & comment (S5):** the embedded cache carries an HMAC tag keyed on a secret derived from the OpenRouter API key and bound to the repo + PR number **and the carrying comment's author login**. A cache planted by a commenter (the identifier and format are public, and diff hashes are computable) is rejected — with a distinct log warning — instead of being injected into the summary; a valid blob copied from another PR fails (context-bound); and a valid blob copied *verbatim* from the bot's own comment into an attacker-authored comment on the same PR fails too (author-bound) — closing the same-PR carrier-replay hijack. The MAC is deliberately **not** keyed on the GitHub token (the default `GITHUB_TOKEN` rotates every run, which would silently kill the cache) and does **not** hardcode a comment author (a PAT/App-token deployment posts under a different identity); the author is read from whatever comment is being written/verified, so the scheme stays identity-agnostic. Only a MAC-verified comment is ever **edited**: if none authenticates (a planted identifier, a pre-upgrade comment, or a rotated key), the action creates a fresh comment — binding the new comment's MAC to its own author login by creating it and then editing in the (hidden) cache blob, so the binding holds from the very first run. The cost: after a key rotation (or on the first post-upgrade run) the old bot comment is left behind as a stale duplicate to delete manually, and the cache re-primes on that run.
*   **Optional Additional-Instructions Analysis:** Applies deployment-supplied instructions (via `additional_instructions_path`) to the diff — e.g. a style guide such as `CONTRIBUTING.md`, a progress tracker, or any project-specific guidance. The instructions themselves tell the agent what to produce.
*   **Optional PR Title Validation:** Validates PR titles against conventional commit format (`type[(scope)]: subject`) and uses the parsed type to inform summary structure.
*   **Deterministic PR labels (opt-in):** With `apply_labels: true`, reconciles `sorry-added` / `native_decide` / `axiom-added` labels straight from the deterministic diff signals (no LLM) — cheaper and more trustworthy than prose, and correct across pushes (a label is removed when its signal goes away). The action touches only these three labels and creates them if missing.
*   **Per-run call cap (opt-in):** `max_summary_files` bounds how many files the LLM summarizes individually on one PR; the overflow is listed and still fully covered by the deterministic tracking.
*   **Upstream Path Reminders:** Flags when changed files fall under a configurable path prefix (e.g., `ToMathlib/`) and reminds about upstream PRs.
*   **Token Usage Tracking:** Logs cumulative input and output usage across all API calls, including billed calls that return unusable structured output. Thinking/reasoning tokens are displayed as a subset of output, not added twice to the total.

## Project Structure

```
summary/                   # workspace member of the lean4repo-utils repository
  action.yml               # GitHub Actions composite action definition
  summary.py               # Main summary orchestration (multi-agent pipeline)
  pyproject.toml           # Project metadata + dependencies (uv workspace member)
  prompts/
    triage.md              # Triage agent: file filtering
    triage_tiered.md       # Triage agent: high/low priority classification (>50 files)
    summarize_file.md          # Summarizer agent: per-file summary generation
    additional_instructions.md # Additional-instructions agent: applies deployment-supplied instructions to the diff
    synthesize_summary.md      # Synthesis agent: self-contained overview from per-file summaries
  tests/
    test_summary.py        # Unit tests
```

The OpenRouter LLM client and the Lean 4 comment parser live in the shared
`leanrepo-common` package (`common/src/leanrepo_common/`), and the pinned
`uv.lock` is the workspace lockfile at the repository root.

## Customizing AI Prompts

The behavior of each AI agent is governed by Markdown prompt templates in the `prompts/` directory. Each template uses `{{PLACEHOLDER}}` syntax for dynamic content injection at runtime.

**Prompt files and their placeholders:**

| Prompt | Agent | Placeholders |
|--------|-------|-------------|
| `triage.md` | Triage (normal) | `{{FILE_LIST}}` |
| `triage_tiered.md` | Triage (>50 files) | `{{FILE_LIST}}` |
| `summarize_file.md` | Summarizer | `{{FILE_PATH}}`, `{{FILE_DIFF}}` |
| `additional_instructions.md` | Additional-instructions | `{{INSTRUCTIONS_CONTENT}}`, `{{DIFF_CONTENT}}` |
| `synthesize_summary.md` | Synthesizer | `{{PR_TITLE}}`, `{{PR_BODY}}`, `{{PER_FILE_SUMMARIES}}`, `{{PR_TYPE_HINT}}` |

## Development

### Running Tests

This project uses [uv](https://docs.astral.sh/uv/):

```bash
uv run pytest tests/ -v   # uv resolves the env from uv.lock automatically
uv run ruff check summary.py
```

### Dependencies

Declared in `pyproject.toml` (pinned in the workspace `uv.lock`): `leanrepo-common` (shared LLM provider + Lean utilities; brings in `openai` pointed at OpenRouter), `PyGithub`, `pydantic`.

### CI

The repository's shared CI workflow (`.github/workflows/ci.yml`) runs, on every push and PR:
- `ruff` linting across the whole repository
- the unit-test suite per workspace member (`pytest tests/` for `common`, `sorry-tracker`, `summary`, `review`)
- `action.yml` YAML validation for both actions
- prompt-template existence checks (including this action's templates)
- env-var cross-validation — ensures every env var `summary.py` reads is provided by `action.yml` (the review action gets the same check)

## License

This project is licensed under the [Apache License 2.0](../LICENSE).
