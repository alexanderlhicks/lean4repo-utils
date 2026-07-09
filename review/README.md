# review — AI code review for Lean 4 pull requests

This GitHub Action provides an AI-powered code review for Pull Requests in Lean 4 projects, with a strong focus on detecting misformalization issues. It reaches any LLM through [OpenRouter](https://openrouter.ai) and analyzes code changes against formal specifications and project dependencies through a multi-agent pipeline.

**Fastest start:** add an `OPENROUTER_API_KEY` Actions secret, then copy the [recommended combined workflow](#recommended-combined-workflow-auto--chatops) below to `.github/workflows/ai-review.yml` in your repository. The sections below explain the workflow, every input, and how the pipeline works.

- [Usage](#usage)
  - [Recommended: Combined Workflow (Auto + ChatOps)](#recommended-combined-workflow-auto--chatops)
  - [Alternative: Minimal Push Workflow (No ChatOps)](#alternative-minimal-push-workflow-no-chatops)
  - [Recommended deployment](#recommended-deployment)
  - [Inputs](#inputs)
  - [Advanced tuning (environment variables)](#advanced-tuning-environment-variables)
- [How it Works](#how-it-works)
- [Features](#features)
- [How requests are made](#how-requests-are-made)
- [Project Structure](#project-structure)
- [Customizing AI Prompts](#customizing-ai-prompts)
- [Development](#development)

## Usage

This is a composite action. It supports both automatic review on PR open and on-demand review via `/review` comments, ideally combined in a single workflow.

### Recommended: Combined Workflow (Auto + ChatOps)

Create a workflow file at `.github/workflows/ai-review.yml`:

```yaml
name: PR Review

on:
  pull_request:
    types: [opened]
  issue_comment:
    types: [created]

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: true

jobs:
  review:
    if: >-
      (
        github.event_name == 'pull_request' &&
        (
          github.event.pull_request.author_association == 'OWNER' ||
          github.event.pull_request.author_association == 'MEMBER' ||
          github.event.pull_request.author_association == 'COLLABORATOR'
        )
      ) ||
      (
        github.event_name == 'issue_comment' &&
        github.event.issue.pull_request &&
        startsWith(github.event.comment.body, '/review') &&
        (
          github.event.comment.author_association == 'OWNER' ||
          github.event.comment.author_association == 'MEMBER' ||
          github.event.comment.author_association == 'COLLABORATOR'
        )
      )
    runs-on: ubuntu-latest
    # The budget is mostly for the multi-agent LLM review (many API calls). The
    # Lean build is quick — lean-action fetches the prebuilt Mathlib cache
    # (`lake exe cache get`) rather than compiling Mathlib from source. Very
    # large PRs (many files/dependents) may want 150.
    timeout-minutes: 120
    permissions:
      contents: read
      pull-requests: write
    steps:
      # Everything after `/review` is freeform: plaintext focus instructions,
      # and any URLs or repo paths it mentions become review context automatically.
      - name: Extract instructions from /review comment
        id: get_args
        if: github.event_name == 'issue_comment'
        env:
          COMMENT_BODY: ${{ github.event.comment.body }}
        run: |
          EOF=$(openssl rand -hex 8)
          {
            echo "instructions<<$EOF"
            printf '%s\n' "$COMMENT_BODY" | sed -E '1s|^/review[[:space:]]*||'
            echo "$EOF"
          } >> "$GITHUB_OUTPUT"
        shell: bash

      - uses: alexanderlhicks/lean4repo-utils/review@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.OPENROUTER_API_KEY }}

          # --- Models (open-weight, a two-family combination) ---------------
          # These two are the action's DEFAULTS (shown for clarity — you can omit
          # them). `model` runs the deep agents (spec, per-file review, cross-file,
          # dependent-impact). `verify_model` is a DIFFERENT family so the
          # verification pass is an independent check (less self-agreement bias).
          # Both MUST support tool-calling (for lean_tools) + structured output;
          # confirm current slugs and the per-model "Tool Call Error Rate" at
          # https://openrouter.ai/models.
          model: deepseek/deepseek-v4-pro   # deep agents — top open intelligence, 1M ctx, prompt caching
          verify_model: z-ai/glm-5.2        # verifier — a top model of a DIFFERENT family
          # Optional cost tier — light structural agents on a cheaper open model:
          # triage_model: minimax/minimax-m3
          # synthesis_model: minimax/minimax-m3

          pr_number: ${{ github.event.issue.number || github.event.pull_request.number }}

          # URLs and repo paths mentioned in the /review comment are extracted
          # into external/spec/repo context automatically; the text itself
          # reaches the reviewers as focus instructions.
          additional_comments: "${{ steps.get_args.outputs.instructions }}"

          # --- Behaviour ----------------------------------------------------
          lean_tools: true          # check claims against the real compiler (kills FP typecheck claims)
          verify_findings: true     # adversarial precision pass
          # See the "Recommended deployment" section below to tune spec_refs,
          # escape_hatch_allowlist, and dependent_impact_max by project.
```

**Key features of this workflow:**
- **Concurrency control** prevents duplicate reviews on the same PR.
- **Access control** restricts triggers to owners, members, and collaborators only (prevents abuse on public repos).
- **Combined triggers** handle both auto-review on PR open and on-demand `/review` comments.

**How developers use it:**

Automatic review runs on PR open. For re-review with context, comment `/review` followed by anything (or nothing):

```text
/review Check that the ring homomorphism in Foo/Hom.lean matches Section 4 of
https://arxiv.org/pdf/2301.12345.pdf, especially the commutativity hypothesis.
Project conventions are in docs/spec.md.
```

The text is freeform. From it, the action automatically:
- **fetches URLs** (`https://arxiv.org/...`) as external references (PDFs, HTML, raw text);
- **loads repo paths that exist** (`docs/spec.md`, `references/paper.pdf`, `MyLib/Core/`) — `.pdf`/`.tex` files as specification references, everything else as repository context. Paths must be repo-relative; a bare word is only treated as a path if it contains a `/` or an extension, so ordinary prose is never misread as one;
- **passes the whole text** to the reviewers as focus instructions.

A project-wide knowledge base (a `docs/kb/` of notes, a LaTeX blueprint, spec documents) should instead be configured statically with `spec_refs` in the `with:` block, so every review gets it — the comment is for per-PR extras: the paper being formalized, a file to focus on, or plain instructions.

### Alternative: Minimal Push Workflow (No ChatOps)

```yaml
name: AI Code Review for Lean PRs

on:
  pull_request:
    types: [opened, synchronize]

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  ai_review_lean:
    if: >-
      github.event.pull_request.author_association == 'OWNER' ||
      github.event.pull_request.author_association == 'MEMBER' ||
      github.event.pull_request.author_association == 'COLLABORATOR'
    runs-on: ubuntu-latest
    timeout-minutes: 90
    permissions:
      contents: read
      pull-requests: write

    steps:
      - uses: alexanderlhicks/lean4repo-utils/review@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.OPENROUTER_API_KEY }}
          # model / verify_model default to open-weight deepseek/deepseek-v4-pro + z-ai/glm-5.2; override with any slug.
          pr_number: ${{ github.event.pull_request.number }}
```

### Recommended deployment

The [combined (auto + ChatOps) workflow above](#recommended-combined-workflow-auto--chatops) is ready to use as-is for Mathlib-based Lean 4 projects. Its defaults:

- **A two-family, open-weight model combination** — the deep agents share a strong `model`, and the verification pass runs on a *different* family (`verify_model`), because an independent, different-family verifier catches false positives a same-model one rationalizes away. (A single model everywhere works and is simpler, but forgoes that benefit; optionally, put the light structural agents — `triage_model`/`synthesis_model` — on a cheaper variant.) Any strong open model qualifies as long as it supports **tool-calling** (for `lean_tools`) and **structured output**; confirm the current slug and the per-model *Tool Call Error Rate* at [openrouter.ai/models](https://openrouter.ai/models). Good open-weight picks as of mid-2026 (pick two *different families* for `model` and `verify_model`): `deepseek/deepseek-v4-pro` (deep agents — top open intelligence, 1M context, prompt caching) and `z-ai/glm-5.2` (verifier — a different top model), with `minimax/minimax-m3` or `deepseek/deepseek-v4-flash` for the optional cheap `triage`/`synthesis` tier. Cheaper strong-agentic verifier alternatives: `xiaomi/mimo-v2.5-pro`, `minimax/minimax-m3`. (`moonshotai/kimi-k2.6` is exceptional at tool-call *throughput* but ranks a notch lower on raw intelligence, so it's a weaker choice for the judgement-heavy verifier.) Avoid `*-max`-style tiers that are proprietary — they aren't open-weight. The pipeline runs tool-calling and structured output in **separate phases**, so it avoids the known issue where models given both at once mis-emit tool arguments.
- **`lean_tools: true`** — the reviewer/verifier check claims against the real compiler, so "won't typecheck / lemma doesn't exist" claims are grounded, not guessed. Fails open to a tool-free review if the model can't call tools.
- **Strict escape-hatch verdict** (empty allowlist) by default.

Tune to your project's characteristics:

| If your project… | …then |
|------|-------|
| formalizes results from **papers**, or maintains a **knowledge base / LaTeX blueprint / spec docs** | set `spec_refs` to the knowledge-base/blueprint paths (e.g. `docs/kb,blueprint/src` — drives the formalization checklist), and mention the relevant paper URL or PDF path in the `/review` comment per-PR. |
| has **native FFI backends** that use `implemented_by`/`opaque` intentionally | `escape_hatch_allowlist: implemented_by,opaque` so intentional native code isn't flagged as a defect. (Code using only `@[extern]` needs nothing — it isn't flagged.) |
| **CI enforces zero `sorry`/`axiom`** | keep the allowlist empty — the strict deterministic verdict aligns with that policy. |
| bridges **computable representations to Mathlib** (`RingEquiv`/`AlgEquiv`, etc.) | focus the reviewer (via `additional_comments`) on those equivalences — a vacuous or wrong bridge is the central misformalization risk. |
| has **shared tactics / core definitions** that fan out to many dependent files | raise `dependent_impact_max` so the second-order pass covers the unchanged consumers a change can break. |
| is **large**, with big PRs (many files and dependents to review) | raise `timeout-minutes` — the multi-agent review, not the Lean build, is what grows (`lean-action` fetches the prebuilt Mathlib cache, so Mathlib isn't rebuilt). Chunked review and budget-guards already handle the size. |

**Prerequisites & caveats:**
- **Dependency discovery needs `lake exe graph`** (from `import-graph`, a Mathlib dependency, so it resolves in most Mathlib-based projects — confirm for yours). If missing, discovery and the dependent-impact pass degrade gracefully to changed-files-only.
- **Cost scales with findings and dependents** — verification is one call per verdict-driving finding, dependent-impact one per unchanged consumer (capped by `dependent_impact_max`). Tune the caps if cost matters.
- **Validate on a live PR and tune.** The defaults are reasoned baselines, not gospel — confirm model choice, thinking budget, and caps against a real review before relying on them.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `github_token` | Yes | — | GitHub Token for API calls |
| `api_key` | Yes | — | OpenRouter API key |
| `model` | No | `deepseek/deepseek-v4-pro` | OpenRouter model slug for the deep agents. Defaults to an open-weight model; override with any slug (must support tool-calling + structured output). |
| `pr_number` | Yes | — | The Pull Request number |
| `spec_refs` | No | `""` | Comma-separated **local** paths (files or dirs; PDF/md/txt/tex/lean) to specification / knowledge-base documents. These drive the formalization checklist (Agent A) and ground every reviewer — the project's standing knowledge base belongs here. |
| `additional_comments` | No | `""` | Freeform focus instructions for the reviewers (e.g. everything after `/review` in a PR comment). URLs it mentions are fetched as external references and existing repo-relative paths are loaded as spec/repo context, automatically. |
| `lint` | No | `false` | Whether to run the Lean linter |
| `dependency_depth` | No | `2` | Depth of transitive dependency traversal (1=direct only, 2=imports of imports) |
| `dependent_impact_max` | No | `10` | Max unchanged dependent files to review for breakage caused by the PR (second-order pass). `0` disables it. |
| `verify_findings` | No | `true` | Run the adversarial verification pass that filters likely-false-positive findings before the verdict. `false` to disable. |
| `escape_hatch_allowlist` | No | `""` | Comma-separated escape hatches sanctioned for this repo (e.g. `opaque,axiom`). Still reported, but do not trigger the hard "Changes Requested" verdict when introduced. |
| `enable_web_search` | No | `false` | Enable OpenRouter web-search grounding for agents (adds cost). Set `true` to enable. |
| `lean_tools` | No | `true` | Give the reviewer + verifier Lean toolchain tools (`lean_check` / `lean_print` / `lean_print_axioms` / `lean_typecheck` via `lake env lean`) so they check claims against the real compiler instead of guessing. `false` to disable. Requires a tool-calling-capable model. |
| `thinking_budget` | No | `10240` | Reasoning token budget for deep-analysis agents (Triage and Synthesis use 1/5 of this). Passed to OpenRouter's unified `reasoning.max_tokens`, which maps it onto each provider's native reasoning control (Anthropic thinking budget, OpenAI/Gemini reasoning level). `0` disables reasoning. |
| `spec_model` | No | `model` | Model override for the Specification Analyst agent |
| `triage_model` | No | `model` | Model override for the Triage agent |
| `review_model` | No | `model` | Model override for the per-file Code Reviewer agent |
| `cross_file_model` | No | `model` | Model override for the Cross-File Analysis agent |
| `synthesis_model` | No | `model` | Model override for the Synthesis agent |
| `verify_model` | No | `z-ai/glm-5.2` | Model for the verification pass. Defaults to a **different open-weight family** than `model` for an independent check (avoids self-agreement bias). Override with any slug; keep it a different family than `model`. |
| `llm_max_run_tokens` | No | `""` | Per-run token ceiling. Once exceeded, no further LLM calls are made and the run degrades gracefully (partial results + a loud banner + a non-Approved verdict) instead of draining a shared key. Empty = disabled. The token ceiling is authoritative. |
| `llm_max_run_cost` | No | `""` | Per-run cost ceiling in OpenRouter credits. **Requires** `llm_max_run_tokens` (a cost-only budget is rejected — cost can be BYOK-fee-only or absent). Empty = disabled. |
| `llm_loud_exit` | No | `false` | When `true`, the action exits **non-zero** if the run degraded (spend/quota/auth failure or budget exhausted), *after* posting the comment. Default `false` keeps the job green with a banner + `::error::` annotation. See the warning below. |

> **Per-run spend control (C3).** `llm_max_run_tokens` / `llm_max_run_cost` bound a
> single run so one huge or hostile PR can't drain the shared key; they layer on top
> of OpenRouter's own per-key credit limit (the bug-proof hard cap — they do **not**
> bound aggregate spend across runs). A spend/quota/auth failure or budget trip is now
> **loud**: a `> [!CAUTION]` banner leads the comment, the verdict can no longer be
> *Approved*, and a `::error::` annotation is emitted — the run no longer fails open to
> a green check. **Do not mark this action as a required check with `llm_loud_exit`
> enabled** — a provider outage would then block every merge. These inputs are
> operator config; source them from the workflow/secrets, never from PR content.
>
> **Actions logs are public** on public repos: exception detail (class + status +
> truncated message) goes to the log, never into the PR comment. **Security:** the
> reviewer's `lean_tools` execute model-directed Lean IO in the workspace, and the
> Lean build step runs the PR branch's `lakefile` code. Until full `lean_tools`
> sandboxing lands (tracked separately), do **not** wire this action under
> `pull_request_target` with a privileged token on an untrusted-fork PR.

### Reviews the PR head, and shows what it used

- **Correct code (PR head).** The action resolves the pull request's head commit from
  `pr_number` and checks that exact SHA out, so a `/review` invoked from a comment
  reviews the PR's code — not the base branch. The same commit anchors the diff, file
  discovery, and the line annotations, and is stamped in the posted comment
  (*"Reviewed at commit `…`"*). Note this means the action reviews the **head** commit
  for every trigger.
- **"References & context used."** Each review comment ends with a collapsible manifest
  listing what actually grounded it — external references fetched, knowledge-base /
  specification files loaded, and repository dependency-graph files (context that
  failed to load is reported once, in *Context Warnings*). This makes it visible
  whether the review drew on the intended sources.
- **Guided vs. unguided.** A plain `/review` is an *unguided* review (grounded only on
  the diff, the dependency graph, and any cited references); a `/review` with
  instructions is titled *"AI Review (with additional instructions)"* and lays those
  instructions out, so a reader can judge the review against what was asked.
- **Findings cite their basis.** Each finding carries an `evidence` line — the paper
  section / checklist item it rests on, the repository symbol misused, or the
  compiler/toolchain output — while original, non-citeable findings remain first-class.

### Advanced tuning (environment variables)

| Env var | Default | Effect |
|---------|---------|--------|
| `MAX_FILE_REVIEW_CHARS` | `400000` | A changed file larger than this is reviewed in declaration-aligned sections (map-reduce) and merged. |
| `DEPENDENT_IMPACT_MAX` | `10` | Same as the `dependent_impact_max` input. |
| `VERIFY_FINDINGS` | `true` | Same as the `verify_findings` input. |
| `VERIFY_MODEL` | `review_model` | Same as the `verify_model` input. |
| `LEAN_TOOLS` | `true` | Same as the `lean_tools` input. Auto-disabled if `lake` is not on PATH. |
| `MAX_PROMPT_CHARS` | `2500000` | Per-call assembled-prompt budget; bulk context is trimmed to fit. |
| `LLM_MAX_CONCURRENCY` | `5` | Cap on concurrent in-flight API calls (keep aligned with `--max-workers`). |
| `ESCAPE_HATCH_ALLOWLIST` | `""` | Same as the `escape_hatch_allowlist` input. |
| `LLM_MAX_RUN_TOKENS` | `""` | Same as the `llm_max_run_tokens` input (empty = disabled). |
| `LLM_MAX_RUN_COST` | `""` | Same as the `llm_max_run_cost` input (requires `LLM_MAX_RUN_TOKENS`). |
| `LLM_LOUD_EXIT` | `false` | Same as the `llm_loud_exit` input. Non-zero exit on a degraded run, after the comment posts. |
| `ENABLE_WEB_SEARCH` | `false` | Same as the `enable_web_search` input. |
| `SPEC_REFS` | `""` | Same as the `spec_refs` input. |

## How it Works

1.  **Checkout & Environment Setup:** Fetches full Git history, installs [uv](https://docs.astral.sh/uv/) (which provisions Python), and sets up Lean/Lake via `lean-action`.
2.  **Build:** `lean-action` fetches the prebuilt Mathlib cache (`lake exe cache get`, auto-detected) so Mathlib isn't compiled from source, then builds the project's own files with `lake build` (with optional linting).
3.  **Discover Related Files:** Identifies changed `.lean` files, then uses the Lake dependency graph for BFS-based transitive dependency and dependent discovery. Splits results into full-context and summary-context tiers.
4.  **Extract Lean Toolchain Info:** Runs `#print axioms` per declaration, scans for `sorry`/`admit`, and captures compiler diagnostics for changed files. Performs lightweight sorry/admit scanning on summary-context overflow files. Operates within a configurable time budget (default 300s).
5.  **Run Multi-Agent Review Pipeline:**
    *   **Pre-checks** (deterministic): Scans diffs for escape hatches with nested block comment and string literal awareness.
    *   **Agent A** (spec analysis): Reads external PDFs/papers with repository structure context, produces a formalization checklist.
    *   **Triage**: Groups files into review clusters using dependency graph and type signatures. Produces review strategies and key hypotheses per cluster.
    *   **Agent B** (per-file review, parallel): Writes a step-by-step analysis of each file, then derives findings from the analysis. Receives cluster review strategy, key hypotheses, and type signatures of related files.
    *   **Cross-File Agent**: Traces composition chains, type-flow, and axiom propagation, then reports issues grounded in that analysis.
    *   **Dependent-Impact Agent** (second-order): Re-reviews unchanged depth-1 importers of the changed files for breakage the PR causes; findings fold into the cross-file results.
    *   **Verification Agent** (precision): Independently tries to refute each verdict-driving finding; refuted findings are dropped and disclosed separately.
    *   **Deterministic Verdict**: Computed from mechanical facts, surviving findings, and review coverage — not from the LLM.
    *   **Synthesis**: Aggregates the (verified) structured review data and formatted reviews into an executive summary.
6.  **Post Review:** Publishes or updates an AI review comment on the PR, with collapsible per-file details grouped by cluster (overflowing into follow-up comments if it exceeds GitHub's size limit). Line-level annotations are posted as GitHub Review comments where findings map to diff lines.

## Features

*   **5-Agent Review Pipeline:**
    1.  **Mechanical Pre-Checks:** Deterministic scanning for escape hatches (`sorry`, `axiom`, `native_decide`, `opaque`, `implemented_by`, `sorryAx`) in both newly introduced and pre-existing code, with comment- and string-awareness (including Lean 4 nested block comments).
    2.  **Specification Analyst (Agent A):** Reads external PDFs and math papers (OpenRouter parses PDFs via its file-parser plugin, with native multimodal where the model supports it) to extract a "Formalization Checklist" — a mapping from paper results to mathematical content that any correct formalization must preserve. Receives repository structure and dependency graph for awareness of existing formalizations.
    3.  **Triage Agent:** Groups changed files into review clusters based on the dependency graph and type signatures, prioritizing tightly-coupled files for joint review. Produces a **review strategy** and **key hypotheses** per cluster that guide the per-file reviewers.
    4.  **Code Reviewer (Agent B):** Evaluates each Lean file's diff and full content against the spec checklist, repository context, and Lean 4 best practices. Writes a structured **analysis** before producing findings (what the code does mathematically, risk assessment, spec mapping). Runs in parallel across files (up to 5 concurrent workers) with cluster-level type signatures, review strategy, and key hypotheses.
    5.  **Cross-File Analysis Agent:** Analyzes composition chains, type-flow across files, axiom/escape-hatch impact propagation, and external dependency correctness.
    6.  **Lead Synthesizer (Agent C):** Aggregates per-file reviews (both formatted and structured data) into a prioritized executive summary with deduplication.
*   **Transitive Dependency Discovery:** Uses `lake exe graph --json` with BFS traversal (configurable depth, default 2) to find both direct and transitive dependencies. Asymmetric depth: dependencies (what we import) go to depth 2; dependents (what imports us) stay at depth 1.
*   **Lean Toolchain Extraction:** Post-build extraction of axiom dependencies (`#print axioms`), `sorry`/`admit` locations, and compiler diagnostics for all changed files, plus lightweight sorry/admit scanning for overflow files.
*   **Tiered Context Management:** Full file content for up to 50 files (configurable via `CONTEXT_LIMIT`), with type-signature-only summaries for overflow files. Depth-1 dependencies are prioritized over depth-2 in the full-context tier, and the cap is enforced against the *total* tier size so very-large PRs gracefully demote excess files to summaries rather than blowing the prompt budget. Per-file reviewers additionally filter out sibling changed files from `REPO_CONTEXT` (each changed file gets its own review pass; cross-file coupling is surfaced by the dedicated cross-file analyzer and the triage-produced cluster signatures). A per-call character budget (`MAX_PROMPT_CHARS`, default ~2.5M chars ≈ 830K tokens) defensively trims dependency content if an individual review's assembled prompt would still exceed the model's context limit.
*   **Context Completeness Guarantees:** External reference fetch errors and Lean toolchain extraction failures are surfaced as warnings in the review output.
*   **Prompt caching:** The stable shared prefix — the operating contract, external reference documents, and (for per-file review) the formalization checklist, repository context, best-practices checklist, and verdict rules — is sent as a `cache_control`-marked block so it is reused across every agent call, while the volatile per-file content (diff, full content) trails after the cache breakpoint. This is a prefix match, so caching holds across the many per-file/chunk review calls of a run.
*   **Agent Deliberation:** Agents think before reviewing. Extended thinking (configurable token budget) gives the model internal reasoning time. FileReview and CrossFileAnalysis schemas include an `analysis` field where the model writes its step-by-step reasoning before committing to findings. Triage produces per-cluster review strategies and testable hypotheses that flow to the per-file reviewers.
*   **Any model via OpenRouter:** A single OpenRouter-backed client reaches Claude, Gemini, GPT, and open-weight models alike — the model is selected purely by its OpenRouter slug (e.g. `deepseek/deepseek-v4-pro`), with no per-provider code. Schema-validated structured output, PDF/multimodal input, prompt caching, and reasoning effort are all requested through OpenRouter's unified parameters.
*   **Structured Output:** All agents produce Pydantic-validated JSON responses. Line-level annotations are posted via the GitHub Review API using the modern `line`/`side` parameters.
*   **Per-Agent Model Selection:** Each pipeline stage can use a different model via CLI flags (`--spec-model`, `--review-model`, `--cross-file-model`, `--synthesis-model`).
*   **Adaptive Pipeline:** Single-file PRs skip triage, cross-file analysis, and synthesis (the per-file review is the output, with a deterministic downstream impact note from the dependency graph). Two-file PRs skip triage but get cross-file analysis.
*   **Deterministic Verdict:** The overall verdict is computed by the pipeline from mechanical facts (introduced escape hatches, honoring `escape_hatch_allowlist`), the structured finding counts, cross-file issues, and review-coverage — not taken from the LLM. The comment leads with the verdict and an explicit *basis*. A file that could not be fully reviewed is a coverage gap that can never be certified as "Approved".
*   **Scales to Large PRs (map-reduce):** A changed file larger than `MAX_FILE_REVIEW_CHARS` (default ~400K chars) is reviewed in declaration-aligned sections and merged, so tens-of-thousands-of-line files are covered in full instead of failing the budget. Truncated structured outputs are retried with a larger token cap rather than lost. Cross-file and synthesis prompts are budget-guarded, and reviews that exceed GitHub's comment limit are split across multiple comments (nothing is truncated away).
*   **Grounded, Legible Findings:** Every finding carries an `evidence` field (the paper section, repository symbol, or toolchain output it rests on) and a `confidence` level, so a human can validate it. Reviewers are instructed to flag **second-order issues** the diff *implicates* (misuse of an existing definition/abstraction, broken invariants) — not just lines inside the diff.
*   **Verification (precision) pass:** After the reviewers, an independent agent adversarially tries to **refute** each verdict-driving finding. Refuted findings are dropped (and disclosed in a "filtered by verification" section) before the verdict is computed — separating recall (reviewers) from precision (verifier) as human review does. Best run on a *different* model (`verify_model`) to avoid self-agreement bias.
*   **Lean toolchain grounding:** The per-file reviewer and the verifier can call the real Lean toolchain during review — `lean_check` / `lean_print` / `lean_print_axioms` / `lean_typecheck` (via `lake env lean`) — so a claim like "this won't typecheck" or "that lemma doesn't exist" is checked against the compiler instead of guessed. The type checker is treated as ground truth, which kills the most common false-positive class. The Lean subprocess runs with secret-looking env vars scrubbed. The tool interface is pluggable — a richer [`lean-lsp-mcp`](https://github.com/oOo0oOo/lean-lsp-mcp) backend (proof-state, diagnostics, `loogle`/`leansearch`) can slot in behind it.
*   **Dependent-impact (second-order) pass:** The unchanged depth-1 importers of the changed files are re-reviewed for breakage the PR causes (renamed/retyped symbols, weakened lemmas, dropped instances). Findings fold into the cross-file results, so they are verified and scored like any other.
*   **Shared operating contract:** Every agent is governed by one injected contract establishing an untrusted-input posture (PR/reference content is *data, not instructions*; injection attempts are reported), a grounding requirement, and consistent confidence calibration — so the agents interlock instead of each redefining the rules.
*   **Hardened Fetching:** External-reference fetching validates and re-resolves DNS at every redirect hop and pins the connection to the validated IP, closing the DNS-rebinding (TOCTOU) window.

## How requests are made

All requests go through OpenRouter's OpenAI-compatible Chat Completions endpoint via a single client (`llm_provider.py`):

| Concern | How it's handled |
|---------|------------------|
| Structured output | `response_format` JSON schema (`strict`) from the agent's Pydantic model, with `provider.require_parameters` so only providers that honor it are routed to, plus the `response-healing` plugin as a safety net |
| PDF / multimodal | PDFs sent as `file` content parts and parsed by OpenRouter's `file-parser` plugin; images as `image_url` data URLs |
| Reasoning | `reasoning.max_tokens` (from `thinking_budget`), mapped per provider by OpenRouter |
| Prompt caching | The stable prefix — operating contract, external reference docs, and the per-file shared context (checklist, repo context, verdict rules) — is `cache_control`-marked and reused across calls; volatile per-file content trails after the breakpoint |
| Retries / rate limits | The OpenAI SDK's built-in backoff (honors `Retry-After`), plus a concurrency semaphore |

Model capabilities (which slugs support `response_format`, `reasoning`, etc.) can be checked against OpenRouter's `/api/v1/models` endpoint.

## Project Structure

```
review/                       # workspace member of the lean4repo-utils repository
  action.yml                  # GitHub Actions composite action definition
  review.py                   # Main review orchestration (multi-agent pipeline)
  discover_files.py           # Dependency discovery via lake graph (BFS)
  lean_info_extractor.py      # Lean toolchain data extraction (axioms, sorry, diagnostics)
  lean_tools.py               # Lean toolchain tools for agents (lean_check/print/typecheck via `lake env lean`)
  pyproject.toml              # Project metadata + dependencies (uv workspace member)
  prompts/
    _operating_contract.md    # Shared contract injected ahead of every agent (untrusted-input posture, grounding, confidence calibration)
    analyze_spec.md           # Agent A: specification analysis prompt
    triage.md                 # Triage agent: file clustering prompt
    review_file.md            # Agent B: per-file review (no spec)
    review_code_with_spec.md  # Agent B: per-file review (with spec checklist)
    cross_file_analysis.md    # Cross-file analysis prompt
    dependent_impact.md       # Second-order: breakage in unchanged dependents
    verify_finding.md         # Verification agent: adversarial refutation of a finding
    synthesize_summary.md     # Synthesis agent: executive summary prompt
    lean4_checklist.md        # Lean 4 best practices checklist (injected into Agent B)
    verdict_rules.md          # Hard verdict rules (injected into Agent B and Synthesis)
  tests/
    test_review.py
    test_discover_files.py
    test_lean_info_extractor.py
    test_lean_tools.py
```

The OpenRouter LLM client (`llm_provider`) and the shared Lean source
utilities (`lean_utils`: module names, comment parsing, file cache) live in
the `leanrepo-common` package (`common/src/leanrepo_common/`), and the pinned
`uv.lock` is the workspace lockfile at the repository root.

## Customizing AI Prompts

The intelligence and behavior of the AI reviewer are governed by Markdown prompt templates in the `prompts/` directory. Each template uses `{{PLACEHOLDER}}` syntax for dynamic content injection at runtime.

### Key prompt files and their placeholders:

**`analyze_spec.md`** (Agent A — Specification Analyst):
`{{EXTERNAL_CONTEXT}}`, `{{FILE_DIFFS}}`, `{{REPO_STRUCTURE}}`, `{{DEPENDENCY_GRAPH}}`

**`triage.md`** (Triage Agent):
`{{DEPENDENCY_GRAPH}}`, `{{ALL_DIFFS}}`, `{{CHANGED_FILE_SIGNATURES}}`, `{{SPEC_CHECKLIST}}`, `{{ADDITIONAL_COMMENTS}}`

**`review_file.md`** / **`review_code_with_spec.md`** (Agent B — Code Reviewer):
`{{REPO_CONTEXT}}`, `{{FILE_PATH}}`, `{{FULL_CONTENT}}`, `{{FILE_DIFF}}`, `{{SPEC_CHECKLIST}}`, `{{ADDITIONAL_COMMENTS}}`, `{{CLUSTER_CONTEXT}}`, `{{LEAN4_CHECKLIST}}`, `{{VERDICT_RULES}}`

**`cross_file_analysis.md`** (Cross-File Agent):
`{{SPEC_CHECKLIST}}`, `{{PRE_CHECK_FINDINGS}}`, `{{ALL_DIFFS}}`, `{{ALL_CHANGED_CONTENTS}}`, `{{DEPENDENCY_CONTEXT}}`, `{{ADDITIONAL_COMMENTS}}`

**`synthesize_summary.md`** (Synthesis Agent):
`{{SPEC_CHECKLIST}}`, `{{PRE_CHECK_FINDINGS}}`, `{{CROSS_FILE_ANALYSIS}}`, `{{PER_FILE_REVIEWS}}`, `{{STRUCTURED_REVIEWS}}`, `{{VERDICT_RULES}}`

**`lean4_checklist.md`** and **`verdict_rules.md`** are static content injected into Agent B and Synthesis prompts. They contain the Lean 4 best practices checklist and hard verdict rules respectively.

## Development

### Running Tests

This project uses [uv](https://docs.astral.sh/uv/):

```bash
uv run pytest tests/ -v   # uv resolves the env from uv.lock automatically
uv run ruff check review.py
```

### Dependencies

Declared in `pyproject.toml` (pinned in the workspace `uv.lock`): `leanrepo-common` (shared LLM provider + Lean utilities; brings in `openai` pointed at OpenRouter), `requests`, `beautifulsoup4`, `pydantic`.

Contributions are welcome. Please ensure changes pass the existing test suite.

## License

This project is licensed under the [Apache License 2.0](../LICENSE).
