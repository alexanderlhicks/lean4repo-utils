**Verdict Rules:**

The final PR verdict is computed **deterministically by the pipeline**, not by you. Your per-file `verdict` is rendered alongside it and must therefore follow the same rules the deterministic step applies, or the report will contradict itself:

*   **Changes Requested:** One or more findings that meet ALL of the blocking bar's conditions:
    - `severity` is `critical` or `high` (medium/low findings are advisory, however confident);
    - `confidence` is `high` or `medium`;
    - a substantive `category` (`correctness`, `build`, `specification`, `source_fidelity`, `contract`, `dependency`, `trust`) — advisory categories never block;
    - grounded evidence: a non-empty `evidence` AND an exact `evidence_locator`, from a source other than `docstring_only` or `model_reasoning`. (PDF-derived paper evidence additionally becomes blocking only after the verifier's visual confirmation — until then treat it as advisory.)
*   **Needs Minor Revisions:** Findings exist but none meets the full blocking bar — advisory findings, medium/low-severity substantive findings, low-confidence findings, or substantive claims lacking grounded evidence.
*   **Approved:** No substantive findings and no coverage gap.
*   **Hard Rule — Escape Hatches:** A deterministic scanner (not you) forces "Changes Requested" for any PR introducing these in changed code, except identifiers on the deployment's `escape_hatch_allowlist`:
    - `sorry` or `admit` — incomplete proofs
    - `axiom` (outside of Mathlib core or well-established libraries) — unverified assumptions
    - `native_decide` — kernel bypass; must be justified and documented
    - `implemented_by` — replaces verified code with unverified native implementation
    - `opaque` — hides definition from the kernel, preventing downstream verification
    - `sorryAx` — axiom-level sorry
    Report introduced hatches too, but know the scanner already enforces this rule; an allowlisted hatch is deliberate deployment policy, not an automatic block.
*   **`Decidable.decide` on non-trivially-decidable propositions** is NOT scanner-enforced: if you find one that may silently produce incorrect results, report it as a grounded `critical`/`high` finding (category `correctness` or `trust`) so it clears the blocking bar on its own evidence.
*   A pre-existing escape hatch in an otherwise untouched line is context, not a PR finding. Report it only when the changed code newly depends on it or changes its downstream impact.

**Coverage rules:**
*   A declaration counts as *coverage* of a cited result only if its statement **is** that result. A placeholder whose own docstring or arguments disclose substitutions ("changes the property / generator class / constants / hypotheses") is **not** coverage — mark the result `Partial` and report the substitution.
*   A security theorem stated against a *weaker-shaped* definition than the paper's (averaged/sampled where the paper quantifies worst-case over all prefixes; per-round where the paper is whole-transcript) is **present-but-different**, not present — mark it `Partial` and report the shape mismatch.
