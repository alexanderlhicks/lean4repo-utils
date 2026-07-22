# Bootstrap: remaining review-toolkit gaps (deterministic / exhaustive-mode)

**For:** an agent or human picking up the code side of the ABF26-review improvements.
**Status of the parent work:** the *prompt-level* recommendations from
[`IMPROVEMENTS_FROM_ABF26_REVIEWS.md`](IMPROVEMENTS_FROM_ABF26_REVIEWS.md) (§1 source-of-source
fidelity, §2 boundary-probe recipe, §3 security-def-shape / `present-but-different`, §4
definitional-chain trace, §5 carrier rubric, §6 coverage non-vacuity) are **all already landed**
in `prompts/` (verified 2026-07-19). Those were the cheap, high-signal wins and they shipped.

## Is any of this actually needed?

**No — not for the toolkit to work.** Everything below is an *optional deterministic backstop*
for **exhaustive-mode / scheduled-audit** runs, exactly as the source doc frames §7–§8
("artifacts and process for exhaustive-mode runs… recommendations, not code"). The per-PR
reviewer is complete and the LLM-facing checks already cover these defect classes in prose. Build
these only if you want machine-checked artifacts (not just model judgement) on deep runs. They are
listed in rough value-per-effort order. Do **not** over-invest; a partial subset is fine.

All `review.py:` line numbers below are as of branch `session-13b-summary`
(`git log -1` = `15de859`); re-anchor by symbol name if they have drifted.

---

## Shared plumbing (read once)

- **Config flow** (`action.yml` input → step `env:` → `review.py`). Inputs declared
  `action.yml:4-88`, mapped in the run-review step `env:` block `action.yml:251-287`. `review.py`
  reads them via argparse (`main()`, `review.py:2970-2988`) **or** `os.environ` (e.g.
  `enable_web_search`/`lean_tools` at `review.py:3145`,`3180-3183`). **No config dataclass** —
  config lives on the `args` Namespace, a few module globals (`LEAN_TOOLS_ENABLED` `229`), and the
  `review_context` dict (`review.py:3197-3207`).
- **Artifact emission.** Primary output = one in-memory PR-comment string printed to stdout
  (`review.py:3630`), captured as `review_text` (`action.yml:296-313`). Disk artifacts are written
  next to it (`review_comments.json` `3633`, `review_annotations.json` `3650`, `review_health.json`
  `3678`) and read back by Post Review (`action.yml:366-411`). **New file artifacts belong in this
  same `review.py:3629-3654` block**, with a matching reader step in `action.yml`.
- **Deterministic Lean primitives** (no LLM): `lean_info_extractor.py:run_lean_command` (`114-131`,
  `lake env lean --stdin` with `import <module>` prepended) and `extract_axioms` (`134-190`, already
  runs `#print axioms <decl>` per decl — `sorryAx` surfaces here). `#print axioms` is auto-run today
  at `lean_info_extractor.py:165`, its output reaching `review.py` via the `LEAN_INFO` env var
  (`review.py:3083`). This is the precedent to mirror for any new deterministic probe.

---

## Gap A — `SOURCE_LEDGER.md` emission  *(highest value, lowest effort)*

**What.** A deterministic artifact: one row per admitted `sorry`, `{decl, cited_source,
disposition}`. Model = Review B's `SOURCE_LEDGER.md`. Ref: IMPROVEMENTS §1 / §8.

**Why.** Makes the source-fidelity check (already in the prompts) *auditable* — a checkable list of
every external admit and its ultimate cited source, instead of only in-comment prose.

**Where it plugs in.**
- Per-decl data already exists: `paper_lean_evidence.py:extract_lean_declarations` (`124-181`)
  yields per-decl `short_name`, `signature`, `docstring_present` (`178`), and
  `contains_sorry_or_admit` (`179`).
- **Missing piece — citation-tag parsing.** There is *no* `[BCHKS25 Thm 1.3]`-style parser anywhere
  (confirmed). Add one right at `extract_lean_declarations` (`~178`): scan the decl's preceding
  docstring line(s) and `short_name` for a citation token (`\[[A-Z][A-Za-z0-9]+\d\d[^\]]*\]`, plus
  `_<lowername><yy>` name mnemonics). Emit `cited_source` (or `None`).
- **Emit** the ledger as a file in the `review.py:3629-3654` block (iterate admitted decls, one
  markdown row each; `disposition` starts `unadjudicated` — the verifier/synthesis can upgrade it to
  `shape-pass | fail | translation-gap`). Add a reader/attach step in `action.yml:366-411` if it
  should be posted.

**Acceptance.** On a PR with tagged external admits, `SOURCE_LEDGER.md` lists every `sorry`-decl
with its parsed citation (or an explicit "no cited source found"), and no admitted decl is missing.
**Effort:** ~half a day (parser + emit + one reader step).

---

## Gap B — coverage-matrix symbol-resolution  *(medium value, low effort)*

**What.** If the repo keeps a coverage/audit matrix (paper result → Lean decl → present/sorry
status), a deterministic check that **every named symbol resolves** and its **claimed sorry-status
matches `#print axioms`**. Ref: IMPROVEMENTS §6 (both reviews found stale/phantom matrix rows).

**Why.** The prompt rule (`verdict_rules.md:25`) tells the model a self-disclosed placeholder isn't
coverage, but nothing mechanically catches a matrix row naming a decl that was **renamed / never
existed**, or whose sorry-status is stale.

**Where it plugs in.** No coverage-matrix consumer exists today (only the free-text
`checklist_coverage` field, `review.py:116`, and heuristic token-matching in
`paper_lean_evidence.py:build_navigation_hints` `369-404`). Add a small **deterministic phase** near
the other deterministic checks (`review.py:3414-3498`): given a matrix path (new input, see Gap D
for the flag), for each symbol call `lean_info_extractor.run_lean_command`/`extract_axioms`
(or `lean_tools.LeanToolbox.run "lean_print" / "lean_print_axioms"`) → unresolved symbol or
sorry-status mismatch = a finding. Matrix location is repo-specific → make it an input
(`coverage_matrix_path`), default off.

**Acceptance.** A matrix row naming a nonexistent decl, or marking a `sorryAx`-carrying decl as
"proven", produces a finding; a clean matrix produces none. **Effort:** ~half a day.

---

## Gap C — `exhaustive` mode flag  *(the enabler; do before/with A–B if gating them)*

**What.** A boolean input gating the heavier passes/artifacts (Gaps A/B/D, plus a hunk-coverage
attestation, IMPROVEMENTS §7) so normal per-PR runs stay cheap. Ref: IMPROVEMENTS §7–§8.

**Where it plugs in.** Follow the `enable_web_search` / `lean_tools` pattern exactly:
1. Declare `exhaustive` input in `action.yml` (near `4-88`); map it in the run-review `env:` block
   (`251-287`).
2. Read it in `main()` beside the other env booleans (`review.py:3145`,`3180-3183`) **or** as
   `parser.add_argument("--exhaustive", action="store_true")` (`~2987`) with
   `${EXHAUSTIVE:+--exhaustive}` appended to argv (`action.yml:302-312`).
3. Thread into `review_context` (`review.py:3197-3207`, mirror `build_succeeded` at `3206`) or a
   module global like `LEAN_TOOLS_ENABLED`, and branch the optional phases on it.

**Acceptance.** Default runs behave identically to today; `exhaustive: true` enables the extra
artifacts/phases. **Effort:** ~1–2 hours (pure plumbing).

---

## Gap D — `SemanticBoundaries.lean` auto-probe  *(highest effort, do last / maybe skip)*

**What.** Auto-generate a Lean probe from admitted-external signatures — for each bound with a
denominator / `Real.sqrt` / floor-ceil / closed regime, emit an obligation (e.g. `example : denom ≠
0`) instantiated at each regime endpoint, compile it, and surface failures. Model = Review B's
`SemanticBoundaries.lean`, which caught the `/0`-collapse. Ref: IMPROVEMENTS §2.

**Why.** The boundary-probe *recipe* is already in the prompts (`review_code_with_spec.md:52`), so
the model is told to check this; a deterministic probe would turn "the model should check" into "the
kernel checked". This is the class the paper-checklist blessed away.

**Where it plugs in / the hard part.** Compilation backends already exist
(`lean_info_extractor.run_lean_command` `114-131`; `lean_tools.LeanToolbox.run "lean_typecheck"`
`139-140`). The **hard part is *generating* the obligations**: extracting denominator/`sqrt`/floor
sub-terms and regime endpoints from a decl's signature is non-trivial term surgery. Pragmatic first
cut: don't parse terms — have the *spec/review agent* (which already reads the statements) emit
candidate boundary obligations as strings into a schema field, then this phase just **compiles**
them deterministically (reusing the `#print axioms` auto-run pattern at
`lean_info_extractor.py:165`, folding results into `LEAN_INFO`/`format_for_review` `320-360`).
Caveat: `_run_lean` prepends `import <module>` and there is no whole-file compile API — pass the
probe as the `code`/`command` string, not a path.

**Acceptance.** A known `/0`-at-endpoint admit (e.g. the BCHKS25-item2 `1−ρ−2δ` denominator)
produces a failing obligation; sound bounds produce none. **Effort:** 1–2 days for the
LLM-emits-obligations cut; substantially more for true automatic term extraction (probably not worth
it — prefer the agent-emitted-obligation route or skip).

---

## Suggested order

C (flag) → A (source ledger) → B (coverage resolution) → D (boundary probe, only if wanted). A and
B are the value; C gates them cheaply; D is optional. None is required — the toolkit is complete and
the review-quality improvements already shipped in the prompts.
