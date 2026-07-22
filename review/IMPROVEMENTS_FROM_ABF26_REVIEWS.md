# Toolkit improvements distilled from two deep ABF26 (PR #505) reviews — 2026-07-18

Two independent, exhaustive manual reviews of a large (+16k line) formalization-of-a-paper
PR were run at identical pins:

- **Review A** — 7-phase orchestrated fan-out (~110 agents): deterministic ledgers →
  file-centric line review with attested 100% hunk coverage → two-sided paper↔Lean walk →
  sorry adjudication + constructivity + prize red-team → adversarial default-refute
  verification → verdict.
- **Review B** — frozen-provisional-then-reconciled: `phase0.py` inventory → per-file +
  source-ledger dispositions → **kernel semantic-boundary probes** → historical comparison
  (findings SHA-256-frozen *before* reading prior reviews) → verdict.

Both converged on the same facts; B reached BLOCK where A reached GO-WITH-FIXES, purely on
weighting. The value here for the CI action (`review/`) is the **classes of defect each
methodology caught that the current pipeline would likely miss**, and which are cheap enough
to fold into an automated, cost-bounded reviewer. This doc is recommendations, not code.

The current pipeline (see `README.md`) already does a lot right that both reviews relied on:
Agent-A checklist from the paper, per-file faithfulness (hypotheses stronger/weaker,
conclusion weaker), `lean_tools` grounding, a different-family verifier, `#print axioms`
extraction, deterministic verdict, docstring-as-untrusted. Keep all of that. The gaps below
are the *specific* misformalization classes that slipped past a paper-checklist-driven review.

---

## 1. Source-of-source fidelity — the single highest-value gap

**What happened.** The paper being formalized (ABF26) *cites* other papers (BCHKS25, CS25,
GG25, GCXK25, CZ25) for its admitted external lemmas. The worst bugs were where the Lean
statement — and often ABF26's own restatement — **differs from the ORIGINAL cited source**,
not from ABF26. Examples (all confirmed): a dropped `p < Δ_C` radius hypothesis (GCXK25 Thm 3);
a real `δ < minDist/n` substituted for the source's integer `f < n−k−1` + field margin
`ε < (q−n)/(kq)` (CS25 Thm 2); a `/0` singular endpoint where the source keeps a finite-length
`−1/n` margin below half-distance (BCHKS25 Thm 1.3); dropped folded-orbit/`ω≠0` invariants
(GG25 Cor 4.10); an invented mixed `⌈⌉/⌊⌋` rounding of a one-integer-parameter theorem (CZ25).

**Why the current pipeline misses it.** Agent A builds the "Formalization Checklist" from the
*paper under review*. A checklist item that faithfully mirrors ABF26 will bless a Lean
statement that faithfully mirrors ABF26 — even when ABF26 itself weakened the source. The
faithfulness check compares Lean ↔ paper, not Lean ↔ paper's-cited-source.

**Recommendation.**
- When a changed declaration is an **admitted external** (tagged `sorry` + a citation like
  `[BCHKS25 Thm 1.3]` in docstring/name), treat the *cited source*, not the paper under review,
  as ground truth for that statement. Agent A already reads external PDFs — extend the
  checklist schema with, per admitted-external item: `cited_source`, `source_hypotheses`,
  `source_conclusion`, and a `deviations_from_source` field. Fetch the cited PDF if a path/URL
  is discoverable (the action already fetches URLs from `/review` and `spec_refs`).
- Add a dedicated **source-fidelity finding category** and a per-admitted-external verify
  question: "Is every hypothesis of the *cited source theorem* present in the Lean statement?
  Is any inequality relaxed (strict→non-strict, integer→real)?" This is exactly the class the
  paper-checklist blesses away.
- Emit a **Source Ledger** artifact (Review B's `SOURCE_LEDGER.md` is the model): one row per
  admitted `sorry`, `{decl, cited source, disposition: shape-pass | fail | translation-gap}`.

## 2. Regime-boundary / degenerate-parameter evaluation (kernel probe)

**What happened.** BCHKS25-item2 admits a formula with `1 − ρ − 2·δ_fld` in a denominator and
a regime `δ_fld ≤ (1−ρ)/2` that includes the point where that denominator is **0**; Lean's
totalized `/0 = 0` silently collapses a `max` branch. Several other admits were false only at
a boundary (list `{f}` at small δ, trivial code at `ℓ=1`, `ω=0`). Review B caught the `/0`
with a **kernel probe** (`SemanticBoundaries.lean`) that literally evaluates the expression at
the endpoint; Review A caught the false BCGM25 admit by exact-arithmetic counterexample.

**Why the current pipeline misses it.** Reviewers read statements; they rarely *evaluate* them
at the worst parameter point, and totalized division/`Real.sqrt`/floor hide singularities.

**Recommendation.**
- Add a **boundary-probe recipe** to the reviewer + verifier prompts for any bound with a
  denominator, `Real.sqrt`, floor/ceil, or a closed regime interval: instantiate the statement
  at each regime endpoint and at degenerate objects (`⊥` code, `ℓ=1`, `δ=0`, `δ=δ_min`,
  `ω=0`) and check for `/0`-collapse, vacuity, or falsity. `lean_tools` can do this today
  (`lean_check`/`lean_typecheck` a small `example` that evaluates the expression at the point).
- Optionally, a deterministic pre-pass could auto-generate a `SemanticBoundaries.lean` probe
  from admitted-external signatures (denominator sub-terms → `example : denom ≠ 0` obligations),
  compile it, and surface failures — the same way `#print axioms` is auto-run today.

## 3. Security-definition *shape* comparison (averaged vs worst-case)

**What happened.** L6.8 targets ArkLib's `rbrKnowledgeSoundness`, which **samples the prefix
inside the game and bounds the mixture (average)**, whereas the paper's Definition A.5 fixes
*every* prefix and bounds the next-challenge probability (worst case). The error constant is
the same and the direction is safe (averaged ≤ worst-case), so it "type-checks as faithful" —
but it proves a strictly weaker property than the paper's definition.

**Why the current pipeline misses it.** The faithfulness check compares hypotheses and
conclusion of a *theorem*; it does not compare the *quantifier structure of the security
definition* the theorem is stated against.

**Recommendation.** Add a checklist item for any RBR / soundness / knowledge-soundness /
extractability theorem: "Does the ArkLib security *definition* used quantify the way the
paper's definition does — ∀-fixed-prefix worst-case vs sampled/averaged; per-round vs
whole-transcript? If not, the theorem is `present-but-different`, not `present`." Feed the
paper's definition text (not just the theorem) into the check.

## 4. Semantic-reduction trace of the headline quantity

**What happened.** The competition's advertised quantity `bestProvableError` is a custom
infimum over a *chosen family of upper-bound formulas*; a `SecurityUpperBound` entry
**lower-bounds that upper bound**, which is not a lower bound on actual protocol soundness.
The math is internally sound; what it *measures* is an analysis frontier, not soundness. One
review flagged this as the top blocker; the other verified the anchors sound *within the
definition*. Both are right — and only surfaced by following the definitional chain from the
headline claim down to primitives.

**Why the current pipeline misses it.** Per-file review sees `caUpperBoundAttack : SecurityUpperBound`
and checks its inequality; it does not ask "what does `SecurityUpperBound`/`bestProvableError`
*reduce to*, and does that equal the property being advertised?"

**Recommendation.** For a PR that defines a headline security/soundness metric, add a
cross-file task: **trace the definitional chain** from the top-level claimed quantity to its
primitives and state, in one sentence, the actual property proved; then compare that sentence
to the PR's advertised claim. Divergences (analysis-bound vs true-soundness; averaged vs
worst-case; language-membership vs knowledge) are findings even when every lemma is correct.

## 5. Constructivity / carrier rubric as a first-class check

**What happened.** The knowledge-soundness extractor is `Classical.choice` on an existence
proposition — it proves a witness *exists* but is neither the paper's deterministic
erasure-decoder nor carries its `O(enc+ecor)` runtime; discrete data (grid indices, error
counts, exact prize rates, `2^-128`) were modeled as `noncomputable NNReal`/`Real`; a
competition *answer* was a `Prop` rather than a witness-returning structure. Both reviews
ran a dedicated constructivity pass (A's X-4b, B's `CONSTRUCTIVITY_AUDIT.md`).

**Why the current pipeline half-misses it.** The current best-practices check flags
`Classical.choice` "where the paper's argument is constructive" as a *proof-strategy note*.
That is too weak for two cases: (i) an extractor/algorithm claimed to be *efficient/deterministic*
must be computable data + a cost model, not choice; (ii) a value that will be *submitted or
checked as a competition answer* must be data (Nat/Rat/structure), not a Prop or a
noncomputable real.

**Recommendation.** Add a small **carrier rubric** to the Lean-4 checklist:
- error counts / grid indices / list sizes / dimensions → `Nat`/`Int` with explicit
  inequalities; rational normalization only at boundaries.
- exact prize rates / thresholds → exact rational (`NNRat` / numerator-denominator naturals),
  decidable/executable comparison.
- a submitted resolution/answer → a witness-returning data structure, not a `Prop`.
- an extractor/algorithm with a *runtime/efficiency claim* → computable function + verified
  spec + cost data; `Classical.choice` is a finding here, not a note.
- `NNReal`/`ENNReal`/`Real` are fine at analytic theorem boundaries once discrete inputs are
  fixed.
Also flag totalized-partial-function traps that leak into interfaces: `bitsOfSecurity 0 = 0`
(should be ∞/perfect), unrestricted-real "bits" fields (allow negatives).

## 6. Coverage non-vacuity (a decl can be "coverage" and not be the theorem)

**What happened.** A placeholder whose **own docstring** says it "changes the property,
generator class, constants, and hypotheses" was counted as coverage of the cited theorem; and
it was in fact *false* for large parameters. Separately, a coverage matrix listed decls that
do not exist in the tree (renamed/out-of-tree) as present.

**Recommendation.**
- Coverage of a paper result requires the admitted statement to *be* that result: add a
  verify question "Does this declaration's statement match the cited theorem, or does its own
  docstring/args disclose substitutions?" A self-disclosed non-faithful placeholder is **not**
  coverage.
- If the repo maintains a coverage/audit matrix, add a deterministic check that every declared
  symbol in it **resolves** (`lean_print`) and its claimed `sorry`-status matches
  `#print axioms` — the same grounding already applied to build claims. (Both reviews found
  stale/phantom matrix rows.)

## 7. Process patterns worth adopting (beyond per-PR CI)

These are cheap to state and raise the ceiling on *deep* reviews (a `/review` with
`additional_comments` asking for an exhaustive pass, or a scheduled audit), even if not every
PR run does them:

- **Anti-anchoring freeze.** Review B froze its findings (SHA-256) *before* reading prior
  reviews, then reconciled — and correctly *withdrew 3* provisional findings while keeping 9.
  The current different-family verifier reduces false positives on a shared finding set; a
  freeze-then-reconcile step reduces *anchoring* to prior verdicts (a different error). For
  re-reviews, instruct the pipeline to derive findings from source first and only then diff
  against the previous review's fix-list.
- **Coverage attestation.** Both reviews produced machine-checked "every changed hunk/line was
  read" attestations and found real gaps (one 219-line band a segment reviewer skipped). The
  action already refuses to "Approve" un-reviewed files; consider emitting an explicit
  hunk-coverage attestation artifact for exhaustive-mode runs.
- **Trust-boundary scoping statement.** The two reviews *disagreed on scope* (one red-teamed
  the companion prize repo, one scoped it out) — and both were right to state it. Have the
  verdict explicitly declare what was in/out of the trust boundary (companion repos, CI gates,
  submission harness), so "Approved" is never read as covering an out-of-scope surface.
- **Adversarial default-refute with severity correction.** Review A's verify pass didn't just
  drop/keep — it *corrected severity* (two "critical" prize findings → low after showing the
  private-repo/human-review model defuses them; kept one with an end-to-end PoC). The current
  verifier is confirm/refute/uncertain; adding an optional `corrected_severity` lets the
  precision stage down-rank over-escalations without discarding the underlying true fact.

## 8. Small, concrete prompt/artifact additions (lowest effort, high signal)

- `analyze_spec.md`: for each paper result that is *itself cited from another paper*, record
  the ultimate source and its hypotheses; mark admitted externals for source-fidelity checking.
- `review_code_with_spec.md`: add the boundary-probe recipe (§2), the security-definition-shape
  check (§3), and the carrier rubric (§5) to the faithfulness section.
- `verify_finding.md`: for a source-fidelity finding, require the verifier to inspect the
  *cited source* PDF (not the paper under review) before confirming/refuting; allow an optional
  `corrected_severity`.
- `verdict_rules.md`: a self-disclosed non-faithful placeholder does not count as coverage;
  a security theorem stated against a weaker-shaped definition is `present-but-different`.
- New artifact: `SOURCE_LEDGER.md` (per admitted `sorry`: cited source + disposition) and, in
  exhaustive mode, a `SemanticBoundaries.lean` auto-probe + hunk-coverage attestation.

---

### Why these and not more
Everything above was a *real* defect class in a real PR that a competent paper-checklist review
(the current pipeline's design point) blessed. None requires abandoning the cost-bounded CI
model: §1/§3/§4/§6 are prompt + schema additions; §2/§5 lean on the `lean_tools` already
present; §7/§8 are artifacts and process for exhaustive-mode runs. The one irreducible lesson:
**for formalizations of papers that cite other papers, faithfulness to the paper under review
is necessary but not sufficient — the cited source governs the admitted statement.**
