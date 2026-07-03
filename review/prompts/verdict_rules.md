**Verdict Rules:**
*   **Approved:** No findings in the "Critical Misformalizations" or "Lean 4 / Mathlib Issues" sections.
*   **Needs Minor Revisions:** Only findings in the "Nitpicks" section.
*   **Changes Requested:** One or more findings in "Critical Misformalizations" or "Lean 4 / Mathlib Issues".
*   **Hard Rule — Escape Hatches:** Any PR containing the following MUST receive a "Changes Requested" verdict, regardless of other findings:
    - `sorry` or `admit` — incomplete proofs
    - `axiom` (outside of Mathlib core or well-established libraries) — unverified assumptions
    - `native_decide` — kernel bypass; must be justified and documented
    - `implemented_by` — replaces verified code with unverified native implementation
    - `opaque` — hides definition from the kernel, preventing downstream verification
    - `sorryAx` — axiom-level sorry
    - `Decidable.decide` on non-trivially-decidable propositions — may silently produce incorrect results