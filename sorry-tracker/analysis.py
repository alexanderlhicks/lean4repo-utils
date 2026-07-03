"""LLM analysis of proof obligations.

Gathers the imported-source context for a file (resolving Lean imports to disk
and concatenating them under a byte budget) and prompts the model for a
contributor-facing explanation: what the statement says, how it fits the
surrounding development, and a high-level proof strategy.
"""

import dataclasses
import re
import sys

from detection import Sorry
from leanrepo_common.lean_utils import import_search_dirs, resolve_import
from leanrepo_common.llm_provider import ContentPart

MAX_IMPORT_FILE_SIZE = 25000      # Max size in bytes for a single imported file.
MAX_TOTAL_IMPORT_BYTES = 300_000  # Cap on the combined size of all imports in one prompt.

_IMPORT_RE = re.compile(r"^import\s+(\S+)")

# Static instructional preamble + worked example. The per-obligation request is
# appended at call time (see generate_ai_analysis).
_PROMPT_PREAMBLE = (
    "You are an expert in Lean 4 and formal mathematics. Your task is to help a "
    "**first-time contributor** — who may know the mathematics but not this codebase — "
    "pick up a proof obligation marked with `sorry`.\n\n"
    "Your response must be a markdown-formatted comment with exactly three sections. "
    "**Do not write the full proof.** Your goal is to orient the contributor and lower the "
    "barrier to making a first attempt. Be concrete: refer to the *actual* names, "
    "hypotheses, and definitions in the provided file and imports — never invent identifiers "
    "you cannot see in the supplied context.\n\n"
    "1.  `### Statement Explanation`: Explain what the theorem/definition states in clear, simple terms. Describe the goal and each hypothesis by name.\n"
    "2.  `### Context`: Explain how this statement relates to other definitions or theorems in the file, imported files, or any provided external references. Cite the specific definitions/lemmas (by name) that the goal depends on, and say whether this is a key lemma feeding a larger result, a generalization, or a bridge between ideas.\n"
    "3.  `### Proof Suggestion`: Provide a high-level, step-by-step suggestion for how to approach the proof. Mention relevant tactics (like `simp`, `rw`, `cases`, `induction`) and name specific lemmas from the provided file/import content that might apply. Do not write the full proof code.\n\n"
    "---\n\n"
    "### Example\n\n"
    "**Full File Content:**\n"
    "```lean\n"
    "import Mathlib\n\n"
    "def is_even (n : ℕ) : Prop :=\n"
    "  ∃ k, n = 2 * k\n\n"
    "theorem even_plus_even (a b : ℕ) (ha : is_even a) (hb : is_even b) : is_even (a + b) := by\n"
    "  sorry\n"
    "```\n\n"
    "**Declaration with `sorry`:**\n"
    "```lean\n"
    "theorem even_plus_even (a b : ℕ) (ha : is_even a) (hb : is_even b) : is_even (a + b) := by\n"
    "  sorry\n"
    "```\n\n"
    "**Your Ideal Response:**\n"
    "```markdown\n"
    "### Statement Explanation\n"
    "This theorem states that for any two natural numbers `a` and `b`, if both `a` and `b` are even, then their sum `a + b` is also even.\n\n"
    "### Context\n"
    "This is a fundamental property of even numbers and relies on the definition `is_even` provided in the same file. It's a basic building block for number theory proofs.\n\n"
    "### Proof Suggestion\n"
    "1.  Start by using the `unfold is_even` tactic to expand the definition of `is_even` in the hypotheses `ha` and `hb` and the goal.\n"
    "2.  This will give you two witnesses, let's say `k_a` and `k_b`, such that `a = 2 * k_a` and `b = 2 * k_b`.\n"
    "3.  Substitute these equations into the goal `is_even (a + b)`.\n"
    "4.  The goal will become `∃ k, 2 * k_a + 2 * k_b = 2 * k`.\n"
    "5.  Use the `ring` tactic or factor out the 2 to show that you can provide `k_a + k_b` as the witness for the existential quantifier.\n"
    "```\n\n"
    "---\n\n"
)


def find_and_read_imports(file_content: str, search_dirs: "list[str]") -> str:
    """Resolve a file's Lean imports to disk and return their concatenated content.

    Files at or above MAX_IMPORT_FILE_SIZE are skipped individually; once the
    running total reaches MAX_TOTAL_IMPORT_BYTES the remaining imports are
    dropped so a single file's `import` block can't blow up the prompt.
    """
    imported_content = []
    total_bytes = 0
    for line in file_content.splitlines():
        match = _IMPORT_RE.match(line)
        if not match:
            continue

        import_path_str = match.group(1)
        full_path = resolve_import(import_path_str, search_dirs)
        if not full_path:
            print(f"⚠️  Could not find imported file for: {import_path_str}")
            continue

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"⚠️  Could not read import file {full_path}: {e}", file=sys.stderr)
            continue

        if len(content) >= MAX_IMPORT_FILE_SIZE:
            print(f"⚠️  Skipping large import: {import_path_str}")
            continue
        if total_bytes + len(content) > MAX_TOTAL_IMPORT_BYTES:
            print(
                f"⚠️  Import context budget ({MAX_TOTAL_IMPORT_BYTES} bytes) reached; "
                f"omitting remaining imports starting at {import_path_str}."
            )
            break
        total_bytes += len(content)
        imported_content.append(f"\n---\n-- Content from: {import_path_str}\n---\n{content}")

    return "".join(imported_content)


class ImportContext:
    """Resolves and caches each file's imported-source context.

    Import search directories are scanned once per repository; each file's
    imports are read at most once (a file's context is independent of how many
    sorries it contains). Build it on the main thread before fanning out.
    """

    def __init__(self, repo_root: str):
        self._search_dirs = import_search_dirs(repo_root)
        self._cache: "dict[str, str]" = {}

    def for_file(self, file_path: str, file_content: str) -> str:
        if file_path not in self._cache:
            self._cache[file_path] = find_and_read_imports(file_content, self._search_dirs)
        return self._cache[file_path]


def _build_prompt(sorry: "Sorry", imports_context: str, has_references: bool) -> str:
    """Assemble the full prompt for one obligation from the static preamble."""
    location_section = f"**Location:** {sorry.file_path}:{sorry.line_num}\n\n"
    reference_section = (
        "**External Reference Content:** provided as attached document(s) above. "
        "Use them for the Context section.\n\n"
        if has_references else ""
    )
    return (
        _PROMPT_PREAMBLE
        + "### User Request\n\n"
        + location_section
        + f"**Full File Content:**\n```lean\n{sorry.full_content}\n```\n\n"
        + f"**Imported Files Content:**\n```lean\n{imports_context}\n```\n\n"
        + reference_section
        + f"**Declaration with `sorry`:**\n```lean\n{sorry.snippet}\n```"
    )


def generate_ai_analysis(
    provider,
    model: str,
    sorry: "Sorry",
    imports_context: str,
    reference_parts: "list[ContentPart]",
) -> str:
    """Ask the LLM for a contributor-facing analysis of one obligation.

    Returns the markdown analysis, or an empty string if the API call fails (a
    single failed obligation must not abort the whole run).
    """
    print(f"🤖 Calling {model} (via OpenRouter) for detailed analysis...")
    try:
        prompt = _build_prompt(sorry, imports_context, bool(reference_parts))

        # Reference docs first (a shared, prompt-cache-marked prefix reused
        # across every sorry), then the per-sorry prompt. Copy parts so the
        # cache flag is safe to set from the parallel analysis threads.
        contents: "list[ContentPart]" = []
        if reference_parts:
            last = len(reference_parts) - 1
            contents.extend(
                dataclasses.replace(part, cache=(i == last))
                for i, part in enumerate(reference_parts)
            )
        contents.append(ContentPart(type="text", data=prompt))

        text, _usage = provider.generate_text(model=model, contents=contents)
        return text
    except Exception as e:
        print(
            f"⚠️ Warning: LLM API call failed for this proof obligation.\n   Error: {e}",
            file=sys.stderr,
        )
        return ""
