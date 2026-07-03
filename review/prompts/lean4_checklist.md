### Lean 4 Best Practices & Potential Pitfalls
1. **Typeclass Assumptions:** Are the typeclass assumptions minimal and correct? Watch for overly strong assumptions (e.g., using `[CommRing R]` when `[Semiring R]` or `[Monoid R]` suffices). Also flag cases where the typeclass assumption is *too weak* for the theorem to be correct—i.e., the statement type-checks but is mathematically false under the weaker assumption.
2. **Implicit vs. Explicit Arguments:** Are `{}` (implicit), `()` (explicit), and `[]` (instance) arguments used correctly? Types and typeclasses should almost always be implicit or instances.
3. **`Prop` vs. `Type`:** Is there a misuse of the type hierarchy? Are propositions properly placed in `Prop` rather than `Type`?
4. **Universe Levels:** Are definitions unnecessarily restricted to `Type` when they should be universe polymorphic (`Type u`, `Type v`)? 
5. **Simp Lemmas:** If `@[simp]` is used, is the lemma actually a good simp lemma? (Does the LHS simplify to a strictly simpler RHS? Is the LHS in normal form?)
6. **Computability:** Does the definition unnecessarily use `noncomputable` where a computable alternative exists in Mathlib? Conversely, does it unnecessarily twist itself to be computable when classical tools (`Classical.choice`, `Classical.em`) are idiomatic for the domain?
7. **Naming Conventions:** Do the definitions and lemmas follow standard Lean 4 / Mathlib conventions? (`camelCase` for variables/defs, `UpperCamelCase` for types/classes, `snake_case` for theorems/proofs).
8. **Escape Hatches & Kernel Bypasses (Critical):** The following constructs bypass or weaken Lean's kernel verification. All must be flagged:
    - `sorry` or `admit` — incomplete proofs; the most common escape hatch
    - `axiom` declarations (outside Mathlib core or well-established libraries) — introduces unverified assumptions; functionally equivalent to `sorry` for downstream proofs
    - `native_decide` — kernel bypass; verify the proposition is genuinely decidable and that bypassing is intentional and documented
    - `implemented_by` — replaces the verified implementation with unverified native code
    - `opaque` — hides the definition body from the kernel, blocking downstream reduction and verification
    - `sorryAx` — axiom-level sorry, often auto-generated
    - `Decidable.decide` on non-trivially-decidable propositions — may silently produce `Decidable.isFalse` for true statements
9. **Project Context Adaptation:** Adapt checklist items to the project's domain. For example, in a project using `Field` and `Fintype` (e.g., cryptographic formalization), focus typeclass checks on those rather than Mathlib-specific patterns like `CommRing` vs. `Semiring`. Consider what algebraic structures and proof patterns are idiomatic for the specific project.