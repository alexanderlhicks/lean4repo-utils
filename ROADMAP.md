# lean4repo-utils — action plan & roadmap

Plan of record for improving the three existing utilities and growing the set.
Derived from (1) a per-package code review, (2) a survey of how a real large Lean
repo (ArkLib) is actually managed, and (3) a deep-research pass whose claims were
adversarially verified against primary sources. It is organised **by need**; each
work item is scoped for a self-contained session (tightly-coupled items may share
one; a few large items split) — see [`SESSIONS.md`](SESSIONS.md).

> Status legend: `TODO` · `WIP` · `DONE` · `PARKED`. Update the status field on
> each item as sessions complete. Keep the traceability appendix in sync.
>
> **Execution:** work is split into self-contained, dependency-ordered sessions in
> [`SESSIONS.md`](SESSIONS.md), which also defines the mandatory per-session
> protocol (adversarial prior-work review → plan review → execute → post-review +
> docs refresh) and the reuse/license/attribution policy. Start there.

---

## Guiding principles (the "why" behind the sequencing)

1. **Deterministic/kernel-checkable checks are gates; LLM output is advisory.**
   Independent benchmarks put LLM code review at ~19% best F1, <10% precision for
   most techniques, ~27% recall, and 64–69% correctness-classification accuracy
   ([SWR-Bench 2509.01494](https://arxiv.org/html/2509.01494v1),
   [CR-Bench 2603.11078](https://arxiv.org/html/2603.11078v1),
   [2505.20206](https://arxiv.org/abs/2505.20206)). But in Lean the **kernel is
   ground truth**, so the deterministic parts of these tools are trustworthy in a
   way the LLM parts are not. Build gates on the deterministic layer; keep the LLM
   layer as advisory triage for a human.
2. **Security is the unlock, not a chore.** The review bot is currently gated to
   trusted authors, so the actual pain — outside PRs — is uncovered. The security
   work (M1) is the prerequisite to safely dropping that gate. A near-identical
   real-world bug reached CVSS 9.1
   ([CVE-2025-47928](https://github.com/spotipy-dev/spotipy/security/advisories/GHSA-h25v-8c87-rvm8)).
3. **Build on prior art; don't reinvent.** `#print axioms`/`sorryAx`,
   [`lean4checker`](https://github.com/leanprover/lean4checker),
   [`leanblueprint`](https://github.com/PatrickMassot/leanblueprint),
   [`SorryDB`](https://github.com/SorryDB/SorryDB), and Mathlib's governance
   patterns ([Growing Mathlib 2508.21593](https://arxiv.org/pdf/2508.21593))
   already solve pieces of this.
4. **Foundation before features.** `common/` is shared by everything; its
   correctness and cost bugs poison every tool, so it goes first.
5. **Measure, don't guess.** Stand up a Lean-specific eval harness so we can tell
   whether the AI tools actually reduce load, rather than assuming it.
6. **Wrap, don't rebuild; generic core + config for the specifics.** Where the
   community maintains something (`lean-action`, `mathlib-update-action`,
   `leanblueprint`, `lean4checker`, SorryDB), integrate/document it rather than
   clone it. Keep the core generic and push repo-specific policy (citation/KB
   rules, audit matrices, warning budgets, path→area maps) into an optional,
   config-driven layer — one config file in the target repo that every tool reads.
7. **Consolidate what's already copy-pasted across the fleet.** A survey of six
   Lean repos (ArkLib, VCV-io, evm-asm, mathlib4, CompPoly, cslib) found the same
   tooling reimplemented 4–6× and drifting: build-timing report, import/umbrella
   freshness, style/whitespace lint, warning budget, docs-integrity, release-tag,
   toolchain bump, the LLM summary/review actions, AGENTS.md+wiki contract. Most
   new milestones are **generalize-an-existing-script**, not greenfield — the best
   version already exists in one repo; the job is to hoist it here, parameterize
   it, and roll it back out. See Appendix C for the cross-repo capability matrix.

---

## Vision — a one-stop shop for Lean 4 repo management

The goal is for this repo to be the single place a Lean 4 maintainer goes for
workflows and utilities across the **whole contribution lifecycle**, at a scale
where public PR volume exceeds a tiny team's review capacity. "One-stop shop"
requires two things beyond a collection of tools:

- **Breadth** — cover author → CI gates → review → merge/release → maintenance →
  docs, not just review/summary/sorries (see coverage map below; gaps become M6).
- **A platform spine** — discoverability, **one-command adoption**, shared config,
  and consistent packaging, so the repo reads as one product (M7). This is what
  turns "several repos to manage" into one bootstrap command per repo.

### Lifecycle coverage map

| Stage | Covered / planned | Gaps (→ milestone) |
| --- | --- | --- |
| Author / contribute | PR-readiness bot (M4 P2) | title-convention lint, naming/docstring lint, scaffolders (M6) |
| CI quality gates | axiom-diff, sorry-diff, cheating-tactic, statement-weakening (M3) | import/umbrella freshness, warning budget, build-timing regression, whitespace/style, docs-integrity (M6) |
| Review | `review/`, `summary/`, routing (M4), eval (E) | — |
| Merge / release | merge-queue (M4 P3) | release-tag on toolchain change, toolchain/mathlib bump (wrap), changelog (M6) |
| Maintain / debt | `sorry-tracker/`, trend (V2), blueprint sync (V1), cross-repo dashboard (V3) | dependency-graph analysis, dead/unused-decl finder, deprecation tracking (M6) |
| Docs / knowledge | — | blueprint + doc-gen build/deploy workflow (M6); KB/citations = repo-specific, config-driven |
| Platform / adoption | README table | catalog, bootstrapper, shared config schema, docs site, packaging conventions, toolkit versioning (M7) |

## Roadmap at a glance

| Milestone | Need | Depends on | Priority |
| --- | --- | --- | --- |
| **M0 — Foundation** | `common/` is correct, its cost telemetry is real, spend is bounded | — | P0 |
| **M1 — Security hardening** | Safe to run on untrusted outside PRs (the unlock) | M0 (C6) | P0 |
| **M2 — Fix existing tools' core defects** | review at the right altitude; sorry-tracker lifecycle; summary quality | M0 | P1 |
| **M3 — Deterministic soundness gates** (new `soundness/`) | Catch unsound contributions mechanically | M0 (C5) | P1 |
| **M4 — Triage, routing & shift-left** (new) | Cut human triage load; bounce mechanical failures to authors | M3 | P2 |
| **M5 — Visibility & dashboards** (new) | See proof/soundness state across one and many repos | M3 | P2 |
| **M6 — Full CI-gate & maintenance coverage** (new) | Close the lifecycle gaps (author, CI, release, maintain, docs) | M0, M7 (spine) | P2 |
| **M7 — Platform & adoption** (new) | Catalog + one-command bootstrap + shared config make it one product | M0 (some items anytime) | P1 spine, P2 rest |
| **E — Evaluation harness** (cross-cutting) | Know whether the AI layer helps | M0 | P1, ongoing |

Suggested execution order (mapped to the `SESSIONS.md` waves): **M0 → M1 → M7 spine
(X1/X2/X4) → M2 → M3 → M4 → M5 → M6 → M7 rest; E throughout.** M3 depends on the
shared matcher (C5). The M7 spine (catalog, config schema, packaging convention)
starts early and grows with each new capability — every M2–M6 item registers itself
in the catalog and reads the shared config, so adoption stays a single command.

---

## M0 — Foundation (`common/`)

Everything imports `common`, so its bugs are everyone's bugs. Land this first.

> **LLM reference (applies to all LLM-touching work, here and elsewhere):** verify
> any change to the OpenRouter layer — cost/usage accounting, timeouts, prompt
> caching, model slugs, streaming, tool-calling, structured output — against the
> OpenRouter API docs, not from memory:
> <https://openrouter.ai/docs/api/reference/overview>.

### C1 · Make cost accounting real  — `DONE` · effort S · P0
- **Result (2026-07-07):** cost round-trips end-to-end — the live
  `OPENROUTER_API_KEY`-gated test observed **$0.000124** on `deepseek/deepseek-v4-pro`
  (a real non-zero cost through `TokenUsage.cost`). `cost_missing` fails closed on any
  unusable cost figure. README `text=`→`data=` fixed.
- **Need:** every cost figure in every tool currently reads `0.0`; no cost control
  can work until this is fixed.
- **Actions:** ~~set `extra_body["usage"] = {"include": true}`~~ — per the OpenRouter
  usage-accounting docs (fetched 2026-07-06) that flag is now **deprecated / a no-op**;
  `cost` is returned automatically. The real fix is to *read it through*: `_usage_from`
  now maps the response `cost` field to `TokenUsage.cost` (the flag is still set for
  literal forward-compat). Added a `cost_missing` fail-closed flag so C3 can tell
  "genuinely $0" from "cost omitted." Proven by a round-trip test through the real
  SDK `CompletionUsage` model **plus** an `OPENROUTER_API_KEY`-gated live test (no
  mock can validate production cost since the flag is a no-op).
- **Files:** `common/src/leanrepo_common/llm_provider.py` (`_build_extra_body`,
  `_usage_from`, `TokenUsage`), `common/README.md`.
- **Acceptance:** round-trip test shows `TokenUsage.cost > 0` ✓ (mocked SDK
  round-trip + live test = $0.000124); README `ContentPart(text=...)` corrected to
  `data=` ✓.
- **Ref:** OpenRouter usage accounting (cost returned automatically; `usage:{include}`
  deprecated) — <https://openrouter.ai/docs/api/reference/overview>.
- **Blocks:** C3.

### C2 · Per-request timeout + configurable concurrency  — `DONE` · effort S · P0
- **Result:** `create_provider`/`__init__` now take `timeout` (default 180 s,
  finite/positive-validated, overridable) and `max_concurrency`. Default
  `max_retries` lowered 4→2 so the worst-case slot-hold ≈ `timeout*(max_retries+1)`
  ≈ 9 min (a *rough* bound: it excludes SDK backoff / Retry-After sleeps, which are
  also held, and httpx applies `timeout` per-read not per-whole-request).
  Concurrency is resolved at construction, not import: a shared process-wide default
  sized from `LLM_MAX_CONCURRENCY` (preserving the global cap) with explicit
  overrides getting their own semaphore; both clamped to `[1, 32]`. Non-positive
  timeout falls back to the default. Behavioral + wiring + clamp tests cover it.
- **Need:** a stuck upstream can hold a concurrency slot ~10 min and starve the pool.
- **Actions:** pass a configurable `timeout=` to the `OpenAI(...)` client; expose
  `timeout` and `max_concurrency` through `create_provider` instead of the
  import-time module global.
- **Files:** `llm_provider.py:61`, `:119`, `:158`.
- **Acceptance:** timeout is set by default and overridable; concurrency no longer
  a module global; tests cover both.

### C3 · Per-run cost/token ceiling + graceful degradation + usage accounting  — `WIP` · effort M · P0
> **Progress (2026-07-08):** plan **re-gated** (full session-runner PLAN, GO on the
> *corrected* plan — the draft's `summary.py` R3 site list was inverted, R3 re-raises
> needed a top-level catch, `LLM_LOUD_EXIT` needed `sys.exit(main())` + a heredoc
> rc-capture in `review/action.yml`, and review-side `::error::` must come from the
> shell step). Common-layer **[1/2] committed** (`d64432f`): `RunBudget`,
> `BudgetExceededError`, `is_hard_llm_failure`, `_reraise_if_fatal`, `TokenUsage.byok`,
> the single `_record_usage` sink, fresh-entry raise, mid-loop break, mid-raise usage
> survival, length-retry budget guard — 28 tests, 159 pass/1 live-skip, ruff clean.
> **Remaining [2/2]:** tool-layer wiring (R3/containment/loud-on-402/R6 leak sweep in
> `review.py`+`summary.py`, `action.yml` plumbing + security hardenings), STEP 9 live
> DoD tests (need `OPENROUTER_API_KEY`), docs, REVIEW gate, close-out. BYOK deferred
> item **resolved** (fee-only `cost`; `is_byok`/`cost_details.upstream_inference_cost`).
- **Rescoped (2026-07-07):** the *hard* account/per-key spend cap belongs on
  OpenRouter (per-key credit limits + account balance are server-side and
  bug-proof — verified: they return `402` when depleted), so we **wrap, don't
  rebuild** (principle #6). What OpenRouter cannot do is bound a *single run*: one
  huge/hostile PR can burn a shared key's whole budget in one invocation. So C3 is
  the **per-run** control layered on top, not an app-level global ledger.
- **Need:** (1) a single PR/run must not be able to consume unbounded spend/tokens
  and starve other PRs sharing a key; (2) usage must not be silently dropped when a
  tool round errors; (3) a spend/quota failure must be *visible*, not swallowed
  into a green check.
- **Actions:**
  - Add a **per-run cost+token ceiling** (built on `TokenUsage.cost`/`cost_missing`
    from C1) that stops further LLM calls when exceeded and returns partial results
    with a "budget exceeded" marker rather than hard-failing mid-run. Treat
    `cost_missing` conservatively (a run of unknown-cost calls must be boundable by
    the token ceiling even when cost can't be summed).
  - **Accumulate `_gather_with_tools` usage incrementally** so usage (incl.
    `cost_missing`) from completed tool rounds survives a later-round exception
    (today it is lost — see the thorough-review finding on `_gather_with_tools`).
  - **Surface hard LLM failures loudly** (esp. `402`/auth/quota): the
    summary/review actions currently fail open and exit `0`, hiding a total AI
    outage behind a green check (observed in the evm-asm fleet, 2026-07-07). Emit a
    non-silent signal (annotation / non-green status / explicit comment).
  - **Document** OpenRouter per-key credit limits as the recommended hard account
    cap (and, for the fleet's BYOK setup, verify how `cost` reports under BYOK —
    it may be fee-only or absent, which interacts with `cost_missing`).
- **Files:** `common/src/leanrepo_common/llm_provider.py` (`_gather_with_tools`,
  generation entry points); the `summary`/`review` call sites for the loud-failure
  surfacing.
- **Acceptance:** test proves the per-run ceiling trips and degrades gracefully;
  test proves usage from completed tool rounds is counted even when a subsequent
  round raises; test proves a 402/quota failure is surfaced (not swallowed).
- **Depends on:** C1 — **DONE** (cost signal landed + live-verified $0.000124,
  merged to `main` in PR #1 alongside C2).

#### Gate-1 APPROVED (2026-07-07) — execute in a fresh session

Plan gate passed (session-runner PLAN mode); maintainer approved **full scope in
one session**, on working branch **`session-2-c3`** (off `main` @ 4ef9174; the
lean product PR excludes the planning docs + runner). Execution checklist lives in
the tracked tasks; the resolved adversarial findings that MUST shape the code:

- **R1 — loud ≠ exit code.** Default loud signal = a posted banner + a
  non-Approved verdict + a GH `::error::` annotation (job stays green). Non-zero
  exit only behind an explicit opt-in env (default **off**); document "do not set
  as a required check if the loud-exit is enabled." No `sys.exit()` inside a
  `finally` (it masks tracebacks and, in `review/action.yml`, kills the comment).
- **R2 — record usage on EVERY completion via ONE authoritative sink.** The draft
  recorded only inside `_gather_with_tools`, so `summary`'s tool-less
  `generate_structured` never accumulated and the ceiling could never trip. Record
  `self._usage_from(completion)` after each `create`/`_parse` on ALL paths (each
  tool round, the phase-2 structured parse, `generate_text`); derive the returned
  per-call `TokenUsage` from the same values — never re-record the aggregate.
- **R3 — invert the fail-open default.** Add `_reraise_if_fatal(e)` (re-raises
  `BudgetExceededError` and `is_hard_llm_failure(e)`) as the FIRST line of every
  LLM-touching broad `except Exception` in `review.py` (triage/spec/cross-file/
  dependent/synthesis/verify/per-file) and `summary.py` (triage/sub-synthesis/
  per-file/synthesis). EXCLUDE comment/annotation-write and URL/doc-read sites.
- **R4 — one graceful contract.** Budget trip mid-tool-loop → break + exactly ONE
  final phase-2 answer from gathered evidence (bounded overshoot); only a
  fresh-entry-already-over budget raises `BudgetExceededError` before any call.
- **R5 — gate on FRESH success.** The loud-failure trigger counts fresh (non-cache,
  non-fallback) successful generations vs hard-LLM failures — a cache hit must not
  suppress the banner in an all-402 run.
- **R6 — no exception bodies in public output.** Drop `{exc}`/`{e}` interpolation
  from PR-visible comments (`summary.py:~1249`, `review.py:~2245`); generic label
  in the comment, full detail server-side only.
- **R7 — wire it, don't ship it dark.** Add `LLM_MAX_RUN_COST`/`LLM_MAX_RUN_TOKENS`
  (+ the loud-exit opt-in) as inputs to BOTH `summary/action.yml` and
  `review/action.yml`, sourced from workflow/secrets ONLY (never the PR checkout);
  add `if: always()` to review's Post-Review step so the banner posts on a
  non-green run.
- **R8 — token ceiling is authoritative.** Reject a cost-only budget (`max_cost`
  without `max_tokens`) at construction; the token cap is the trustworthy bound
  (BYOK `cost` may be fee-only/absent). `cost_missing` sets a sticky
  `cost_unreliable`; warn if it flips under an active cost cap.
- **R9 — no Approve on total outage.** Set `review_incomplete` at the ORCHESTRATION
  level on any hard-LLM failure / empty-due-to-failure, so `compute_deterministic_verdict`
  cannot Approve an all-402 run.

New primitives in `common`: `RunBudget` (thread-safe, token-authoritative,
`cost_reliable`, rejects cost-only), `BudgetExceededError(.usage)`,
`is_hard_llm_failure(exc)` (401/402/403 or Auth/PermissionDenied or a
credit/quota/payment message substring; 429/timeout/5xx are NOT hard). Seed
`_bare_provider` with a disabled budget. Docs (STEP 10): key limits = hard cap,
BYOK cost caveat, and the honest soft-bound note (the ceiling caps extra *rounds*,
not ~`max_workers` in-flight maximal calls). Full step-by-step + test list is in
the PLAN-gate output; re-run the runner PLAN mode if that context is lost.

### C4 · Lean source-scanning correctness  — `TODO` · effort M · P1
- **Need:** silent false negatives/positives in the shared scanners corrupt every
  tool's sorry/decl analysis.
- **Actions:** make `is_in_comment` string-aware (or route all detection through
  `scrub_line`); `file_path_to_module_name` → strip only the `.lean` suffix
  (`removesuffix`); `FileCache.read` → `errors="replace"` / `utf-8-sig`; fix
  `strip_comments` escaped-backslash handling to match `scrub_line`.
- **Files:** `lean_utils.py:14-47`, `:206`, `:280`, `:73`.
- **Acceptance:** regression tests for `/- ` inside a string, a lowercase `lean/`
  path component, a non-UTF-8 file, and `"x\\" -- sorry`.

### C5 · Shared canonical sorry/axiom/escape-hatch matcher  — `TODO` · effort M · P0
- **Need:** the correctness-critical matcher is currently reimplemented in each
  tool; review even uses a weaker string scanner. New gates (M3) need one source
  of truth.
- **Actions:** add a word-boundary matcher over `scrub_line` output that classifies
  `sorry`/`admit`/`sorryAx`/`native_decide`/`decide`/`opaque`/`@[extern]`/
  `implemented_by`/`axiom`; expose a stable API. Migrate summary, review
  (`scan_escape_hatches`), and sorry-tracker to it.
- **Files:** new API in `lean_utils.py`; callers in all three tools.
- **Acceptance:** one matcher, three callers; existing tool tests still pass;
  matcher has its own edge-case suite (identifiers like `sorryAx`, multi-line
  strings, docstrings).
- **Blocks:** M3 (G1–G4), M2/R5.

### C6 · Injection-resistant content wrapper (+ optional response cache)  — `TODO` · effort S · P1
- **Need:** M1 needs a shared way to fence untrusted text; identical generations
  are re-run.
- **Actions:** add a helper that wraps untrusted spans in a per-run random nonce
  fence and emits the paired system instruction. Optionally: response cache keyed
  on `(model, content-hash, schema)`.
- **Files:** new helper in `common`.
- **Acceptance:** helper used by M1/S3; documented as defense-in-depth only.
- **Ref:** OpenRouter prompt caching / `cache_control` —
  <https://openrouter.ai/docs/api/reference/overview>.

---

## M1 — Security hardening (the outside-PR unlock)

Do **S1 and S2 together** — S1 alone re-opens the injection surface that the
checkout bug currently masks.

### S1 · [BLOCKER] Fix the chatops checkout ref  — `TODO` · effort S · P0
- **Need:** on the `/review` path the action checks out the **base branch**, so
  every full-file read / escape-hatch scan / Lean query runs on the wrong code —
  silently wrong, not failing.
- **Actions:** check out `refs/pull/<pr_number>/head` (or the PR head SHA)
  explicitly in the composite action.
- **Files:** `review/action.yml:81-84`.
- **Acceptance:** a `/review` run demonstrably reads PR-head content; add an
  action-wiring assertion/test (action.yml is currently untested).

### S2 · Two-stage privilege split for untrusted builds  — `TODO` · effort L · P0
- **Need:** the review action builds/elaborates attacker code with `GH_TOKEN` in
  the environment (`#eval IO.getEnv "GH_TOKEN"` exfiltrates it). Matches
  CVE-2025-47928 (CVSS 9.1).
- **Actions:** split into (1) an **untrusted, secret-free, network-restricted**
  job that builds and extracts Lean info → sanitized artifacts, and (2) a
  **secret-bearing** analysis job that consumes only those artifacts. Apply
  `_scrubbed_env()` everywhere untrusted Lean is elaborated (not just
  `lean_tools`).
- **Files:** `review/action.yml` (job graph), `review/lean_info_extractor.py:51-67`,
  `:118-135`.
- **Acceptance:** no secret present in env during untrusted elaboration; artifacts
  passed between stages are data-only.
- **Depends on:** C6.

### S3 · Nonce-fence untrusted content + low-trust-on-empty  — `TODO` · effort M · P1
- **Need:** `---` fences are forgeable from inside a Lean comment; spotlighting cuts
  static-attack ASR to <2% but collapses under adaptive attack, so this is
  defense-in-depth layered under S2.
- **Actions:** wrap all untrusted spans (diffs, file contents, PR title/body) in
  C6's nonce fence in both `review` and `summary` prompts; treat "reviewer
  returned zero findings on a non-trivial diff" as low-trust.
- **Files:** `review/prompts/*`, `summary/prompts/*`, both `*.py` call sites.
- **Depends on:** C6.

### S4 · summary: read policy file from base ref  — `TODO` · effort S · P1
- **Need:** `additional_instructions_path` (default `CONTRIBUTING.md`) is read from
  the attacker's checkout into an "obey-me" prompt slot.
- **Actions:** read it via `git show <BASE>:<path>` instead of the working tree.
- **Files:** `summary/summary.py:1180-1185`.
- **Acceptance:** test pins that the base-ref version is used, not the PR's.

### S5 · summary: authenticate the comment cache  — `TODO` · effort M · P1
- **Need:** public fingerprint + attacker-controlled diff hash lets a PR author
  post a crafted comment and inject per-file summaries.
- **Actions:** HMAC the cache with the GitHub token, or verify the carrying
  comment's author is the bot (`github-actions[bot]`).
- **Files:** `summary/summary.py:764-767`, `:776-793`, `:1100-1103`.
- **Acceptance:** test with a forged attacker comment shows the cache is rejected.

### S6 · Document the trust model + safe workflows  — `TODO` · effort S · P2
- **Need:** deployers must understand `pull_request_target`/`issue_comment`
  pitfalls.
- **Actions:** add a security section to the READMEs: the two-stage pattern, when
  secrets are/aren't present, chatops authorization, safe example workflows.
- **Refs:** [GitHub Security Lab](https://securitylab.github.com/resources/github-actions-new-patterns-and-mitigations/).

### S7 · Sandbox `lean_tools` model-directed Lean execution  — `TODO` · effort M · P1
- **Found during C3 (2026-07-08), out of that scope; recorded here.** The review
  reviewer/verifier agents can run model-chosen Lean code via `lean_typecheck`
  (`lake env lean --stdin`), which is arbitrary IO (filesystem/network) directed by
  a model that has ingested untrusted PR content. `_scrubbed_env()` removes secrets
  from the child env, but the checkout's `GITHUB_TOKEN` persists in `.git/config`
  and the process cwd is the repo, so exfiltration/tampering surface remains.
- **Shipped now as touched-file hardenings in C3 [2/2]** (not full mitigation):
  `persist-credentials: false` on the review checkout, a startup delete of any
  pre-existing `review_annotations.json`/`review_comments.json`, and a README
  warning not to wire the action under `pull_request_target` with a privileged
  token until this lands.
- **Actions (this item):** run model-directed Lean in a network-denied, cwd-pinned
  sandbox with a scrubbed `.git/config`; treat all tool output as data. Composes
  with S2's two-stage split.
- **Files:** `review/lean_tools.py`, `review/action.yml`.
- **Depends on / relates to:** S2.

---

## M2 — Fix existing tools' core defects

### review — right altitude & robustness

- **R1 · Demote the verdict** `TODO` · M · P1 — LLM-only findings become advisory;
  only deterministic/confirmed findings (escape hatches, verified) hard-block.
  Gate low-confidence/unverified to "needs a look," not Changes Requested.
  `review.py:1894-1900`. *This is the change the efficacy evidence most demands.*
- **R2 · Idempotent annotated review** `TODO` · M · P1 — dedupe/replace prior bot
  reviews (key on head SHA); today every run posts a duplicate. `action.yml:249`.
- **R3 · Wire or remove `BUILD_OUTPUT`** `TODO` · S · P2 — read at
  `review.py:2080`, never set. Either feed lake diagnostics in or delete the code
  and README claim.
- **R4 · Cap verification cost + graph-size guard** `TODO` · M · P2 — per-PR
  ceiling on verifier calls (`review.py:1728-1734`); truncate/guard the
  `LAKE_GRAPH`/`LEAN_INFO` env passthrough (`discover_files.py:185-188`).
- **R5 · Use `scrub_line` in `scan_escape_hatches`; dedupe overlapping clusters**
  `TODO` · S · P2 — via C5. `review.py:709/720`, `:2232-2249`.

### sorry-tracker — lifecycle & detection

- **T1 · Reliable dedup** `TODO` · M · P1 — list label-scoped issues once and
  match markers client-side; use a punctuation-free hash id; persist created ids
  per run to bridge GitHub search's eventual consistency. `github_issues.py:60-64`,
  `:87-94`.
- **T2 · Create the label idempotently** `TODO` · S · P1 — `gh label create` up
  front (fresh repos currently fail every issue). `github_issues.py:95-99`.
- **T3 · Compiler-truth detection** `TODO` · L · P1 — replace regex attribution
  with `lake build` "uses 'sorry'" warnings / Lean REPL (evaluate reusing
  [SorryDB](https://github.com/SorryDB/SorryDB)). Fixes the never-reset
  `current_decl_header` mis-attribution and the same-name-collapse false negative.
  `detection.py:60-68`, `:82`, `:92`.
- **T4 · Close the lifecycle** `TODO` · M · P1 — reconcile & auto-close
  resolved-sorry issues; reopen instead of recreate; add `--max-issues` and
  rate-limit spacing.
- **T5 · Output & offline polish** `TODO` · S · P2 — strip ```` ```markdown ````
  fences from LLM output; make `--dry-run` truly gh-free. `analysis.py:52-63`,
  `issues.py:39`, `:132-133`.

### summary — quality & correctness

- **U1 · Feed the enclosing declaration/statement to the summarizer** `TODO` · M ·
  P1 — reuse the source index already built for sorry tracking; biggest quality
  win per unit effort. `summary/prompts/summarize_file.md`, source-index code.
- **U2 · Deterministic PR labels** `TODO` · S · P2 — emit `sorry-added`,
  `native_decide`, `axiom-added` from the deterministic signals (cheaper and more
  trustworthy than prose; feeds M4/M5).
- **U3 · Fix `\ No newline` line-number desync** `TODO` · S · P2 — filter
  `\`-prefixed diff lines. `summary.py:639-641`.
- **U4 · Prompt-prefix caching + call-count cap** `TODO` · M · P2 — mark stable
  prompt templates `cache=True`; add a hard ceiling on files/LLM calls with an
  overflow "not individually summarized" list. `summary.py:308-313`, `:1219-1234`.

---

## M3 — Deterministic soundness gates  (new package: `soundness/`)

A new deterministic, no-LLM, injection-proof utility — usable both as a CI gate
and as a high-trust signal source for `review`/`summary`. This is the biggest gap
in ArkLib (no axiom check in CI today) and the highest-trust new capability.
Build on Lean primitives (principle #3); depends on C5.

**Most of this already exists in `evm-asm` as working shell scripts** — the job is
to generalize, parameterize (config), and roll out to the other repos (principle
#7), not to invent. Add a **ratchet mode** to each numeric gate: a committed
baseline file (sorry count, axiom count, warning count) that CI allows to *shrink
but never grow* — evm-asm's `check-conformance-floor.sh` + `*-baseline.txt` proves
the pattern and neatly handles "tolerate existing debt, block new debt."

- **G1 · Axiom-footprint diff gate** `TODO` · L · P1 — run `#print axioms` on
  changed declarations, diff base vs head, fail on **newly introduced** axioms /
  `sorryAx` / `Lean.ofReduceBool` (native_decide). Optional `lean4checker` replay
  for full assurance. *Prior art: `evm-asm/scripts/check-axioms.sh` + `axiom-allow.txt`
  (allowlist restricted to `propext`/`Classical.choice`/`Quot.sound`).*
  [ValidatingProofs](https://lean-lang.org/doc/reference/latest/ValidatingProofs/).
- **G2 · New-vs-baseline sorry-diff gate** `TODO` · M · P1 — using C5 + hunk line
  numbers, report only sorries the PR **adds**. Ratchet mode for the baseline
  count. *Prior art: `VCV-io/scripts/pr-summary.py` counts new sorries;
  `ArkLib/scripts/abf26/coverage.py` tracks sorry status; both are manual today.*
- **G3 · Cheating-tactic linter** `TODO` · M · P1 — flag `native_decide`,
  `bv_decide`, `implemented_by`, `@[extern]`, `opaque`, `decide` on
  non-trivially-decidable props, and spurious `axiom` declarations. *Prior art:
  `evm-asm/scripts/check-forbidden-tactics.sh`; `CompPoly/scripts/lint-style.py`
  (ERR_NDEC bans `native_decide`/`Lean.ofReduceBool`).*
- **G4 · Statement-weakening detector** `TODO` · L · P2 — `#print` a changed
  lemma's type on base vs head; flag dropped hypotheses / weakened conclusions
  (the misformalization class; mechanically checkable). *Prior art:
  `evm-asm/scripts/check-statement-tamper.sh`.*
- **Packaging** `TODO` · M — expose as a composite action + CLI; wire outputs into
  `review` (as confirmed findings) and `summary` (as labels).

---

## M4 — Triage, routing & shift-left  (new)

Validated by Mathlib's governance data; depends on M3's gates.

- **P1 · PR router / labeler** `TODO` · M · P2 — path-based area auto-labeling +
  reviewer routing. Support **both proven models** so a repo can pick by size:
  (a) *small team* — a `CODEOWNERS` wired directly to a written area-ownership doc,
  as in `cslib` (GOVERNANCE.md area maintainers → `.github/CODEOWNERS` auto-review);
  (b) *large team* — area-label map + `maintainer merge`/delegate flow with no
  CODEOWNERS, as in Mathlib (`scripts/autolabel.lean` + `check_title_labels`,
  `maintainer_merge.yml`). ArkLib currently has neither.
  [Growing Mathlib](https://arxiv.org/pdf/2508.21593).
- **P2 · Pre-flight "make-PR-ready" bot** `TODO` · L · P2 — on PR open, run the
  deterministic gates (build, warning budget, umbrella freshness, new-sorry diff,
  axiom diff, citation/blueprint completeness) and post a self-serve checklist;
  bounce mechanical failures back to the author before a human/LLM looks. Highest-
  leverage volume reducer.
- **P3 · Merge-queue / batching guidance** `PARKED` · P3 — document/adopt GitHub's
  native merge queue (bors is the validated pattern but mathlib3-era).

## M5 — Visibility & dashboards  (new)

- **V1 · Blueprint status sync** `TODO` · M · P2 — extend the
  [`leanblueprint`](https://github.com/PatrickMassot/leanblueprint) graph with live
  `\leanok`/sorry status; surface "stated but unproved." (ArkLib: 218 `\lean` vs 17
  `\leanok`.)
- **V2 · Sorry/axiom trend metric** `TODO` · M · P2 — emit a committed JSON + a
  per-PR delta comment (Mathlib's `technical-debt-metrics.sh` pattern; note the
  Goodhart caveat).
- **V3 · Cross-repo health dashboard** `TODO` · L · P3 — aggregate sorry counts,
  axiom footprint, build health, and toolchain drift across the several managed
  repos (the fleet: ArkLib, VCV-io, evm-asm, CompPoly, cslib, …). Prior art:
  [queueboard](https://leanprover-community.github.io/queueboard/).

## E — Evaluation harness  (cross-cutting, start early)

- **E1 · Lean-specific review eval set** `TODO` · L · P1, ongoing — adapt the
  structure of [Martian's code-review-benchmark](https://github.com/withmartian/code-review-benchmark)
  (curated PRs, human-verified golden findings, precision/recall) to Lean, using
  the kernel as ground truth where possible. Measure the review tool's precision/
  recall **and the actual lift from the adversarial-verification stage** (an open
  question the research could not answer). Track over time so tuning is
  evidence-based, not vibes.

---

## M6 — Full CI-gate & maintenance coverage  (new)

Closes the lifecycle gaps from the coverage map. All deterministic; each is a
composite action + CLI, config-driven, reusing `common` (C4/C5). Generalize the
patterns ArkLib/Mathlib run as ad-hoc scripts today. Prioritize within by how
much manual review each removes.

- **Q1 · Import / umbrella-file freshness** `TODO` · S · P2 — verify the umbrella
  `<Lib>.lean` imports exactly the module set; autofix mode. (ArkLib
  `check-imports.sh`.)
- **Q2 · Warning-budget gate** `TODO` · M · P2 — fail on scoped warning classes
  over a captured build log, with path-prefix/exclude config. (ArkLib
  `check-warning-log.py`.)
- **Q3 · Build-timing regression report** `TODO` · M · P2 — diff per-file build
  time vs a baseline artifact; comment on the PR. (ArkLib `build-timing-report`.)
- **Q4 · Text/style lint** `TODO` · M · P3 — whitespace, line-length, banned
  `set_option`/imports, docstring presence; config + exceptions file. (Mathlib
  `lint-style`.)
- **Q5 · Docs-integrity / link check** `TODO` · S · P3 — dead internal links,
  symlink/reference integrity. (ArkLib `check-docs-integrity.py`.)
- **Q6 · PR-title / commit-convention linter** `TODO` · S · P2 — enforce
  `type(scope): subject`; surfaces as a check + advisory comment.
- **Q7 · Release-tag on toolchain change** `TODO` · S · P3 — auto-tag when
  `lean-toolchain` changes. (ArkLib `release-tag.yml`.)
- **Q8 · Toolchain / mathlib bump** `TODO` · M · P3 — **wrap/document**, don't
  rebuild (principle #6). Prefer cslib's **LKG (last-known-good)** pattern
  (`lake-update.yml` + `leanprover-community/downstream-reports` — opens both a bump
  PR and an incompatibility issue) over plain dependabot / `mathlib-update-action`.
- **Q9 · Dependency-graph analysis** `TODO` · M · P3 — module/decl dependency graph
  + queries (import cycles, fan-in hotspots). (ArkLib `dependency_analysis/`.)
- **Q10 · Dead / unused-declaration finder** `TODO` · M · P3 — flag unreferenced
  private/internal declarations.
- **Q11 · Blueprint + doc-gen build/deploy workflow** `TODO` · M · P3 — reusable
  workflow that builds Lean docs + the `leanblueprint` site and deploys to Pages.
- **Q12 · Deprecation tracking** `TODO` · M · P3 — inventory `@[deprecated]` and
  age them out; config-driven policy. *Prior art: Mathlib's rich toolkit —
  `scripts/create_deprecated_modules.lean`, `add_deprecations.sh`,
  `remove_deprecated_decls.yml` + the `DeprecatedModule`/`DeprecatedSyntax` linters.*

Further items from the six-repo survey:

- **Q13 · Generated-doc integrity** `TODO` · M · P2 — render status/reference docs
  from a Lean exe and `--check` they match in CI, so status docs can't silently
  drift from the code. *Prior art: `evm-asm` renders PROGRESS.md/DRIFT.md from
  `MainProgress.lean` and gates on drift; `VCV-io/scripts/extract-doc-fragments.py
  --check`; ArkLib's `kb/check_generated.py`.* The DRIFT.md "what is NOT proven"
  TCB ledger is a strong pattern to offer generically.
- **Q14 · Layering / import-direction / TCB-isolation checker** `TODO` · M · P2 —
  enforce that core modules don't import downstream/interop/codegen layers
  (config: allowed edges). High-value soundness-adjacent gate. *Prior art:
  `VCV-io/scripts/check-interop-isolation.sh`, `evm-asm/scripts/check-layering.sh`,
  Mathlib's `DirectoryDependency` linter.*
- **Q15 · Structural budgets** `TODO` · M · P3 — `maxHeartbeats` ceiling with an
  approved-overrides allowlist (proof-performance budget), file-size caps, and
  unimported-file detection. *Prior art: `evm-asm/scripts/check-heartbeats-approved.sh`,
  `check-file-size.sh`, `check-unimported.sh`.*
- **Q16 · Structure/taxonomy invariants** `TODO` · M · P3 — optional, config-driven:
  every module imports the repo prelude; every declaration lives under a namespace
  in the sanctioned taxonomy. *Prior art: `cslib`'s `checkInitImports` exe and the
  `topNamespace` env-linter (`Cslib/Foundations/Lint/Basic.lean`).*
- **Q17 · Citation / bib validation** `TODO` · M · P2 — check docstring citations
  resolve to `references.bib`, flag undefined/unused bib keys. *Prior art: ArkLib
  `kb/` (bib→JSON sync, citation extraction), Mathlib `lint-bib.sh`, `cslib`'s
  `[Author][BibKey]` docstring convention (no checker yet — ripe to add).*
- **Q18 · Unused imports** `TODO` · M · P3 — wrap `shake` for unused-import
  detection (dead/unused *declarations* are Q10). *Prior art: Mathlib `shake.yaml`
  + `noshake.json`, `VCV-io/scripts/noshake.json`.*
- **Q19 · Downstream-breakage check across the fleet** `TODO` · L · P2 — your repos
  form a dependency graph (ArkLib requires VCVio + CompPoly); test whether a change
  in an upstream repo breaks its downstreams and report it. *Prior art: Mathlib
  `pr_check_downstream.yml` + `downstream_dashboard.py`; cslib's
  `downstream-reports` LKG mechanism.*
- **Q20 · PR-hygiene automation** `TODO` · M · P3 — stale-PR nudging and
  progress/velocity/churn tracking. *Prior art: `evm-asm` `stale-pr-nudge.yml`,
  `progress-velocity-check.yml`, `churn-report.sh`.*

## M7 — Platform & adoption  (new)

The spine that makes the collection a product. Start the P1 items early; every new
capability (M2–M6) plugs into them.

- **X1 · Capability catalog** `TODO` · S · P1 — a single machine-readable manifest
  (name, kind action/CLI, purpose, inputs, config keys) that generates the README
  menu and powers the bootstrapper. Every tool registers here.
- **X2 · Shared config schema** `TODO` · M · P1 — one config file in the target
  repo (e.g. `leanrepo.toml`) that all tools read: model slugs, spend caps, area
  path→label map, warning budget, sorry policy, citation/KB toggles. Replaces
  scattered per-action inputs. (Enables principle #6.)
- **X3 · One-command bootstrapper** `TODO` · L · P1 — `lean4repo-utils init`
  inspects a target repo (lakefile, layout, blueprint presence) and drops in the
  right workflow files + a starter `leanrepo.toml`; `--check`/`--update` modes for
  keeping several repos in sync. The core "one-stop shop" feature.
- **X4 · Packaging convention + template** `TODO` · S · P1 — a documented contract
  (composite action + CLI + tests + catalog entry + config keys) and a cookiecutter
  so new capabilities are uniform.
- **X5 · Docs site** `TODO` · M · P2 — generated from the catalog: per-capability
  pages, the lifecycle map, adoption guide, security/trust model (S6).
- **X6 · Toolkit versioning & release** `TODO` · M · P2 — tagged releases so target
  repos can pin `@vN` instead of `@main`; changelog; CI publishes the catalog/docs.
- **X7 · Agent-onboarding scaffold** `TODO` · M · P2 — generate/refresh the
  `AGENTS.md` (+ `CLAUDE.md` symlink), a `docs/wiki/` hub with a maintenance
  contract, and `docs/skills/` reusable workflows — the pattern every surveyed repo
  reinvented (ArkLib/VCV-io/CompPoly wikis, cslib `copilot-instructions.md`,
  evm-asm `AGENTS.md`+`docs/agents/`). Ship a template + a drift check.

## Appendix A — Traceability (item → source)

- **Code review (pass 1):** C1–C6, S1/S4/S5, R1–R5, T1–T5, U1–U4 each carry the
  originating `file:line` above. Blockers independently verified against source
  this session: S1 (no `ref:` in `review/action.yml`), C1 (no `usage:{include}`),
  C2 (no `timeout=`), S4 (working-tree `open()`), T1/T2 (search-based dedup, no
  `gh label create`), sorry-tracker `current_decl_header` never reset.
- **ArkLib survey:** motivates M3 (no axiom check in CI), G2 (manual new-vs-old
  sorry distinction), P1 (no CODEOWNERS), V1 (blueprint `\leanok` gap), V3
  (several co-managed repos).
- **Deep research (verified claims):** principle #1 (efficacy benchmarks) → R1, E1;
  principle #2 + S2 (CVE-2025-47928); S3 (spotlighting static vs adaptive);
  G1 (Lean audit primitives, lean4checker); T3 (SorryDB); P1/V2 (Growing Mathlib);
  V1 (leanblueprint); V3 (queueboard). Full citations inline above.

## Appendix B — Research caveats to respect while executing

- Governance guidance rests largely on one (authoritative, maintainer-authored)
  source — Mathlib-specific, generalize with care.
- AI-review efficacy numbers are from general-software benchmarks; Lean's
  kernel-checkable ground truth may change the picture — E1 exists to find out.
- The GitHub Actions security specifics (S2/S6 support) were extracted from
  primary sources but did **not** pass the 3-vote verification gate; treat as
  strong leads and re-confirm against the cited advisories when implementing.
- Model-specific benchmark numbers are early-2026 and will drift.

## Appendix C — Cross-repo capability matrix (the fleet)

Surveyed 2026-07: **A**=ArkLib, **V**=VCV-io, **E**=evm-asm, **M**=mathlib4,
**C**=CompPoly, **S**=cslib. "✓" = has a working implementation; the point is how
much is **already built and duplicated** — consolidation targets (principle #7).
Classification: **core** = belongs in the generic toolkit; **cfg** = generic
mechanism + repo-specific config; **specific** = stays in the repo.

| Capability | A | V | E | M | C | S | → item | Class |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | --- | --- |
| Build + Mathlib cache | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | (baseline) | core |
| Import/umbrella freshness (`mk_all`/`update-lib`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Q1 | core |
| Warning budget / `--wfail` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Q2 | cfg |
| Style / whitespace lint | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Q4 | core |
| Build-timing regression report | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Q3 | core |
| Docs-integrity / link check | ✓ | ✓ |  |  | ✓ |  | Q5 | core |
| Docs + (blueprint) build & deploy | ✓ |  |  | ✓ |  | ✓ | Q11 | cfg |
| Release-tag on toolchain change | ✓ | ✓ |  | ✓ | ✓ |  | Q7 | core |
| Toolchain / mathlib bump | ✓ |  |  | ✓ | ✓ | ✓ | Q8 | wrap |
| LLM PR summary + review | ✓ | ✓ | ✓ |  | ✓ |  | (this repo!) | core |
| **Axiom audit / allowlist** |  |  | ✓ |  |  |  | **G1** | core |
| **Forbidden/cheating tactics** |  |  | ✓ |  | ✓ |  | **G3** | core |
| **Statement-tamper / weakening** |  |  | ✓ |  |  |  | **G4** | core |
| New-vs-baseline sorry diff | ~ | ~ | ~ |  |  |  | G2 | core |
| Layering / import-direction / TCB | ~ | ✓ | ✓ | ✓ |  | ~ | Q14 | cfg |
| Generated-doc integrity (`--check`) | ✓ | ✓ | ✓ |  |  |  | Q13 | core |
| Ratchet/baseline (monotonic floor) |  |  | ✓ |  |  |  | G2/V2 | core |
| `maxHeartbeats` / file-size budgets | ~ |  | ✓ | ✓ |  |  | Q15 | cfg |
| Init-prelude + namespace/taxonomy |  |  |  | ✓ |  | ✓ | Q16 | cfg |
| Unused imports (`shake`) / dead decls |  | ✓ | ✓ | ✓ |  | ~ | Q18 | core |
| Citation / bib validation | ✓ | ~ | | ✓ | | ~ | Q17 | cfg |
| PR-title convention check |  |  | ~ | ✓ |  | ✓ | Q6 | core |
| Autolabel by area | | | ✓ | ✓ | | | P1 | core |
| CODEOWNERS / area-ownership routing |  |  | ✓ | — |  | ✓ | P1 | cfg |
| Maintainer-merge / bors queue |  |  |  | ✓ |  |  | M4 P3 | wrap |
| Stale-PR nudge / velocity / churn |  |  | ✓ | ~ |  |  | Q20 | core |
| Downstream-breakage tracking |  |  |  | ✓ |  | ✓ | Q19 | core |
| Deprecation automation |  |  |  | ✓ |  |  | Q12 | core |
| Benchmark harness |  |  | ✓ | ✓ | ✓ | ✓ | (E1-adjacent) | cfg |
| C FFI build (nightly-isolated) |  | ✓ |  |  |  |  | — | specific |
| Differential testing vs reference | | ✓ | ✓ | | | | — | specific |
| Docker reproducibility env |  |  | ✓ | ✓ |  |  | — | specific |
| Sorry-tracker (issues) — this repo | ✓ |  |  |  |  |  | `sorry-tracker/` | core |
| Agent guide + wiki maintenance contract | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | X7 | core |

Read-out: the top block is **already in every repo, duplicated** → consolidation is
the highest-confidence win; the **bold** soundness gates exist only in evm-asm →
generalizing them is the highest-*value* win; `specific` rows stay per-repo (offered
as templates). `~` = partial/manual/convention-only.
