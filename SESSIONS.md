# leanrepo-utils — session plan & execution protocol

Companion to [`ROADMAP.md`](ROADMAP.md). ROADMAP says *what* and *why* (with
`file:line` refs, acceptance criteria, prior art). This doc says *how we work*:
the validated, adversarial protocol every session follows, and the split of the
whole backlog into self-contained sessions.

> **Prime directive.** We are after clean, correct code that others can safely
> depend on. Quality dominates speed. Every single line is validated. If rigor
> slows us down, that is acceptable and expected. All reviews are **adversarial** —
> the reviewer's job is to *break* the work, not to bless it.

---

## Per-session protocol (four phases, hard gates between them)

Each session is one coherent unit → one self-contained **local commit** (a PR only
at milestones). Do **not** proceed past a gate until it is satisfied; if a gate
fails, fix or re-scope before continuing.

### Phase 0 — Prior-work review *(adversarial)*
- Read this session's ROADMAP item(s) and their status.
- Read the originating evidence: pass-1 review `file:line`, research citations,
  and any prior-art script named in the item.
- **Re-verify dependencies actually landed and are correct.** Do not trust a
  `DONE` marker — re-check each dependency's acceptance criteria still holds and
  hasn't regressed. Adversarially confirm the current code matches the
  assumptions this session is built on (the referenced lines still exist/mean
  what we think).
- **Output:** a short written *context brief* (what's done, what this depends on,
  confirmed current state, anything that has drifted since the roadmap was written).
- **Gate 0:** the brief is written and every dependency is confirmed green. If a
  dependency is not actually done/correct, stop and address that first.

### Phase 1 — Task & plan review *(adversarial)*
- Restate the scope and the acceptance criteria in your own words.
- Draft the execution plan: files to touch, approach, the tests that will prove it.
- **Adversarially critique the plan before writing code:** edge cases, failure
  modes, security (prompt injection, untrusted input, secret exposure), blast
  radius, backward compatibility, what a hostile reviewer would attack.
- **Reuse/license check** (if adapting anything from another repo — see policy
  below): identify source repo + exact path + commit, read its LICENSE and the
  file header, confirm license compatibility, decide copy-with-attribution vs
  clean-room reimplementation, and plan the attribution mechanics.
- **LLM check** (if the session touches LLM calls, prompts, model/slug selection,
  token/cost/usage accounting, prompt caching, timeouts, streaming, tool-calling,
  or structured output): consult the **OpenRouter API reference**
  (<https://openrouter.ai/docs/api/reference/overview>) and cite the specific
  endpoint/parameter your plan relies on. Several pass-1 bugs (cost always `0.0`,
  no timeout) came from not matching the API contract — verify against the docs,
  not memory.
- **Gate 1:** the plan survives adversarial critique and (if applicable) the
  license path is settled. Prefer to use `EnterPlanMode`/`ExitPlanMode` for this.

### Phase 2 — Execute
- Implement in small, reviewable increments; no drive-by changes outside scope.
- Write tests alongside (or first), covering the Phase-1 edge cases — not just the
  happy path, and not tests that only assert the mock was called.
- Validate continuously: run the member's test suite, `ruff check`, and exercise
  the change **end-to-end** where it has a runtime surface (use the `verify`
  skill / drive the actual tool, don't rely on unit tests alone).
- Add attribution at the point of reuse (header comment + `NOTICE` entry).

### Phase 3 — Post-execution review + documentation refresh *(adversarial)*
- **Adversarial diff review** (use `code-review` at high effort, or a fresh
  fork/subagent so the reviewer isn't the author): hunt correctness bugs, missed
  edge cases, security holes, over-mocked tests, and confirm attribution/license
  compliance is present and correct. Findings must be resolved or explicitly,
  defensibly deferred.
- Re-verify **every** acceptance criterion, end-to-end.
- **Documentation refresh** (part of Done, not optional): flip the ROADMAP item to
  `DONE` with a one-line result; update the affected README(s); once they exist,
  update the capability catalog (X1) and `CHANGELOG`; update `NOTICE` for any
  reuse; add/refresh a project memory if a non-obvious fact emerged.
- **Gate 3:** commit-ready — clean, correct, self-contained, documented, attributed
  (PR-ready at milestones).

---

## Definition of Done (every session, no exceptions)
1. Acceptance criteria (ROADMAP) all met and **verified end-to-end**, not just unit-tested.
2. Tests added/updated; they exercise real behavior and the Phase-1 edge cases.
3. `ruff check` clean; member test suite green; workspace still builds.
4. Adversarial post-review passed; findings resolved or defensibly deferred.
5. Any reuse is license-compatible and attributed (header + `NOTICE`).
6. Docs refreshed: ROADMAP status, README(s), catalog/CHANGELOG (once they exist), memory.
7. One coherent, self-contained local commit (PRs only at milestones); nothing out
   of scope; no unexplained TODOs left behind.

## Reuse, license & attribution policy
This repo is **Apache-2.0**. Before adapting *any* code from another repository:
1. **Verify the source LICENSE at the exact commit** — never assume. Read the
   repo LICENSE *and* the individual file header (files can carry their own).
2. **Compatibility:** Apache-2.0 / MIT / BSD-style → OK to copy with attribution.
   GPL/AGPL/LGPL/unlicensed/ambiguous → **do not copy**; reimplement clean-room
   from the behavior/spec and note it was independently written.
3. **Attribution** when copying/adapting: (a) an in-file header —
   `Adapted from <repo>@<short-sha> <path> (licensed <SPDX>)`; (b) preserve any
   original copyright lines; (c) an entry in a top-level `NOTICE` file; (d) record
   provenance in the commit message and the ROADMAP item.
4. **Even for the user's own repos** (ArkLib, VCV-io, evm-asm, CompPoly) attribute
   the source repo/path/commit for traceability — and never relicense vendored
   third-party content (e.g. VCV-io's `third_party/` C code has its own licenses).
5. **Prefer wrapping** a maintained upstream action/tool over vendoring its source
   (principle #6): `lean-action`, `lean-release-tag`, `mathlib-update-action`,
   `lint-style-action`, `leanblueprint`, `lean4checker`.

*Licenses to confirm at reuse time (not yet verified here):* mathlib4 (expected
Apache-2.0), the user's repos (expected Apache-2.0), `leanblueprint`, `SorryDB`,
Martian `code-review-benchmark`, `leanprover-community/*` actions.

---

## Orchestration model (chosen)

**Strictly serial · human at every gate · runner-assisted.** Exactly one session in
flight at a time. The maintainer approves both the Phase-1 plan gate and the Phase-3
review gate for every session. A reusable **session-runner** does the read-only
analysis that bookends each gate so the human decides on a synthesized brief, not a
blank page. A background workflow cannot pause for approval, so the runner is split
into two read-only invocations with the human gate (and the code-writing) in between:

1. **PLAN gate** — run the runner in `plan` mode → returns the prior-work brief
   (deps adversarially re-verified against real code), a compliance pre-check
   (OpenRouter docs + reuse/license), an adversarially-critiqued execution plan, and
   the risks that must be resolved before coding. **Human approves.**
2. **Phase 2 — execute** — in the main session, on a working branch (not `main`).
   Small increments, tests + end-to-end verification, attribution at point of reuse.
3. **REVIEW gate** — run the runner in `review` mode over the **uncommitted** diff →
   returns a multi-lens adversarial review (correctness / security / license /
   test-quality / acceptance+docs) with blocking vs non-blocking findings.
   Reviewer ≠ author by construction. **Human resolves blockers, then approves.**
4. **Land locally** — the machine gate is run **locally** (`uv run --no-sync ruff check .` +
   the relevant member test suite(s), from `README.md` §Development); on green,
   make **one focused local commit** for the session and flip the ROADMAP item to
   `DONE`. No per-session PR.

### Milestones & PRs
Progress lives locally as per-session commits on a working branch. Open a PR
(and run CI) **only at a significant milestone** — a wave completing or a shippable,
user-facing capability landing, e.g.: Wave 0 foundation done · Wave 1 security
unlock done · `soundness/` package shipped · PR router shipped · bootstrapper
shipped. The maintainer decides when a milestone is reached; at that point push the
working branch and open one PR covering the batch, reviewed at milestone granularity
(`review` mode with `base: <branch-point>`).

**Runner:** [`.claude/workflows/session-runner.js`](.claude/workflows/session-runner.js).
Invoke with the `Workflow` tool, e.g.
`Workflow({ scriptPath: ".claude/workflows/session-runner.js", args: { session: "1", items: "C1, C2", phase: "plan" } })`,
then after coding (per-session review of uncommitted work — omit `base`)
`Workflow({ ..., args: { session: "1", items: "C1, C2", phase: "review" } })`,
or at a milestone `args: { ..., phase: "review", base: "<branch-point>" }`.
The runner is **read-only** (it never edits the repo); all mutation happens in the
human-supervised Phase 2. Selective worktree parallelism (Wave 7) is deferred while
the model is strictly serial.

## Reference docs
- **OpenRouter API** — <https://openrouter.ai/docs/api/reference/overview> —
  authoritative for the shared LLM layer. **Mandatory** reading for any
  LLM-touching session (see the Phase-1 LLM check). LLM-touching sessions in the
  backlog below: **1, 2, 5, 7, 13, 14, 17, 23, 24, 27, 63, 64** (any that touch
  model output, cost/usage, caching, or prompts).
- Lean soundness primitives — <https://lean-lang.org/doc/reference/latest/ValidatingProofs/>
  (Wave 4 soundness gates).

## Session backlog (dependency-ordered)

IDs reference ROADMAP items. "Reuse" flags a license/attribution checkpoint.
`L` items may split into more than one commit if a phase gate can't otherwise be
met — keep coupled items (e.g. S1+S2) in a single session/commit. 64 sessions total.

### Wave 0 — Foundation (`common/`) — do first
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 1 | C1, C2 | LLM provider: real cost accounting + request timeout + configurable concurrency | — | — |
| 2 | C3 | Per-run cost/token ceiling + graceful degradation + tool-phase usage accounting + loud LLM-failure surfacing (per-key limits = hard cap, wrapped) | 1 | — |
| 3 | C4 | `lean_utils` scanning correctness fixes | — | — |
| 4 | C5 | Shared sorry/axiom/escape-hatch matcher + migrate all three tools | 3 | evm-asm token lists |
| 5 | C6 | Injection-resistant content wrapper (+ optional response cache) | — | — |

### Wave 1 — Security (the outside-PR unlock)
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 6 | S1, S2 | **Blocker:** chatops checkout ref + two-stage secret-free build/analysis split *(one commit — coupled)* | 1 | CVE-2025-47928 / GH Security Lab (docs only) |
| 7 | S3 | Nonce-fence untrusted content + low-trust-on-empty | 5 | — |
| 8 | S4 | summary: read policy file from base ref | — | — |
| 9 | S5 | summary: authenticate the comment cache | — | — |
| 10 | S6 | Security trust-model docs + safe example workflows | 6–9 | — |

### Wave 2 — Platform spine (start early; everything later plugs in)
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 11 | X1, X2 | Capability catalog manifest + shared `leanrepo.toml` config schema | — | — |
| 12 | X4 | Packaging convention + capability template (cookiecutter) | 11 | — |
| 13 | *(new)* | **Fleet convergence** *(needs your go-ahead)* — point ArkLib/VCV-io/CompPoly at `summary/`+`review/` here; retire the split external actions | 6–10, 11 | update those repos' workflows |

### Wave 3 — Fix existing tools (`review`, `sorry-tracker`, `summary`)
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 14 | R1 | review: demote verdict (LLM advisory; deterministic-only blocks) | — | — |
| 15 | R2 | review: idempotent annotated review (dedupe/replace, key on head SHA) | — | — |
| 16 | R3 | review: wire or remove `BUILD_OUTPUT` | — | — |
| 17 | R4 | review: cap verification cost + graph-size env guard | — | — |
| 18 | R5 | review: use `scrub_line` in escape-hatch scan; dedupe clusters | 4 | — |
| 19 | T1 | sorry-tracker: client-side dedup + punctuation-free id + per-run persistence | — | — |
| 20 | T2 | sorry-tracker: idempotent `gh label create` | — | — |
| 21 | T3 | sorry-tracker: compiler-truth detection (`lake`/REPL); evaluate SorryDB reuse `L` | 4 | SorryDB |
| 22 | T4 | sorry-tracker: lifecycle (close/reopen, `--max-issues`, rate spacing) | 19 | — |
| 23 | T5 | sorry-tracker: strip md fences; truly-offline `--dry-run` | — | — |
| 24 | U1 | summary: feed enclosing declaration/statement to summarizer | 3 | — |
| 25 | U2 | summary: deterministic PR labels from signals | 4 | — |
| 26 | U3 | summary: fix `\ No newline` line-number desync | — | — |
| 27 | U4 | summary: prompt-prefix caching + call-count cap w/ overflow list | — | — |

### Wave 4 — Deterministic soundness gates (new `soundness/`)
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 28 | G3 | Cheating-tactic linter | 4, 11 | evm-asm `check-forbidden-tactics.sh`, CompPoly `lint-style.py` |
| 29 | G1 | Axiom-footprint diff gate (+ ratchet; optional `lean4checker`) `L` | 4, 11, 28 | evm-asm `check-axioms.sh`+`axiom-allow.txt`; lean4checker |
| 30 | G2 | New-vs-baseline sorry-diff gate (ratchet mode) | 4, 11 | evm-asm conformance-floor; VCV `pr-summary.py` |
| 31 | G4 | Statement-weakening detector `L` | 4, 11 | evm-asm `check-statement-tamper.sh` |
| 32 | *(G pkg)* | `soundness/` composite action + CLI; wire outputs into review/summary | 28–31 | — |

### Wave 5 — Triage, routing & shift-left
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 33 | P1 | PR router/labeler — both models (CODEOWNERS-area & autolabel+maintainer-merge) | 11 | cslib CODEOWNERS; mathlib `autolabel.lean` |
| 34 | P2 | Pre-flight make-PR-ready bot (runs the gates, posts checklist) `L` | 28–32, 11 | ArkLib `docs/skills/make-pr-ready.md` |
| 35 | P3 | Merge-queue guidance/integration (native merge queue) `PARKED` | — | mathlib `bors.toml` (ref) |

### Wave 6 — Visibility & dashboards
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 36 | V2 | Sorry/axiom trend metric (committed JSON + per-PR delta; ratchet) | 29, 30 | mathlib `technical-debt-metrics` |
| 37 | V1 | Blueprint status sync (extend `leanblueprint`) | 11 | leanblueprint |
| 38 | V3 | Cross-repo fleet health dashboard `L` | 29, 30, 36 | mathlib queueboard (ref); optionally consumes 57 (Q19) |

### Wave 7 — Full CI-gate & maintenance coverage (consolidation; each generalizes a fleet script)
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 39 | Q1 | Import/umbrella freshness (+ autofix) | 11 | ArkLib `check-imports.sh`, mathlib `mk_all.lean`, VCV `update-lib.sh` |
| 40 | Q2 | Warning-budget gate (scoped + `--wfail`; ratchet) | 11 | ArkLib/VCV `check-warning-log.py` |
| 41 | Q3 | Build-timing regression report | 11 | the shared `build_timing_report.sh` (all repos) |
| 42 | Q4 | Text/style + whitespace lint | 11 | mathlib lint-style; consider wrapping `lint-style-action` |
| 43 | Q5 | Docs-integrity / link check | 11 | ArkLib/CompPoly `check-docs-integrity.py` |
| 44 | Q6 | PR-title / commit-convention linter | 11 | mathlib `ValidatePRTitle`, cslib `pr-title.yml` |
| 45 | Q7 | Release-tag on toolchain change | — | wrap `lean-release-tag` |
| 46 | Q8 | Toolchain/mathlib bump (LKG pattern) | — | wrap `mathlib-update-action`/`lean-update`/`downstream-reports` |
| 47 | Q9 | Dependency-graph analysis (module/decl graph + queries) | 11 | ArkLib `dependency_analysis/`, mathlib import-graph |
| 48 | Q10 | Dead / unused-declaration finder | 11 | mathlib `UpstreamableDecl`/`grind_unused_lemmas.sh` |
| 49 | Q11 | Blueprint + doc-gen build/deploy reusable workflow | 37 | wrap doc-gen4 + leanblueprint; ArkLib `docs.yml` |
| 50 | Q12 | Deprecation tracking automation | 11 | mathlib deprecation toolkit |
| 51 | Q13 | Generated-doc integrity (`--check`) | 11 | evm-asm `check-progress`/`check-drift`, VCV `extract-doc-fragments.py` |
| 52 | Q14 | Layering/import-direction/TCB-isolation checker | 11 | VCV `check-interop-isolation.sh`, evm-asm `check-layering.sh`, mathlib `DirectoryDependency` |
| 53 | Q15 | Structural budgets (`maxHeartbeats`, file-size, unimported) | 11 | evm-asm `check-heartbeats-approved`/`check-file-size`/`check-unimported` |
| 54 | Q16 | Structure/taxonomy invariants (init-prelude, namespace linter) | 11 | cslib `checkInitImports` + `topNamespace` |
| 55 | Q17 | Citation / bib validation | 11 | ArkLib `kb/`, mathlib `lint-bib.sh`, cslib bib convention |
| 56 | Q18 | Unused imports (`shake`) | 11 | mathlib `shake`/`noshake`, VCV `noshake.json` |
| 57 | Q19 | Downstream-breakage check across the fleet dep-graph `L` | 11 | mathlib `pr_check_downstream`, cslib `downstream-reports` |
| 58 | Q20 | PR-hygiene automation (stale nudge, velocity, churn) | 11 | evm-asm `stale-pr-nudge`/`progress-velocity`/`churn-report` |

### Wave 8 — Platform completion & evaluation (E1 is cross-cutting, can start after #1)
| # | Items | Session | Deps | Reuse |
|---|---|---|---|---|
| 59 | X3 | One-command bootstrapper (`init`/`--check`/`--update`) `L` | 11, 12 | — |
| 60 | X5 | Docs site (generated from catalog) | 11, 12 | — |
| 61 | X6 | Toolkit versioning & release (pin `@vN`) | 12 | — |
| 62 | X7 | Agent-onboarding scaffold (AGENTS.md + wiki contract + skills) | 11 | fleet patterns |
| 63 | E1a | Eval harness skeleton + first Lean eval set `L` | 1 | Martian `code-review-benchmark` (structure) |
| 64 | E1b | Ongoing measurement + adversarial-verification-lift study | 63, 14 | — |

---

## Progress
- **Session 1 (C1, C2)** — ✅ **DONE**, merged to `main` in PR #1 (`4ef9174`); C1
  live-verified ($0.000124). Reviewed across three adversarial passes.
- **Session 2 (C3)** — Gate-1 **APPROVED** (full scope, one session); ready to
  execute on branch `session-2-c3`. Approved plan + R1–R9 in [`ROADMAP.md`](ROADMAP.md) C3.

## Recommended start
Sessions **1 → 2 → 3 → 4** (Wave 0), then **6** (the security blocker). Session 11
(spine) can begin any time and should land before Wave 3 so tool fixes adopt the
shared config. Session 13 (fleet convergence) is **blocked on your decision** —
confirm whether this repo becomes the canonical action source for your other repos.
