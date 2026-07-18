import argparse
import contextlib
import dataclasses
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
import urllib3.util.connection as urllib3_connection
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from leanrepo_common.diff_utils import parse_git_diff_header, unquote_git_path
from leanrepo_common.lean_utils import (
    scrub_line, strip_comments_preserve_strings,
    FileCache, file_path_to_module_name,
)
from leanrepo_common.llm_provider import (
    LLMProvider, ContentPart, TokenUsage, create_provider,
    RunHealth, BudgetExceededError, is_hard_llm_failure, _reraise_if_fatal,
    parse_run_budget, describe_exc,
)
from lean_tools import LeanToolbox, lean_available

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

ACTION_PATH = os.path.dirname(os.path.realpath(__file__))


def _read_prompt_file(name: str) -> str:
    """Read a prompt fragment from the prompts/ dir, returning '' if absent."""
    try:
        with open(os.path.join(ACTION_PATH, "prompts", name), "r") as f:
            return f.read()
    except OSError:
        logging.warning(f"Prompt fragment '{name}' not found; proceeding without it.")
        return ""


# Shared operating contract injected ahead of every agent's instructions
# (untrusted-input posture, grounding requirement, confidence calibration).
# Defined once here so the agents stay consistent instead of each redefining it.
OPERATING_CONTRACT = _read_prompt_file("_operating_contract.md")

# --- Pydantic Schemas for Multi-Agent Orchestration ---
class ReferenceMappingEntry(BaseModel):
    paper_result: str = Field(description="The theorem/definition as stated in the paper (section number, statement).")
    mathematical_content: str = Field(description="The precise mathematical content (hypotheses, conclusion, objects) that any correct formalization must preserve.")
    status: Literal["Present", "Missing", "Partial"] = Field(description="Whether the diff contains a corresponding formalization. Use Partial (with deviations_from_source filled) for present-but-different statements.")
    cited_source: str = Field(default="", description="When the paper result is itself cited from another work (an admitted external lemma), the ULTIMATE source (paper + theorem number). The cited source, not the paper under review, governs the admitted statement.")
    deviations_from_source: str = Field(default="", description="For an admitted external: every hypothesis, quantifier shape, or inequality of the cited source's statement that the paper's restatement or the Lean statement drops, relaxes (strict→non-strict, integer→real), or substitutes. Empty when faithful or not applicable.")

class ChecklistItem(BaseModel):
    concept: str = Field(description="The mathematical concept or theorem name.")
    verification_steps: list[str] = Field(description="List of specific things to check for to avoid misformalization.")
    severity: Literal["Critical", "Major", "Minor"] = Field(description="Severity of this item: 'Critical', 'Major', or 'Minor'")

class SpecChecklist(BaseModel):
    reference_mapping: list[ReferenceMappingEntry] = Field(default_factory=list, description="Paper Result → Expected Lean Statement mapping table.")
    items: list[ChecklistItem] = Field(description="List of checklist items derived from the specification.")

# --- Agent B: Per-File Review Schema ---
class ChecklistResult(BaseModel):
    item: str = Field(description="The checklist item being verified.")
    status: Literal["satisfied", "violated", "unclear"] = Field(description="Whether the code satisfies, violates, or is unclear on this item.")
    explanation: str = Field(description="Brief explanation of the status.")

class Finding(BaseModel):
    description: str = Field(description="Description of the finding.")
    location: str = Field(default="", description="File path and line/range if applicable, e.g. 'MyFile.lean:42' or 'MyFile.lean:42-55'.")
    evidence: str = Field(default="", description="What grounds this finding: the specific paper section/checklist item, the repository symbol or definition being misused, or the compiler/toolchain output it rests on. Cite specifics so a human can verify it independently.")
    evidence_source: Literal["compiler", "kernel", "paper_or_spec", "trusted_repo_reference", "lean_source", "downstream_contract", "docstring_only", "model_reasoning"] = Field(default="model_reasoning", description="The primary source of truth for this finding. Docstrings are intent metadata, never sufficient evidence for a blocking correctness finding; model_reasoning is advisory only.")
    evidence_locator: str = Field(default="", description="Exact locator for the evidence source: command/output, paper section/page/statement, declaration and line, ArkLib/component path, or downstream consumer line.")
    evidence_medium: Literal["pdf", "tex", "markdown", "plain_text", "lean", "compiler", "kernel", "repository", "downstream", "unknown"] = Field(default="unknown", description="The medium containing the cited evidence. PDF-derived paper evidence requires visual confirmation by the verifier; logical section/theorem anchors are preferred over page coordinates.")
    confirmation_method: Literal["unconfirmed", "visual", "text", "compiler", "kernel", "downstream"] = Field(default="unconfirmed", description="Internal confirmation method set by the verifier, never by the initial reviewer. PDF evidence is blocking only after visual confirmation.")
    verification_status: Literal["unverified", "confirmed"] = Field(default="unverified", description="Internal precision-stage status. The adversarial verifier sets this to confirmed; do not set it based on the initial review alone.")
    confidence: Literal["high", "medium", "low"] = Field(default="medium", description="Your confidence that this finding is genuinely correct and not a false positive.")
    suggested_fix: str = Field(default="", description="Suggested fix or corrected code snippet, if applicable.")
    severity: Literal["critical", "high", "medium", "low"] = Field(default="medium", description="Impact if true: critical/high findings are blockers, medium/low findings are advisory unless the deterministic rules say otherwise.")
    category: Literal["correctness", "build", "specification", "source_fidelity", "contract", "dependency", "trust", "style", "generalization", "proof", "documentation"] = Field(default="correctness", description="Primary finding category. Use source_fidelity when a Lean statement (or the paper's own restatement) deviates from the ORIGINAL cited source of an admitted external result. Use style/generalization/proof/documentation for advisory feedback rather than presenting it as a correctness issue.")
    disconfirming_check: str = Field(default="", description="A concrete check that could show this finding is false, such as a compiler command, declaration lookup, or specification passage.")
    how_to_confirm: str = Field(default="", description="The shortest actionable way for a maintainer to confirm and resolve this finding.")

class FileReview(BaseModel):
    analysis: str = Field(default="", description="Step-by-step analysis BEFORE findings: (1) What does the changed code do mathematically? (2) How do changes relate to the spec checklist? (3) What are the riskiest aspects? (4) Any ambiguities in mathematical intent?")
    verdict: Literal["Approved", "Needs Minor Revisions", "Changes Requested"] = Field(description="The verdict for this file.")
    checklist_results: list[ChecklistResult] = Field(default_factory=list, description="Checklist verification results (only when spec checklist provided).")
    critical_misformalizations: list[Finding] = Field(default_factory=list, description="Mathematical errors, broken assumptions, missing hypotheses.")
    lean_issues: list[Finding] = Field(default_factory=list, description="Lean 4 / Mathlib idiom violations, typeclass issues, escape hatches.")
    nitpicks: list[Finding] = Field(default_factory=list, description="Naming, style, minor cleanups.")
    coverage_incomplete: bool = Field(default=False, description="Internal pipeline flag set when a large file could only be partially reviewed. Do not set this; always leave it false.")

# --- Cross-File Analysis Schema ---
class CrossFileAnalysis(BaseModel):
    analysis: str = Field(default="", description="Trace the main composition chains across files BEFORE reporting issues. Identify type-flow paths, axiom propagation chains, and external dependency interfaces.")
    composition_issues: list[Finding] = Field(default_factory=list, description="Issues with how files connect: type mismatches, broken composition chains.")
    escape_hatch_impact: list[Finding] = Field(default_factory=list, description="Axioms/sorries and their downstream impact through the dependency chain.")
    external_dependency_issues: list[Finding] = Field(default_factory=list, description="Incorrect usage of external library APIs.")
    missing_cross_file_verification: list[Finding] = Field(default_factory=list, description="Spec items requiring multi-file coordination that lack it.")

# --- Synthesis Schema ---
class SynthesisSummary(BaseModel):
    tldr: str = Field(description="1-2 sentence executive summary of the PR state.")
    precheck_summary: str = Field(description="Summary of mechanical pre-check results.")
    checklist_coverage: str = Field(default="", description="How well the PR covers the specification checklist.")
    cross_file_summary: str = Field(default="", description="Summary of cross-file analysis findings.")
    critical_misformalizations: list[Finding] = Field(default_factory=list, description="Aggregated critical misformalizations.")
    key_lean_issues: list[Finding] = Field(default_factory=list, description="Grouped/deduplicated Lean issues across files.")
    overall_verdict: Literal["Approved", "Needs Minor Revisions", "Changes Requested"] = Field(description="The overall PR verdict.")

# --- Triage Schema ---
class ReviewCluster(BaseModel):
    name: str = Field(description="Short descriptive name for the cluster.")
    files: list[str] = Field(description="File paths in this cluster.")
    review_question: str = Field(description="The key cross-file question to answer for this cluster.")
    priority: Literal["critical", "high", "medium", "low"] = Field(description="Priority of this cluster.")
    review_strategy: str = Field(default="", description="Detailed review strategy: what mathematical properties to verify, what cross-file interactions to check, specific concerns about potential issues.")
    key_hypotheses: list[str] = Field(default_factory=list, description="Specific testable hypotheses for the per-file reviewer to verify or falsify.")

class TriageResult(BaseModel):
    clusters: list[ReviewCluster] = Field(description="Review clusters ordered by priority.")


# --- Verification Schema ---
class FindingVerdict(BaseModel):
    verdict: Literal["confirmed", "refuted", "uncertain"] = Field(
        description="'confirmed' = the finding is a real issue; 'refuted' = the finding is wrong / a false positive; 'uncertain' = cannot be determined from the provided context."
    )
    reasoning: str = Field(description="Brief justification, citing the specific code or specification that confirms or refutes the finding.")
    confirmation_method: Literal["unconfirmed", "visual", "text", "compiler", "kernel", "downstream"] = Field(default="unconfirmed", description="How the verifier confirmed the finding. Use visual for PDF-derived evidence; do not claim visual confirmation without inspecting the original PDF representation.")
    corrected_severity: Literal["", "critical", "high", "medium", "low"] = Field(default="", description="Optional: when the finding is CONFIRMED as a true fact but the reviewer over-escalated its impact, the corrected (lower) severity. The pipeline applies corrections downward only — a verifier can never raise severity — and ignores this field unless verdict is confirmed.")


# --- Token Usage Tracking ---
class TokenTracker:
    """Tracks cumulative token usage across all API calls (thread-safe)."""
    def __init__(self):
        self._lock = threading.Lock()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_thinking_tokens = 0
        self.call_count = 0

    def record(self, usage: TokenUsage):
        """Records token usage from a provider response."""
        with self._lock:
            self.call_count += 1
            self.total_input_tokens += usage.input_tokens
            self.total_output_tokens += usage.output_tokens
            self.total_thinking_tokens += usage.thinking_tokens

    def summary(self) -> str:
        with self._lock:
            total = self.total_input_tokens + self.total_output_tokens + self.total_thinking_tokens
            parts = [f"Token usage: {self.total_input_tokens:,} input + {self.total_output_tokens:,} output"]
            if self.total_thinking_tokens > 0:
                parts.append(f" + {self.total_thinking_tokens:,} thinking")
            parts.append(f" = {total:,} total across {self.call_count} API calls")
            return "".join(parts)

token_tracker = TokenTracker()
file_cache = FileCache()

# Thinking budgets (set by main() from CLI args)
THINKING_BUDGET_HIGH = 10240   # deep analysis agents (Agent A, B, Cross-File)
THINKING_BUDGET_LOW = 2048     # structural agents (Triage, Synthesis)

# Whether agents may call the Lean toolchain (set by main() — enabled when
# requested and `lake` is available). See _make_toolbox.
LEAN_TOOLS_ENABLED = False

# --- Per-run spend control + loud-on-failure (C3) ---
# Env-var NAMES the entrypoint reads. Module constants so a test can import them and
# assert action.yml wires the EXACT same names (a LLM_MAX_RUN_TOKEN vs _TOKENS typo
# would otherwise ship the whole feature dark). Operator-config only — sourced from
# the workflow/secrets env, NEVER from the untrusted PR checkout.
ENV_MAX_RUN_TOKENS = "LLM_MAX_RUN_TOKENS"
ENV_MAX_RUN_COST = "LLM_MAX_RUN_COST"
ENV_BUDGET_MODE = "LLM_BUDGET_MODE"
ENV_LOUD_EXIT = "LLM_LOUD_EXIT"
ENV_BUILD_SUCCEEDED = "BUILD_SUCCEEDED"
ENV_BUILD_STATUS = "BUILD_STATUS"
# Process exit code when loud-exit is enabled AND the run degraded. Applied ONLY at
# the entrypoint via sys.exit(main()) — never inside a finally, and only after the
# comment has been written. Do NOT set this action as a required check with loud-exit
# on (a spend/quota outage would then block merges).
LOUD_EXIT_CODE = 2
# The health flag file review.py writes for the action's shell step to read (its
# stdout is the PR-comment channel, so it cannot print ::error:: itself).
REVIEW_HEALTH_FILE = "review_health.json"
# Per-run health tracker; a fresh one is installed at the top of main(). Module-global
# so the ThreadPool worker functions can record into it (thread-safe).
run_health = RunHealth()

# FIXED loud-failure strings (no interpolation — they render in the PR comment and,
# for the annotation, in the checks UI / a workflow command; dynamic content there is
# an injection channel). At most a status-code integer is ever added, never a message.
_LOUD_BANNER = (
    "> [!CAUTION]\n"
    "> **This review did not complete normally.** One or more AI calls failed for a "
    "spend, quota, or authentication reason, or the per-run budget was exhausted. The "
    "results below are PARTIAL and must not be read as a clean review — see the Actions "
    "log for details."
)
_LOUD_ANNOTATION = (
    "AI review degraded: an LLM spend/quota/auth failure or per-run budget exhaustion "
    "left the review incomplete — results are partial (see the run log)."
)


def _safe_md_path(s: str) -> str:
    """Neutralise markdown-breaking characters in a PR-author-controlled file path
    before it is rendered inside a code span in the bot's comment (git permits
    backticks and newlines in path names, which could otherwise break out of the span
    and inject attacker-authored markdown into a comment the review bot appears to own)."""
    return s.replace("`", "").replace("\n", " ").replace("\r", " ")


def _skipped_marker() -> str:
    """A deterministic, orchestration-derived list of files skipped by a budget trip.
    Pure run state — never LLM text — so a model-emitted fake 'budget exceeded'
    narrative cannot impersonate it."""
    if not run_health.skipped_files:
        return ""
    files = ", ".join(f"`{_safe_md_path(fp)}`" for fp in run_health.skipped_files)
    return f"\n> **Skipped (per-run budget):** {files}\n"


def _write_review_health() -> None:
    """Write the health flag file for the action's shell step to read. review.py's own
    stdout is the PR-comment channel (redirected to $GITHUB_OUTPUT), so it cannot print
    the ::error:: workflow command itself — the shell step emits it from this file."""
    try:
        with open(REVIEW_HEALTH_FILE, "w") as f:
            json.dump({
                "degraded": run_health.degraded,
                "budget_exceeded": run_health.budget_exceeded,
                "hard_failures": run_health.hard_failures,
            }, f)
    except Exception as e:
        logging.warning(f"Failed to write review health flag: {describe_exc(e)}")


def _review_comment_header(additional_comments: str) -> str:
    """Header for a posted review. Distinguishes a GUIDED review — the reviewer supplied
    ``/review`` instructions, laid out so the review can be read against them — from an
    UNGUIDED one (plain ``/review``: the "naive" baseline, grounded only on auto-pulled
    context). Also stamps the reviewed commit (from the S1-resolved ``PR_HEAD_SHA``) for
    auditability and multi-review continuity. Replaces the old "Initial vs. not-initial"
    label, which was a trigger proxy, not what a reader needs.

    The instructions are rendered as a blockquote (each line prefixed) so multi-line text
    stays contained; in the pilot they come from a commenter-gated (trusted) ``/review``.
    """
    instr = (additional_comments or "").strip()
    head = (os.environ.get("PR_HEAD_SHA") or "").strip()
    stamp = f"*Reviewed at commit `{head[:12]}`.*\n\n" if head else ""
    if instr:
        title = "### 🤖 AI Review (with additional instructions)\n\n"
        quoted = "\n".join(f"> {ln}" for ln in instr.splitlines())
        body = f"> **Review instructions:**\n{quoted}\n\n"
    else:
        title = "### 🤖 AI Review\n\n"
        body = ("*Unguided review — no extra instructions; grounded only on the diff, the "
                "repository dependency graph, and any cited references.*\n\n")
    return title + stamp + body


_MANIFEST_REPO_CAP = 25  # cap the repo-context list so a large dep-graph doesn't bloat the comment


def _render_reference_manifest(records: "Optional[List[ContextRecord]]") -> str:
    """Deterministic 'References & context used' section (no LLM call).

    Lists the context the reviewers were GIVEN — external references fetched, KB/spec
    files loaded, and repository/dependency files — as an inert code-span list, so a
    reader can see exactly what grounded the review (and, for the pilot, whether the KB
    was pulled). Load FAILURES are NOT repeated here — they remain in the separate
    'Context Warnings' block, so each item appears once (single source). KB/external
    references are handed to the model intact (they are not in `_TRIMMABLE_KEYS`), so
    listing them here is faithful. Repository/dependency context, by contrast, IS in
    `_TRIMMABLE_KEYS` — under budget pressure some listed files may be trimmed from the
    prompt, so that section is labelled "provided" (not "guaranteed used"). Rendered on
    both the happy and the degraded paths."""
    loaded = [r for r in (records or []) if r.ok]
    if not loaded:
        return ""
    ext = [r.ref for r in loaded if r.category == "external"]
    spec = [r.ref for r in loaded if r.category == "spec"]
    repo = [r.ref for r in loaded if r.category == "repo"]
    out = ["\n<details><summary>📚 <b>References &amp; context used</b></summary>\n"]
    if ext:
        out.append("\n**External references fetched:**")
        out.extend(f"- `{_safe_md_path(u)}`" for u in ext)
    if spec:
        out.append(f"\n**Knowledge base / specification ({len(spec)}):**")
        out.extend(f"- `{_safe_md_path(p)}`" for p in spec)
    if repo:
        out.append(f"\n**Repository context provided ({len(repo)} file(s) from the dependency "
                   "graph; large sets may be trimmed to fit the model's budget):**")
        out.extend(f"- `{_safe_md_path(p)}`" for p in repo[:_MANIFEST_REPO_CAP])
        if len(repo) > _MANIFEST_REPO_CAP:
            out.append(f"- …and {len(repo) - _MANIFEST_REPO_CAP} more")
    out.append("\n</details>\n")
    return "\n".join(out)


def _emit_degraded_review(per_file_reviews: dict, records: "Optional[List[ContextRecord]]" = None) -> None:
    """Render + print a degraded review comment after a fatal aborted the run
    mid-flight (C3, STEP 5a). Leads with the CAUTION banner and a NOT-approved basis,
    then whatever per-file reviews completed before the abort, plus the budget
    skipped-files marker. The comment is STILL written, so the failure is loud and
    visible rather than a silent red job with no explanation (acceptance #3)."""
    comment = "### 🤖 AI Review\n\n"
    _head = (os.environ.get("PR_HEAD_SHA") or "").strip()
    if _head:
        comment += f"*Reviewed at commit `{_head[:12]}`.*\n\n"
    comment += _LOUD_BANNER + "\n"
    comment += _skipped_marker()
    comment += "\n" + _format_verdict_basis(
        "Changes Requested",
        ["The automated review did not complete (see the notice above); treat this PR as NOT reviewed."],
    ) + "\n\n---\n"
    completed = {fp: t for fp, t in (per_file_reviews or {}).items() if t}
    if completed:
        comment += "\n**Partial results — reviews that completed before the run stopped:**\n"
        for fp, text in completed.items():
            comment += f"\n<details><summary>📄 **Review for `{_safe_md_path(fp)}`**</summary>\n\n{text}\n</details>\n"
    # The manifest belongs on the degraded path too — it is most diagnostic exactly when
    # the run aborted (what context HAD been loaded before it stopped).
    comment += _render_reference_manifest(records)
    comments = split_into_comments(comment, MAX_GITHUB_COMMENT_SIZE)
    print(comments[0])
    if len(comments) > 1:
        try:
            with open('review_comments.json', 'w') as f:
                json.dump(comments[1:], f)
        except Exception as e:
            logging.warning(f"Failed to write overflow comments: {describe_exc(e)}")


def _make_toolbox(module: Optional[str]) -> Optional[LeanToolbox]:
    """A Lean toolbox scoped to `module`, or None when Lean tools are disabled.
    Kept behind a factory so the CLI backend can later be swapped for an
    lean-lsp-mcp-backed toolbox in one place."""
    if not LEAN_TOOLS_ENABLED:
        return None
    return LeanToolbox(module=module or None)

# Named constants for thresholds
LARGE_FILE_LINE_THRESHOLD = 1500
MAX_GITHUB_COMMENT_SIZE = 65000
HTTP_TIMEOUT = 30
MAX_HTTP_REDIRECTS = 5

# Verdict severity ordering (worst wins). Shared by the per-file review merge
# and the deterministic overall verdict.
_VERDICT_RANK = {"Approved": 0, "Needs Minor Revisions": 1, "Changes Requested": 2}

# The four CrossFileAnalysis finding lists (used by cross-file, dependent-impact,
# and the verification/verdict machinery).
_CROSS_FILE_CATEGORIES = (
    "composition_issues", "escape_hatch_impact",
    "external_dependency_issues", "missing_cross_file_verification",
)

# Conservative character budget for assembled prompts. At ~3 chars/token for
# code-heavy text this is roughly 830K tokens, leaving headroom under the 1M
# context limit for output + thinking. Overridable via MAX_PROMPT_CHARS.
try:
    MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "2500000"))
except ValueError:
    MAX_PROMPT_CHARS = 2_500_000

# A changed file whose full content exceeds this many characters is reviewed in
# declaration-aligned sections (map-reduce) rather than a single call, so very
# large files (tens of thousands of lines) get fully reviewed instead of failing
# the prompt/output budget. Each section stays within this size. Overridable.
try:
    MAX_FILE_REVIEW_CHARS = int(os.environ.get("MAX_FILE_REVIEW_CHARS", "400000"))
except ValueError:
    MAX_FILE_REVIEW_CHARS = 400_000

# Replacement keys that are safe to truncate when a prompt exceeds the budget,
# in order of preference (bulkiest / least-essential first).
_TRIMMABLE_KEYS = ("REPO_CONTEXT", "DEPENDENCY_CONTEXT")

# Rough spend-budget guard for prompt assembly. Tokenization is model-specific
# and unavailable here, so use a conservative chars/token estimate when a
# per-run token budget is configured. In advisory mode this is a sizing hint; in
# hard mode the provider also enforces the exact budget after each returned call.
_PROMPT_CHARS_PER_BUDGET_TOKEN = 2
_MIN_DYNAMIC_PROMPT_CHARS = 20_000

# --- Helper Functions ---
def _load_prompt(template_name: str, replacements: Dict[str, str]) -> str:
    """Loads a prompt template and applies replacements with validation."""
    path = os.path.join(ACTION_PATH, "prompts", template_name)
    with open(path, "r") as f:
        template = f.read()

    # Substitute in one pass. Re-scanning the already-substituted values lets
    # attacker-controlled source such as ``{{VERDICT_RULES}}`` acquire prompt
    # structure when a later replacement happens to use that key.
    result = _render(template, replacements)

    # Check for any remaining unreplaced placeholders
    remaining = re.findall(r'\{\{([A-Za-z_]+)\}\}', result)
    if remaining:
        logging.warning(f"Unreplaced placeholders in {template_name}: {remaining}")

    return result


def _render(template: str, replacements: Dict[str, str]) -> str:
    """Apply {{KEY}} substitutions to a template string."""
    return re.sub(
        r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}",
        lambda match: replacements.get(match.group(1), match.group(0)),
        template,
    )


def _fit_replacements_to_budget(
    template: str,
    replacements: Dict[str, str],
    max_chars: int,
    context_label: str = "",
    trimmable: Tuple[str, ...] = _TRIMMABLE_KEYS,
) -> Dict[str, str]:
    """Trim the `trimmable` keys (in order, bulkiest/least-essential first) so
    that `_render(template, result)` is at most `max_chars` characters.

    Pure string logic — does not touch disk. Keeps the non-trimmable inputs
    (e.g. FILE_DIFF, FULL_CONTENT, the spec checklist) intact; those are the core
    of the review.
    """
    rendered = _render(template, replacements)
    if len(rendered) <= max_chars:
        return replacements

    result = dict(replacements)
    tag = f" ({context_label})" if context_label else ""

    for key in trimmable:
        current = result.get(key, "")
        if not current:
            continue
        rendered = _render(template, result)
        if len(rendered) <= max_chars:
            return result
        overshoot = len(rendered) - max_chars
        marker = f"\n\n[... {key} truncated to fit context window budget ...]\n"
        if len(current) > overshoot + len(marker):
            keep = len(current) - overshoot - len(marker)
            result[key] = current[:keep] + marker
            logging.warning(
                f"Prompt{tag} exceeded budget by {overshoot:,} chars; "
                f"truncated {key} to {keep:,} chars."
            )
        else:
            result[key] = f"[{key} omitted: exceeded context window budget]"
            logging.warning(
                f"Prompt{tag} still over budget after considering {key}; "
                f"dropped {key} entirely."
            )

    rendered = _render(template, result)
    if len(rendered) > max_chars:
        logging.warning(
            f"Prompt{tag} remains {len(rendered):,} chars (> {max_chars:,}) after "
            f"trimming {list(trimmable)}. The API may still reject it."
        )
    return result


def _fit_prompt_to_budget(
    template_name: str,
    replacements: Dict[str, str],
    max_chars: int = MAX_PROMPT_CHARS,
    context_label: str = "",
    trimmable: Tuple[str, ...] = _TRIMMABLE_KEYS,
) -> Dict[str, str]:
    """Disk-backed wrapper around `_fit_replacements_to_budget`.

    Reads the named prompt template and returns a trimmed replacements dict.
    """
    path = os.path.join(ACTION_PATH, "prompts", template_name)
    with open(path, "r") as f:
        template = f.read()
    return _fit_replacements_to_budget(template, replacements, max_chars, context_label, trimmable)


def _part_char_size(part: ContentPart) -> int:
    """Approximate how many prompt characters a content part contributes.

    This is only used for conservative preflight trimming against a token spend
    ceiling. Exact token accounting stays in the provider after a response
    returns.
    """
    data = part.data
    if isinstance(data, str):
        return len(data)
    if isinstance(data, (bytes, bytearray)):
        # Binary parts are sent as data: URLs with base64 payloads.
        return ((len(data) + 2) // 3) * 4 + len(part.mime_type or "")
    return len(str(data))


def _call_prompt_char_budget(
    provider: LLMProvider,
    *,
    thinking_budget: Optional[int] = None,
    external_parts: Optional[List[ContentPart]] = None,
    cached_body: str = "",
    parallelism: int = 1,
    fallback: int = MAX_PROMPT_CHARS,
) -> int:
    """Return a prompt-template char budget for one LLM call.

    Without a configured per-run token ceiling this preserves the existing
    context-window cap. With one, it estimates how much input room remains after
    already-spent tokens, output/thinking reserve, the operating contract,
    external references, any cached prefix, and the number of calls that can
    enter concurrently before usage is recorded.
    """
    budget = getattr(provider, "prompt_budget", None) or getattr(provider, "budget", None)
    max_tokens = getattr(budget, "max_tokens", None)
    if not max_tokens:
        return fallback

    try:
        snap = budget.snapshot()
        spent = (snap.input_tokens or 0) + (snap.output_tokens or 0)
    except Exception:
        spent = 0

    output_reserve = getattr(provider, "max_tokens", 16_384)
    if thinking_budget and thinking_budget > 0:
        output_reserve += int(thinking_budget)

    try:
        n_parallel = max(1, int(parallelism))
    except (TypeError, ValueError):
        n_parallel = 1

    # For parallel stages, several calls can enter before any one records usage.
    # Split the remaining run budget across that many in-flight calls so they
    # cannot each claim the full remaining budget during preflight sizing.
    prompt_tokens = ((max_tokens - spent) // n_parallel) - output_reserve
    if prompt_tokens <= 0:
        return _MIN_DYNAMIC_PROMPT_CHARS

    total_chars = min(fallback, prompt_tokens * _PROMPT_CHARS_PER_BUDGET_TOKEN)
    stable_chars = len(OPERATING_CONTRACT or "") + len(cached_body or "")
    stable_chars += sum(_part_char_size(p) for p in (external_parts or []))
    return max(_MIN_DYNAMIC_PROMPT_CHARS, total_chars - stable_chars)


def _check_ip_safe(ip_str: str) -> Tuple[bool, str]:
    """Returns (is_safe, reason) for a resolved IP address string."""
    ip = ipaddress.ip_address(ip_str)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return False, f"Blocked private/reserved IP: {ip}"
    # Cloud metadata endpoints that might not be in standard reserved ranges
    CLOUD_METADATA_IPS = {'169.254.169.254', '168.63.129.16', '100.100.100.200'}
    if ip_str in CLOUD_METADATA_IPS:
        return False, f"Blocked cloud metadata IP: {ip_str}"
    return True, ""


def _validate_url(url: str) -> Tuple[bool, str]:
    """Validates a URL is safe to fetch (SSRF protection).
    Blocks private IPs, link-local, loopback, cloud metadata, and non-HTTP(S) schemes.
    Returns (is_safe, reason) — does NOT resolve DNS (use _resolve_and_validate for that)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False, f"Blocked non-HTTP scheme: {parsed.scheme}"
        hostname = parsed.hostname
        if not hostname:
            return False, "No hostname in URL"
        hostname = hostname.lower()
        # Check for obvious private/dangerous hostnames
        if hostname in ('localhost', '127.0.0.1', '::1', '0.0.0.0'):
            return False, f"Blocked localhost URL: {hostname}"
        # Try to resolve and check IP ranges
        try:
            return _check_ip_safe(hostname)
        except ValueError:
            # hostname is a domain name, not an IP — check for metadata endpoints
            if hostname.endswith('.internal'):
                return False, f"Blocked internal hostname: {hostname}"
        return True, ""
    except Exception as e:
        return False, f"URL validation error: {e}"


def _resolve_and_validate(url: str) -> Tuple[bool, str, set]:
    """Validates URL and resolves DNS, returning pinned IPs to prevent DNS rebinding.
    Returns (is_safe, reason, resolved_ips)."""
    is_safe, reason = _validate_url(url)
    if not is_safe:
        return False, reason, set()

    parsed = urlparse(url)
    hostname = parsed.hostname.lower()
    # If hostname is already an IP, no DNS resolution needed
    try:
        ipaddress.ip_address(hostname)
        return True, "", {hostname}
    except ValueError:
        pass

    # Resolve DNS and validate all IPs
    try:
        resolved = {
            addr_info[4][0]
            for addr_info in socket.getaddrinfo(hostname, None)
            if addr_info[4]
        }
    except socket.gaierror as e:
        return False, f"Hostname resolution failed for {hostname}: {e}", set()
    if not resolved:
        return False, f"No IP addresses resolved for hostname: {hostname}", set()
    for resolved_ip in resolved:
        ip_safe, ip_reason = _check_ip_safe(resolved_ip)
        if not ip_safe:
            return False, f"{ip_reason} (via DNS resolution of {hostname})", set()
    return True, "", resolved


def _normalize_external_url(url: str) -> str:
    """Normalizes supported external reference URLs before fetching."""
    processed_url = url
    if "github.com" in url and "/blob/" in url:
        processed_url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        logging.info(f"Converted GitHub URL to raw: {processed_url}")
    return processed_url


# Serializes the process-global create_connection override used to pin DNS.
# URL fetching runs sequentially at startup (before review threads), but the
# lock keeps the override safe even if that ever changes.
_dns_pin_lock = threading.Lock()


def _pin_address(address, hostname: str, pinned_ip: str):
    """Substitute a pre-validated IP for `hostname` in a urllib3
    create_connection address tuple, preserving the port and any extra fields.
    Addresses for other hosts pass through unchanged."""
    if address and address[0] == hostname:
        return (pinned_ip,) + tuple(address[1:])
    return address


@contextlib.contextmanager
def _pinned_dns(hostname: str, pinned_ip: str):
    """Pin `hostname` to `pinned_ip` at the socket layer for the duration of the
    context, so the TCP connection targets the exact IP we validated. This closes
    the DNS-rebinding TOCTOU window between validation and connection. TLS SNI and
    certificate verification still use `hostname` (only the connect target is
    substituted, not the request host)."""
    original = urllib3_connection.create_connection

    def pinned_create_connection(address, *args, **kwargs):
        return original(_pin_address(address, hostname, pinned_ip), *args, **kwargs)

    with _dns_pin_lock:
        urllib3_connection.create_connection = pinned_create_connection
        try:
            yield
        finally:
            urllib3_connection.create_connection = original


def _fetch_url_content(url: str, timeout: int = HTTP_TIMEOUT, max_redirects: int = MAX_HTTP_REDIRECTS) -> Tuple[requests.Response, str]:
    """Fetches a URL while validating and resolving DNS at every hop to prevent
    SSRF via redirects and DNS rebinding (TOCTOU). The connection is pinned to
    the exact IP validated for each hop, so a DNS record that changes between
    validation and connection cannot redirect us to an unsafe host."""
    headers = {'User-Agent': 'Mozilla/5.0'}
    current_url = url
    visited = set()
    session = requests.Session()

    for _ in range(max_redirects + 1):
        is_safe, reason, resolved_ips = _resolve_and_validate(current_url)
        if not is_safe:
            raise ValueError(f"Blocked unsafe URL '{current_url}': {reason}")
        if current_url in visited:
            raise ValueError(f"Redirect loop detected while fetching '{url}'")
        visited.add(current_url)

        # Pin the connection to a validated IP so requests/urllib3 cannot
        # re-resolve to a different (unvalidated) address.
        hostname = (urlparse(current_url).hostname or "").lower()
        pinned_ip = next(iter(resolved_ips), None)
        if pinned_ip and hostname:
            with _pinned_dns(hostname, pinned_ip):
                response = session.get(current_url, timeout=timeout, headers=headers, allow_redirects=False)
        else:
            response = session.get(current_url, timeout=timeout, headers=headers, allow_redirects=False)
        if 300 <= response.status_code < 400 and response.headers.get("Location"):
            current_url = urljoin(current_url, response.headers["Location"])
            continue

        response.raise_for_status()
        return response, current_url

    raise requests.TooManyRedirects(f"Too many redirects while fetching '{url}'")


@dataclasses.dataclass
class ContextRecord:
    """One reference the reviewers were given, for the 'References & context used'
    manifest. Populated as an ADDITIVE side-channel by the loaders (see the optional
    `records` param) so the loaders' ``(parts, errors)`` return contract — relied on by
    callers and tests — is untouched. ``ref`` is the source URL/path; ``category`` is
    external | spec | repo; ``ok`` is whether it loaded."""
    ref: str
    category: str
    ok: bool


def get_document_content(urls_str: str, records: "Optional[List[ContextRecord]]" = None) -> Tuple[List[ContentPart], List[str]]:
    """Fetches content from URLs and returns provider-agnostic ContentParts.

    If `records` is given, appends a :class:`ContextRecord` per URL (loaded/failed) for
    the reference manifest — additive; the ``(parts, errors)`` return is unchanged."""
    if not urls_str:
        logging.info("No external references provided.")
        return [], []

    parts, errors = [], []
    urls = [url.strip() for url in urls_str.split(',') if url.strip()]
    logging.info(f"Fetching content from {len(urls)} external references...")

    for url in urls:
        processed_url = ""
        fallback_delegated = False
        try:
            logging.info(f"Processing URL: {url}")
            processed_url = _normalize_external_url(url)
            response, final_url = _fetch_url_content(processed_url)
            content_type = response.headers.get("Content-Type", "")
            final_path = urlparse(final_url).path.lower()

            if "application/pdf" in content_type or final_path.endswith('.pdf'):
                parts.append(ContentPart(type="pdf", data=response.content, mime_type="application/pdf"))
                logging.info(f"Added PDF part from: {url}")
            elif "text/html" in content_type or final_path.endswith(('.html', '.htm')):
                soup = BeautifulSoup(response.content, "html.parser")
                for element in soup(["script", "style", "nav", "footer", "header"]):
                    element.decompose()
                text = soup.get_text()
                lines = (line.strip() for line in text.splitlines())
                content = "\n".join(chunk for line in lines for chunk in line.split("  ") if chunk)
                parts.append(ContentPart(type="text", data=f"--- Content from {url} ---\n{content}\n"))
                logging.info(f"Added parsed HTML part from: {url}")
            else:
                content = response.text
                parts.append(ContentPart(type="text", data=f"--- Content from {url} ---\n{content}\n"))
                logging.info(f"Added plain text part from: {url}")
            if records is not None:
                records.append(ContextRecord(url, "external", True))
        except Exception as e:
            # R6: the errors list renders into the PR comment's Context Warnings —
            # keep the exception body out of it; log the detail (class/status/truncated).
            logging.error(f"Error processing document '{url}': {describe_exc(e)}")
            # A validated public PDF may still reject ordinary HTTP clients (for
            # example, a Cloudflare challenge). OpenRouter can fetch public PDF
            # URLs itself, so preserve the URL as a provider-native file part.
            # Never use this fallback for validation failures: doing so would
            # bypass the workflow's SSRF boundary.
            if (isinstance(e, requests.RequestException) and processed_url and
                    urlparse(processed_url).path.lower().endswith(".pdf")):
                parts.append(ContentPart(type="pdf", data=processed_url, mime_type="application/pdf"))
                fallback_delegated = True
                logging.info(f"Delegated PDF URL fallback to provider: {url}")
                errors.append(f"Could not fetch PDF reference '{url}' locally; delegated the public URL to the provider.")
            else:
                errors.append(f"Error processing document '{url}'.")
            if records is not None:
                records.append(ContextRecord(url, "external", fallback_delegated))
    return parts, errors

def get_local_reference_parts(paths_str: str, records: "Optional[List[ContextRecord]]" = None) -> Tuple[List[ContentPart], List[str]]:
    """Read local reference/specification files (a repository knowledge base) as
    provider-agnostic ContentParts so they can drive Agent A's checklist and
    ground every reviewer — just like external PDF/URL references, but from disk.

    PDFs are sent as native PDF parts; other files (`.md`, `.txt`, `.tex`,
    `.lean`) as text — `.tex` so LaTeX blueprints work as a spec source.
    Directories are expanded. Returns (parts, errors). If `records` is given, appends a
    :class:`ContextRecord` per file (loaded/failed) for the manifest — additive."""
    if not paths_str:
        return [], []
    parts: List[ContentPart] = []
    errors: List[str] = []
    raw = [p.strip() for p in paths_str.split(',') if p.strip()]
    files: List[str] = []
    for path in raw:
        if os.path.isdir(path):
            for root, _, names in os.walk(path):
                files.extend(os.path.join(root, n) for n in names
                             if n.endswith(('.pdf', '.md', '.txt', '.tex', '.lean')))
        elif os.path.isfile(path):
            files.append(path)
        else:
            errors.append(f"Could not find spec reference: {path}")
            if records is not None:
                records.append(ContextRecord(path, "spec", False))
    for fp in sorted(set(files)):
        try:
            if fp.lower().endswith('.pdf'):
                with open(fp, 'rb') as f:
                    parts.append(ContentPart(type="pdf", data=f.read(), mime_type="application/pdf"))
            else:
                content = file_cache.read(fp)
                if content is None:
                    errors.append(f"Error reading spec reference {fp}")
                    if records is not None:
                        records.append(ContextRecord(fp, "spec", False))
                    continue
                parts.append(ContentPart(type="text", data=f"--- Specification reference: {fp} ---\n{content}\n"))
            logging.info(f"Added local spec reference: {fp}")
            if records is not None:
                records.append(ContextRecord(fp, "spec", True))
        except Exception as e:
            logging.warning(f"Error reading spec reference {fp}: {describe_exc(e)}")
            errors.append(f"Error reading spec reference {fp}.")
            if records is not None:
                records.append(ContextRecord(fp, "spec", False))
    return parts, errors


_INSTRUCTION_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')


def _merge_csv(existing: str, extra: List[str]) -> str:
    """Append `extra` items to a comma-separated string, skipping duplicates."""
    items = [p.strip() for p in existing.split(',') if p.strip()] if existing else []
    for item in extra:
        if item not in items:
            items.append(item)
    return ','.join(items)


def extract_refs_from_instructions(text: str) -> Tuple[List[str], List[str], List[str]]:
    """Pull references out of freeform `/review` instructions.

    The ChatOps entry point is just `/review <anything>`: the text may mention
    external URLs, repository paths, or neither. This finds them so they can be
    fetched as review context — the text itself still reaches the agents intact
    as additional comments (mentions are extracted, not removed).

    Returns (urls, spec_paths, repo_paths):
    - urls: http(s) URLs, for `external_refs`.
    - spec_paths: existing local `.pdf`/`.tex` files, for `spec_refs` (they
      need the reference pipeline; repo context reads them as raw text).
    - repo_paths: other existing files/directories, for `repo_context_refs`.

    Only repo-relative paths are honored (no absolute paths, no `..`): comment
    text is lower-trust than workflow configuration. A bare word is only
    treated as a path if it contains a `/` or an extension, so prose like
    "check the docs" never drags in a directory — write `docs/` to mean one.
    """
    if not text:
        return [], [], []
    urls: List[str] = []
    for match in _INSTRUCTION_URL_RE.finditer(text):
        url = match.group(0).rstrip('.,;:!?\'"`')
        if url not in urls:
            urls.append(url)
    spec_paths: List[str] = []
    repo_paths: List[str] = []
    for raw_token in _INSTRUCTION_URL_RE.sub(' ', text).split():
        token = raw_token.strip('`"\',;:!?()[]{}<>*').rstrip('.')
        if not token:
            continue
        if os.path.isabs(token) or '..' in token.split('/'):
            continue
        if '/' not in token and not os.path.splitext(token)[1]:
            continue
        if not os.path.exists(token):
            continue
        target = spec_paths if token.lower().endswith(('.pdf', '.tex')) else repo_paths
        if token not in target:
            target.append(token)
    return urls, spec_paths, repo_paths


def _extract_added_lines(diff_text: str) -> List[str]:
    """Extracts only added lines (starting with +) from a unified diff, excluding diff headers."""
    added = []
    for line in diff_text.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            added.append(line[1:])  # Strip the leading '+'
    return added


def _added_line_numbers(diff_text: str) -> set:
    """New-file line numbers of the lines a diff adds (the '+' lines), derived
    from the `@@` hunk headers. Used to classify an escape hatch found in the
    full file as introduced-by-this-PR (line was added) vs pre-existing —
    which, unlike scanning the added lines in isolation, tracks block-comment
    depth correctly across the whole file."""
    added = set()
    current = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith('@@'):
            m = re.search(r'\+(\d+)', line)
            current = (int(m.group(1)) - 1) if m else 0
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith('+') and not line.startswith('+++'):
            current += 1
            added.add(current)
        elif line.startswith('-'):
            pass  # deletion: does not advance the new-file counter
        elif line == r'\ No newline at end of file':
            continue  # unified-diff metadata, not a source line
        else:
            current += 1  # context line advances the new-file counter
    return added


def _is_in_string(keyword: str, line: str) -> bool:
    """Basic check: if the keyword appears only inside a string literal."""
    # Find all string regions and check if keyword is exclusively within them
    in_string = False
    string_ranges = []
    start = 0
    for i, ch in enumerate(line):
        if not in_string and ch == '"':
            in_string = True
            start = i
        elif in_string and ch == '"' and (i == 0 or line[i-1] != '\\'):
            in_string = False
            string_ranges.append((start, i))

    if not string_ranges:
        return False

    # Check each occurrence of keyword
    for m in re.finditer(rf'\b{re.escape(keyword)}\b', line):
        in_any_string = any(s <= m.start() <= e for s, e in string_ranges)
        if not in_any_string:
            return False  # At least one occurrence is outside strings
    return True  # All occurrences are inside strings


# Kernel-bypassing constructs scanned deterministically by the mechanical
# pre-check. Escape hatches introduced by a PR trigger the hard verdict rule
# (unless allow-listed for the project — see ESCAPE_HATCH_ALLOWLIST).
ESCAPE_HATCHES = ['sorry', 'admit', 'axiom', 'native_decide', 'implemented_by', 'opaque', 'sorryAx']

# Project-configurable allowlist of escape hatches that are sanctioned for this
# repository and therefore do NOT trigger the hard "Changes Requested" verdict
# when introduced (they are still reported, as context). Comma-separated env var,
# e.g. ESCAPE_HATCH_ALLOWLIST="opaque,axiom". Case-sensitive keyword match.
ESCAPE_HATCH_ALLOWLIST = {
    kw.strip() for kw in os.environ.get("ESCAPE_HATCH_ALLOWLIST", "").split(",") if kw.strip()
}


def scan_escape_hatches(diff_by_file: Dict[str, str]) -> Dict[str, list]:
    """Deterministic scan for kernel-bypassing constructs and oversized files.

    Returns a structured dict so callers can both format it for humans and make
    a deterministic verdict decision:
        {
          "introduced":  [(file, keyword, snippet), ...],   # new in this PR
          "preexisting": [(file, keyword, line_no, snippet), ...],  # context only
          "large_files": [(file, n_lines), ...],            # context only
        }

    The whole file is scanned once with correct nested-comment tracking; each
    hatch is then classified as *introduced* if its line was added by the diff
    (via :func:`_added_line_numbers`) or *pre-existing* otherwise. Classifying
    against the full-file scan — rather than re-scanning the diff's added lines
    in isolation — is what makes a keyword inside a block comment that opens on
    an unchanged context line correctly ignored, instead of a false positive
    that would spuriously force a "Changes Requested" verdict.
    """
    introduced: list = []
    preexisting: list = []
    large_files: list = []

    for file_path, diff in diff_by_file.items():
        if not file_path.endswith('.lean'):
            continue

        added = _added_line_numbers(diff)
        full_content = file_cache.read(file_path)

        if full_content is None:
            # File content unavailable (e.g. deleted in this PR). Best-effort:
            # scan the diff's added lines directly. Block-comment depth cannot be
            # reconstructed from a non-contiguous added-line view, so this path
            # is approximate; it is only reached when the accurate full-file scan
            # is impossible.
            comment_depth = 0
            in_string = False
            for line in _extract_added_lines(diff):
                code, comment_depth, in_string = strip_comments_preserve_strings(
                    line, comment_depth, in_string,
                )
                if not code.strip():
                    continue
                for keyword in ESCAPE_HATCHES:
                    if re.search(rf'\b{keyword}\b', code):
                        introduced.append((file_path, keyword, line.strip()[:120]))
            continue

        full_lines = full_content.splitlines(keepends=True)
        comment_depth = 0
        in_string = False
        for i, line in enumerate(full_lines, 1):
            code, comment_depth, in_string = scrub_line(line, comment_depth, in_string)
            if not code.strip():
                continue
            for keyword in ESCAPE_HATCHES:
                if re.search(rf'\b{keyword}\b', code):
                    snippet = line.strip()[:120]
                    if i in added:
                        introduced.append((file_path, keyword, snippet))
                    else:
                        preexisting.append((file_path, keyword, i, snippet))

        if len(full_lines) > LARGE_FILE_LINE_THRESHOLD:
            large_files.append((file_path, len(full_lines)))

    return {"introduced": introduced, "preexisting": preexisting, "large_files": large_files}


def introduced_hatches_triggering_verdict(scan: Dict[str, list]) -> list:
    """Introduced escape hatches that are NOT allow-listed — i.e. the ones that
    deterministically force a 'Changes Requested' verdict."""
    return [(f, kw, s) for (f, kw, s) in scan["introduced"] if kw not in ESCAPE_HATCH_ALLOWLIST]


def format_prechecks(scan: Dict[str, list]) -> str:
    """Render a structured escape-hatch scan as human-readable markdown."""
    parts = []
    if scan["introduced"]:
        lines = []
        for file_path, keyword, snippet in scan["introduced"]:
            allow = " *(allow-listed — does not affect verdict)*" if keyword in ESCAPE_HATCH_ALLOWLIST else ""
            lines.append(
                f"- **`{keyword}`** introduced in `{_safe_md_path(file_path)}`{allow}: "
                f"`{_safe_md_path(snippet)}`"
            )
        parts.append("**Escape hatches introduced in this PR (triggers hard verdict rule):**\n" + "\n".join(lines))
    if scan["preexisting"]:
        lines = [f"- `{keyword}` in `{_safe_md_path(file_path)}` line {ln}: `{_safe_md_path(snippet)}`"
                 for file_path, keyword, ln, snippet in scan["preexisting"]]
        parts.append("**Pre-existing escape hatches in touched files (context only, does not affect verdict):**\n" + "\n".join(lines))
    if scan["large_files"]:
        lines = [f"- **Large file**: `{_safe_md_path(file_path)}` is {n} lines (exceeds {LARGE_FILE_LINE_THRESHOLD}-line lint threshold)"
                 for file_path, n in scan["large_files"]]
        parts.append("**File size (context only, does not affect verdict):**\n" + "\n".join(lines))

    if not parts:
        return "No escape hatches or file size issues detected."
    return "\n\n".join(parts)


def run_mechanical_prechecks(diff_by_file: Dict[str, str]) -> str:
    """Deterministic pre-checks: escape-hatch scan + file-size check, formatted
    as markdown. See :func:`scan_escape_hatches` for the structured form used by
    the deterministic verdict."""
    return format_prechecks(scan_escape_hatches(diff_by_file))


def get_summary_context(paths_str: str) -> str:
    """Reads type signatures and key declarations from files for summary-level context.
    Handles attributes on preceding lines, where-clauses, and inductive constructors."""
    if not paths_str:
        return ""
    summary_parts = []
    paths = [p.strip() for p in paths_str.split(',') if p.strip()]
    SIG_START = re.compile(
        r'^\s*(?:private |protected |noncomputable |partial |unsafe )*'
        r'(?:def |theorem |lemma |structure |class |instance |axiom |opaque |abbrev |inductive |variable |notation |macro |syntax )'
    )
    ATTR_LINE = re.compile(r'^\s*@\[')

    for file_path in paths:
        if not file_path.endswith('.lean'):
            continue
        content = file_cache.read(file_path)
        if content is None:
            continue
        all_lines = content.splitlines(keepends=True)
        sig_lines = []
        capturing = False
        pending_attr = None  # attribute line waiting for a declaration

        for line in all_lines:
            stripped = line.strip()

            # Standalone attribute line (e.g., @[simp])
            if ATTR_LINE.match(line) and not capturing:
                pending_attr = line.rstrip()
                continue

            if SIG_START.match(line):
                if pending_attr:
                    sig_lines.append(pending_attr)
                    pending_attr = None
                capturing = True
                sig_lines.append(line.rstrip())
            elif capturing:
                if not stripped:
                    capturing = False
                elif stripped.startswith(':= by') or stripped.startswith(':= fun') or stripped == ':= {':
                    capturing = False
                elif stripped.startswith(':=') and 'where' not in stripped:
                    capturing = False
                elif stripped == 'where':
                    sig_lines.append(line.rstrip())
                    # continue capturing structure fields
                elif stripped.startswith('|'):
                    sig_lines.append(line.rstrip())
                elif SIG_START.match(line):
                    if pending_attr:
                        sig_lines.append(pending_attr)
                        pending_attr = None
                    sig_lines.append(line.rstrip())
                elif line[0] in (' ', '\t'):
                    sig_lines.append(line.rstrip())
                else:
                    capturing = False
            else:
                pending_attr = None

        if sig_lines:
            summary_parts.append(f"--- Signatures from {file_path} ---\n" + "\n".join(sig_lines) + "\n--- End ---\n")

    return "\n".join(summary_parts)


def _call_provider(provider: LLMProvider, model: str, contents: List[ContentPart],
                   schema, thinking_budget=None, toolbox: Optional[LeanToolbox] = None):
    """Wrapper: calls provider, records token usage, returns parsed object.
    When `toolbox` is given, the model may call its Lean tools before answering."""
    if toolbox is not None:
        parsed, usage = provider.generate_structured(
            model=model, contents=contents, schema=schema,
            thinking_budget=thinking_budget,
            tools=toolbox.specs() or None, tool_runner=toolbox.run,
        )
    else:
        parsed, usage = provider.generate_structured(
            model=model, contents=contents, schema=schema, thinking_budget=thinking_budget,
        )
    advisory_budget = getattr(provider, "prompt_budget", None)
    hard_budget = getattr(provider, "budget", None)
    if advisory_budget is not None and advisory_budget is not hard_budget:
        advisory_budget.record_and_check(usage)
    token_tracker.record(usage)
    return parsed


# Marker in the per-file review templates separating the stable, per-run-constant
# prefix (checklist, repo context, best-practices checklist, verdict rules) from
# the volatile per-file content. The prefix is sent as a prompt-cached block so it
# is reused across every per-file / chunk review call. Stripped before sending.
CACHE_SPLIT_MARKER = "<<<CACHE_SPLIT>>>"


def _build_contents(prompt_text: str, external_parts: Optional[List[ContentPart]] = None,
                    cached_body: Optional[str] = None) -> List[ContentPart]:
    """Assemble request contents: shared external reference docs first — marked
    as a prompt-cache breakpoint so the stable prefix is reused across every
    agent call — then the per-call instruction prompt last (prefix caching is a
    prefix match, so volatile content must come after the cached span).

    Copies the external parts (rather than mutating the shared list) so the
    cache flag is safe to set from the parallel per-file review threads.

    The shared operating contract leads every request so its rules bind before
    the model sees any untrusted reference/PR content.
    """
    stable: List[ContentPart] = []
    if OPERATING_CONTRACT:
        stable.append(ContentPart(type="text", data=OPERATING_CONTRACT))
    if external_parts:
        stable.extend(dataclasses.replace(part, cache=False) for part in external_parts)
    if cached_body:
        # Per-file review's stable prefix (checklist, repo context, verdict rules):
        # constant across the run's per-file calls, so it caches too.
        stable.append(ContentPart(type="text", data=cached_body))
    # Put the single cache breakpoint at the END of the stable prefix, so the
    # whole prefix (operating contract + any reference docs + cached body) is
    # reused across every agent call — including the common no-external-refs case,
    # where the contract alone would otherwise be re-sent uncached on each of the
    # many per-file / verification calls.
    if stable:
        stable[-1] = dataclasses.replace(stable[-1], cache=True)
    return stable + [ContentPart(type="text", data=prompt_text)]


def get_lake_graph_str() -> str:
    """The serialized module import graph from the discover step.

    Prefers the file path in LAKE_GRAPH_PATH (a real repo's graph can exceed
    Linux's 128 KiB per-env-string cap, so the discover step hands over a file,
    not a value); the inline LAKE_GRAPH env remains as a fallback for local
    runs and tests."""
    path = (os.environ.get('LAKE_GRAPH_PATH') or '').strip()
    if path:
        try:
            with open(path, 'r', errors='replace') as f:
                return f.read()
        except OSError as e:
            logging.warning(f"Could not read LAKE_GRAPH_PATH {path!r}: {describe_exc(e)}")
    return os.environ.get('LAKE_GRAPH', '')


def run_triage(provider: LLMProvider, diff_by_file: Dict[str, str], spec_checklist: str, additional_comments: str, model_name: str) -> List[ReviewCluster]:
    """Triage Agent: Groups changed files into review clusters based on dependencies and coupling."""
    all_diffs = "\n".join([f"--- {f} ---\n{d}" for f, d in diff_by_file.items()])

    # Use the import graph from the discover step (avoids rebuilding it)
    dep_graph = get_lake_graph_str() or "Dependency graph not available."

    # Generate type signatures of changed files for semantic clustering
    changed_files_str = ','.join(f for f in diff_by_file.keys() if f.endswith('.lean'))
    changed_signatures = get_summary_context(changed_files_str)

    additional_section = ""
    if additional_comments and additional_comments.strip():
        additional_section = f"**Additional Reviewer Comments:**\n---\n{additional_comments}\n---\n"

    replacements = {
        "DEPENDENCY_GRAPH": dep_graph,
        "ALL_DIFFS": all_diffs,
        "SPEC_CHECKLIST": spec_checklist or "No specification checklist provided.",
        "ADDITIONAL_COMMENTS": additional_section,
        "CHANGED_FILE_SIGNATURES": changed_signatures or "No signatures extracted.",
    }
    try:
        # On a very large PR, degrade gracefully: trim the full diffs (bulkiest,
        # least-essential for clustering) so triage still runs on the type
        # signatures and dependency graph instead of failing to a per-file
        # fallback that loses all clustering.
        replacements = _fit_prompt_to_budget(
            "triage.md",
            replacements,
            max_chars=_call_prompt_char_budget(provider, thinking_budget=THINKING_BUDGET_LOW),
            context_label="triage",
            trimmable=("ALL_DIFFS",),
        )
        prompt_text = _load_prompt("triage.md", replacements)
    except FileNotFoundError:
        logging.warning("triage.md not found, falling back to per-file review.")
        return [ReviewCluster(name=f, files=[f], review_question="Review this file independently.", priority="medium")
                for f in diff_by_file if f.endswith('.lean')]

    try:
        logging.info("Triage Agent is grouping files into review clusters...")
        contents = _build_contents(prompt_text)
        triage = _call_provider(provider, model_name, contents, TriageResult, thinking_budget=THINKING_BUDGET_LOW)
        logging.info(f"Triage complete: {len(triage.clusters)} clusters identified.")
        return triage.clusters
    except Exception as e:
        # R3: count a hard spend/auth/quota failure, then re-raise budget/hard into
        # the top-level containment catch — never silently fall back on those.
        _reraise_if_fatal(e)  # budget/hard → re-raised to the handler that records + contains it
        logging.error(f"Triage failed, falling back to per-file: {describe_exc(e)}")
        return [ReviewCluster(name=f, files=[f], review_question="Review this file independently.", priority="medium")
                for f in diff_by_file if f.endswith('.lean')]


def analyze_specification(provider: LLMProvider, external_parts: List[ContentPart], model_name: str, all_diffs: str, summary_context: str = "", lake_graph: str = "") -> str:
    """Agent A: Analyzes the external specification and generates a checklist."""
    if not external_parts:
        return ""

    try:
        replacements = {
            "EXTERNAL_CONTEXT": "Refer to the external reference documents provided above (attached as content parts before this prompt).",
            "FILE_DIFFS": all_diffs,
            "REPO_STRUCTURE": summary_context or "No repository structure available.",
            "DEPENDENCY_GRAPH": lake_graph or "Dependency graph not available.",
            "PAPER_LEAN_EVIDENCE": os.environ.get("PAPER_LEAN_EVIDENCE", "No paper/Lean source index available."),
        }
        replacements = _fit_prompt_to_budget(
            "analyze_spec.md",
            replacements,
            max_chars=_call_prompt_char_budget(
                provider,
                thinking_budget=THINKING_BUDGET_HIGH,
                external_parts=external_parts,
            ),
            context_label="spec",
            trimmable=("FILE_DIFFS", "REPO_STRUCTURE", "DEPENDENCY_GRAPH"),
        )
        prompt_text = _load_prompt("analyze_spec.md", replacements)
    except FileNotFoundError:
        logging.error("Error: Prompt template 'analyze_spec.md' not found")
        return ""
    
    contents = _build_contents(prompt_text, external_parts)

    try:
        logging.info("Agent A (Spec Analyst) is generating the formalization checklist...")
        checklist = _call_provider(
            provider, model_name, contents, SpecChecklist,
            thinking_budget=THINKING_BUDGET_HIGH,
        )
        checklist_str = ""
        if checklist:
            # Reference mapping table
            if checklist.reference_mapping:
                checklist_str += "**Reference Mapping (Paper → Lean):**\n"
                for entry in checklist.reference_mapping:
                    status_icon = {"Present": "✅", "Missing": "❌", "Partial": "⚠️"}.get(entry.status, "?")
                    checklist_str += f"- {status_icon} **{entry.paper_result}**\n"
                    checklist_str += f"  - Mathematical content: {entry.mathematical_content}\n"
                    checklist_str += f"  - Status: {entry.status}\n"
                    if entry.cited_source:
                        checklist_str += f"  - Cited source (governs the admitted statement): {entry.cited_source}\n"
                    if entry.deviations_from_source:
                        checklist_str += f"  - ⚠️ Deviations from cited source: {entry.deviations_from_source}\n"
                checklist_str += "\n"

            # Checklist items
            for item in checklist.items:
                checklist_str += f"- **{item.concept}** [{item.severity}]\n"
                for step in item.verification_steps:
                    checklist_str += f"  - [ ] {step}\n"
        
        logging.info("Spec checklist generated successfully.")
        return checklist_str
    except Exception as e:
        _reraise_if_fatal(e)  # budget/hard → re-raised to the handler that records + contains it
        logging.error(f"Error during Spec Analysis: {describe_exc(e)}")
        return ""

def get_pr_diff(pr_number: str) -> Tuple[str, List[str]]:
    """Fetches the diff of the specified pull request.

    S1: when the action pins the base/head SHAs (``PR_BASE_SHA``/``PR_HEAD_SHA``, set
    from the same resolve step that drove the checkout), diff against those exact
    commits (``git diff base...head`` on the already-checked-out worktree) so the diff
    can never skew from the reviewed tree if the PR is force-pushed mid-run. Without the
    pins (local runs / older callers), fall back to ``gh pr diff`` — same three-dot
    semantics, but re-resolves the current head."""
    logging.info(f"Fetching PR diff for PR #{pr_number}...")
    errors = []
    base_sha = (os.environ.get("PR_BASE_SHA") or "").strip()
    head_sha = (os.environ.get("PR_HEAD_SHA") or "").strip()
    if base_sha and head_sha:
        cmd = ["git", "diff", f"{base_sha}...{head_sha}"]
    else:
        cmd = ["gh", "pr", "diff", pr_number]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
        diff = result.stdout.strip()
        if not diff:
            logging.warning("PR diff is empty.")
            errors.append("Could not retrieve PR diff or diff is empty.")
        logging.info("Successfully fetched PR diff.")
        return diff, errors
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        # R6/public-log: do not echo raw git stderr (may carry token-adjacent detail);
        # log a sanitized description, surface a generic error upward.
        logging.error(f"Failed to fetch PR diff for PR #{pr_number}: {describe_exc(e)}")
        errors.append(f"Failed to fetch PR diff for PR #{pr_number}.")
        return "", errors


def get_repo_files_by_path(paths_str: str, records: "Optional[List[ContextRecord]]" = None) -> Tuple[Dict[str, str], List[str]]:
    """Read content from a comma-separated string of file/directory paths,
    keyed by path. Returns (path_to_content, errors). If `records` is given, appends a
    :class:`ContextRecord` per file (loaded/failed) for the manifest — additive."""
    if not paths_str:
        return {}, []
    errors: List[str] = []
    paths = [p.strip() for p in paths_str.split(',') if p.strip()]
    logging.info(f"Fetching content from {len(paths)} repository paths...")
    expanded_files: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                expanded_files.extend([os.path.join(root, name) for name in files if name.endswith(('.lean', '.md'))])
        elif os.path.isfile(path):
            expanded_files.append(path)
        else:
            errors.append(f"Could not find file or directory: {path}")
            if records is not None:
                records.append(ContextRecord(path, "repo", False))
    result: Dict[str, str] = {}
    for file_path in sorted(set(expanded_files)):
        content = file_cache.read(file_path)
        if content is None:
            errors.append(f"Error reading file {file_path}")
            if records is not None:
                records.append(ContextRecord(file_path, "repo", False))
            continue
        result[file_path] = content
        if records is not None:
            records.append(ContextRecord(file_path, "repo", True))
    return result, errors


def _format_repo_files(files_by_path: Dict[str, str], exclude: Optional[set] = None) -> str:
    """Render a path→content dict as the REPO_CONTEXT block format.
    `exclude`: optional set of paths to omit (e.g. files being reviewed
    separately)."""
    exclude = exclude or set()
    emitted = [
        f"--- Start of content from {path} ---\n{content}\n--- End of content from {path} ---\n\n"
        for path, content in files_by_path.items() if path not in exclude
    ]
    if not emitted:
        return "No repository context files were provided." if not files_by_path \
            else "No repository context files remain after excluding files under review."
    return "".join(emitted)


def get_repo_files_content(paths_str: str) -> Tuple[str, List[str]]:
    """Legacy wrapper: returns the concatenated REPO_CONTEXT string."""
    if not paths_str:
        logging.info("No repository context files were provided.")
        return "No repository context files were provided.", []
    files_by_path, errors = get_repo_files_by_path(paths_str)
    return _format_repo_files(files_by_path), errors

def split_diff_into_files(diff_content: str) -> Dict[str, str]:
    """Splits a full git diff into a dictionary of per-file diffs.
    Handles renames by using the new (b/) path as the key."""
    files = {}
    # Split on every `diff --git ` line start, not on the full unquoted-path
    # pattern: with core.quotePath=true (git's default) non-ASCII paths arrive
    # C-quoted and a `a/(.+) b/(.+)` pattern misses them entirely — the file
    # would silently escape both review and the coverage check.
    file_diffs = re.split(r'(?=^diff --git )', diff_content, flags=re.MULTILINE)
    for file_diff in file_diffs:
        if not file_diff.strip():
            continue
        parsed = parse_git_diff_header(file_diff.splitlines()[0])
        if parsed:
            new_path = parsed[1]
            # For renames, check the rename header
            rename_match = re.search(r'^rename to (.+)$', file_diff, re.MULTILINE)
            if rename_match:
                new_path = unquote_git_path(rename_match.group(1))
            files[new_path] = file_diff
    return files

def _finding_lines(f: Finding, include_fix: bool = True) -> List[str]:
    """Render a Finding as markdown lines, surfacing its grounding (evidence) and
    confidence so a human can validate it."""
    loc = f" (`{f.location}`)" if f.location else ""
    conf = f" _(confidence: {f.confidence})_" if f.confidence else ""
    tags = []
    if f.category:
        tags.append(f.category)
    if f.severity:
        tags.append(f.severity)
    tag_text = f" _({' · '.join(tags)})_" if tags else ""
    lines = [f"- {f.description}{loc}{tag_text}{conf}"]
    if f.evidence:
        lines.append(f"  - Evidence: {f.evidence}")
    if f.evidence_source:
        lines.append(f"  - Evidence source: `{f.evidence_source}`")
    if f.evidence_locator:
        lines.append(f"  - Evidence locator: `{f.evidence_locator}`")
    if f.evidence_medium != "unknown":
        lines.append(f"  - Evidence medium: `{f.evidence_medium}`")
    if f.confirmation_method != "unconfirmed":
        lines.append(f"  - Confirmation method: `{f.confirmation_method}`")
    if f.verification_status == "confirmed":
        lines.append("  - Verification: independently confirmed")
    if include_fix and f.suggested_fix:
        lines.append(f"  - Suggested fix: {f.suggested_fix}")
    if f.how_to_confirm:
        lines.append(f"  - How to confirm: {f.how_to_confirm}")
    if f.disconfirming_check:
        lines.append(f"  - Disconfirming check: {f.disconfirming_check}")
    return lines


def _format_file_review(review: FileReview, file_path: str) -> str:
    """Formats a structured FileReview into markdown.

    The per-file model verdict is explicitly labeled as an agent assessment;
    the overall deterministic policy is rendered separately. Findings that do
    not meet the blocking grounding contract remain visible in an advisory
    details block instead of looking like ordinary blocking issues.
    """
    parts = []

    if review.analysis:
        parts.append(f"**Analysis:**\n{review.analysis}\n")

    parts.append(f"**Agent assessment:** {review.verdict}\n")

    critical = [f for f in review.critical_misformalizations if _has_blocking_grounding(f)]
    lean_issues = [f for f in review.lean_issues if _has_blocking_grounding(f)]
    advisory = [
        *[f for f in review.critical_misformalizations if not _has_blocking_grounding(f)],
        *[f for f in review.lean_issues if not _has_blocking_grounding(f)],
        *review.nitpicks,
    ]

    if review.checklist_results:
        parts.append("**Checklist Verification:**")
        for cr in review.checklist_results:
            icon = {"satisfied": "✅", "violated": "❌", "unclear": "⚠️"}.get(cr.status, "?")
            parts.append(f"- {icon} **{cr.item}**: {cr.explanation}")
        parts.append("")

    if critical:
        parts.append("**Critical Misformalizations:**")
        for f in critical:
            parts.extend(_finding_lines(f))
        parts.append("")
    else:
        parts.append("**Critical Misformalizations:** None\n")

    if lean_issues:
        parts.append("**Lean 4 / Mathlib Issues:**")
        for f in lean_issues:
            parts.extend(_finding_lines(f))
        parts.append("")
    else:
        parts.append("**Lean 4 / Mathlib Issues:** None\n")

    if advisory:
        parts.append("<details><summary>💡 <b>Advisory feedback</b></summary>\n")
        for f in advisory:
            parts.extend(_finding_lines(f, include_fix=False))
        parts.append("\n</details>\n")
    else:
        parts.append("**Advisory feedback:** None\n")

    return "\n".join(parts)


# Top-level declaration starts (no leading indentation), used to align file
# chunk boundaries with declaration boundaries.
_TOP_LEVEL_DECL = re.compile(
    r'^(?:private |protected |noncomputable |partial |unsafe )*'
    r'(?:def |theorem |lemma |structure |class |instance |axiom |opaque |abbrev |inductive |example |notation |macro |syntax )'
)


def _chunk_file_by_declarations(full_content: str, max_chars: int) -> List[Tuple[int, int, str]]:
    """Split file content into contiguous chunks at top-level declaration
    boundaries, each at most `max_chars` where possible (a single declaration
    larger than `max_chars` becomes its own oversized chunk). The chunks cover
    the whole file with no gaps or overlaps.

    Returns a list of (start_line, end_line, text) with 1-indexed inclusive line
    ranges.
    """
    lines = full_content.splitlines(keepends=True)
    if not lines:
        return []

    # Unit boundaries: line 0 (preamble/imports) plus every top-level decl start.
    boundaries = sorted(set([0] + [i for i, ln in enumerate(lines) if _TOP_LEVEL_DECL.match(ln)]))
    units = [(s, boundaries[k + 1] if k + 1 < len(boundaries) else len(lines))
             for k, s in enumerate(boundaries)]

    chunks: List[Tuple[int, int]] = []
    cur_start = cur_end = cur_len = 0
    for (s, e) in units:
        unit_len = sum(len(lines[x]) for x in range(s, e))
        if cur_len == 0:
            cur_start, cur_end, cur_len = s, e, unit_len
        elif cur_len + unit_len <= max_chars:
            cur_end, cur_len = e, cur_len + unit_len
        else:
            chunks.append((cur_start, cur_end))
            cur_start, cur_end, cur_len = s, e, unit_len
    if cur_len > 0 or not chunks:
        chunks.append((cur_start, cur_end))

    return [(s + 1, e, "".join(lines[s:e])) for (s, e) in chunks]


def _parse_diff_hunks(diff_text: str) -> List[Tuple[int, int, str]]:
    """Parse a per-file unified diff into (new_start, new_end, hunk_text) tuples
    using new-file line numbers. `hunk_text` includes the `@@` header and body."""
    header_re = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
    lines = diff_text.splitlines(keepends=True)
    hunks: List[Tuple[int, int, str]] = []
    idx = 0
    while idx < len(lines):
        m = header_re.match(lines[idx])
        if not m:
            idx += 1
            continue
        new_start = int(m.group(1))
        new_count = int(m.group(2)) if m.group(2) is not None else 1
        j = idx + 1
        while j < len(lines) and not lines[j].startswith('@@') and not lines[j].startswith('diff --git'):
            j += 1
        new_end = max(new_start, new_start + new_count - 1)
        hunks.append((new_start, new_end, "".join(lines[idx:j])))
        idx = j
    return hunks


def _diff_header(diff_text: str) -> str:
    """Everything before the first hunk (the `diff --git`/`---`/`+++` lines)."""
    out = []
    for ln in diff_text.splitlines(keepends=True):
        if ln.startswith('@@'):
            break
        out.append(ln)
    return "".join(out)


def _diff_for_range(header: str, hunks: List[Tuple[int, int, str]], start_line: int, end_line: int) -> str:
    """Reconstruct a file diff containing only the hunks overlapping the
    inclusive line range [start_line, end_line]. Empty string if none overlap."""
    selected = [text for (hs, he, text) in hunks if hs <= end_line and he >= start_line]
    return header + "".join(selected) if selected else ""


_MECHANICAL_BUILD_CLAIM = re.compile(
    r"\b(?:won['’]?t|will not|does not|doesn't|cannot|can't|fails to|unable to)"
    r"\s+(?:typecheck|type-check|compile|build|elaborate)|"
    r"\b(?:unknown identifier|unknown constant|declaration .* not found|type mismatch|failed to synthesize)\b",
    re.IGNORECASE,
)
_COMPILER_EVIDENCE = re.compile(
    r"\b(?:compiler|toolchain|lake(?:\s+env)?\s+lean|lean_(?:check|typecheck|print)|"
    r"stderr|stdout|error:\s|diagnostic)\b",
    re.IGNORECASE,
)
_PREEXISTING_DEPENDENCY_LANGUAGE = re.compile(
    r"\b(?:depend|impact|transitive|rel(?:y|ies)|uses?|calls?|changed code|new code|"
    r"introduced|downstream|inherited)\b",
    re.IGNORECASE,
)
_NON_BLOCKING_EVIDENCE_SOURCES = {"docstring_only", "model_reasoning"}
_ADVISORY_CATEGORIES = {"style", "generalization", "proof", "documentation"}

# Ordering for the verifier's downward-only severity correction (survives()).
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_EVIDENCE_SOURCE_MEDIA = {
    "compiler": {"compiler"},
    "kernel": {"kernel"},
    "paper_or_spec": {"pdf", "tex", "markdown", "plain_text", "lean"},
    "trusted_repo_reference": {"repository", "lean", "downstream"},
    "lean_source": {"lean"},
    "downstream_contract": {"downstream", "repository", "lean"},
}


def _finding_location(location: str) -> Tuple[str, Optional[int]]:
    """Extract a normalized path and first line from a model-supplied location."""
    if not location:
        return "", None
    match = re.search(r"(.+?):(\d+)", location.strip())
    if not match:
        return location.strip().lower(), None
    return match.group(1).strip().lower(), int(match.group(2))


def _finding_text(finding: Finding) -> str:
    return " ".join((finding.description, finding.evidence, finding.suggested_fix)).lower()


def _is_pdf_evidence(finding: Finding) -> bool:
    """Detect PDF-backed evidence, including older findings without the medium field."""
    if finding.evidence_medium == "pdf":
        return True
    locator = f"{finding.evidence_locator} {finding.evidence}".lower()
    return bool(re.search(r"(?:\.pdf\b|\bpdf\b|arxiv\.org/pdf/)", locator))


def _has_blocking_grounding(finding: Finding, require_verification: bool = False) -> bool:
    """Whether a finding has enough provenance to drive a blocking verdict.

    This is intentionally a conservative gate. The evidence text is still
    shown to reviewers, but a model assertion without an exact source locator,
    or a claim based only on a docstring/model reasoning, cannot block a PR.
    this gate. Only critical/high-severity findings can drive a blocking
    verdict; medium/low findings remain advisory even when well located.
    """
    # Verification is precision metadata: an explicit refutation removes a
    # finding and confirmation is displayed, but an unavailable or uncertain
    # verifier must not silently erase a grounded issue from the issue path.
    # ``require_verification`` remains accepted for compatibility with callers
    # using the earlier policy. PDF evidence remains special because visual
    # confirmation is required.
    return (
        finding.confidence in ("high", "medium")
        and finding.severity in ("critical", "high")
        and finding.category not in _ADVISORY_CATEGORIES
        and finding.evidence_source not in _NON_BLOCKING_EVIDENCE_SOURCES
        and bool(finding.evidence.strip())
        and bool(finding.evidence_locator.strip())
        and (
            finding.evidence_source not in _EVIDENCE_SOURCE_MEDIA
            or finding.evidence_medium in _EVIDENCE_SOURCE_MEDIA[finding.evidence_source]
        )
        and (not _is_pdf_evidence(finding) or finding.confirmation_method == "visual")
    )


def _finding_fingerprint(finding: Finding) -> Tuple[str, str, str]:
    """Build a conservative duplicate key.

    Only exact normalized duplicates are collapsed. This layer must not infer
    that differently worded reports about the same token are identical; that is
    semantic work for the agents and verifier.
    """
    path, line = _finding_location(finding.location)
    text = _finding_text(finding)
    normalized = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return path, str(line or ""), f"{finding.category}:{' '.join(normalized.split())}"


def _deduplicate_finding_lists(
    per_file_structured: Dict[str, "FileReview"],
    cross_file_structured: Optional["CrossFileAnalysis"],
) -> int:
    """Deduplicate findings across chunk/category/agent boundaries in place."""
    seen = set()
    removed = 0
    lists = []
    for review in per_file_structured.values():
        if review is not None:
            lists.extend((review, name) for name in ("critical_misformalizations", "lean_issues", "nitpicks"))
    if cross_file_structured is not None:
        lists.extend((cross_file_structured, name) for name in _CROSS_FILE_CATEGORIES)

    # Keep blockers ahead of advisory entries when duplicate reports compete.
    lists.sort(key=lambda item: {"critical_misformalizations": 0, "lean_issues": 1, "composition_issues": 1,
                                  "escape_hatch_impact": 1, "external_dependency_issues": 1,
                                  "missing_cross_file_verification": 1, "nitpicks": 2}.get(item[1], 3))
    for owner, name in lists:
        kept = []
        for finding in getattr(owner, name):
            key = _finding_fingerprint(finding)
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            kept.append(finding)
        setattr(owner, name, kept)
    return removed


def _filter_ungrounded_findings(
    per_file_structured: Dict[str, "FileReview"],
    cross_file_structured: Optional["CrossFileAnalysis"],
    precheck_scan: Dict[str, list],
    build_succeeded: bool,
) -> List[str]:
    """Apply deterministic boundaries around common false-positive classes.

    The model remains responsible for semantic judgment. This function only
    removes claims that contradict facts the workflow itself established: a
    successful build cannot be used as evidence for a bare build failure claim,
    and a pre-existing escape hatch is not a newly introduced PR defect unless
    the finding explicitly discusses dependency/impact.
    """
    notes: List[str] = []
    preexisting = precheck_scan.get("preexisting", [])

    # `verification_status` is an internal pipeline field. Reset any value
    # emitted by the initial model before the independent verifier gets a
    # chance to confirm findings; otherwise the model could self-certify its
    # own report by returning ``confirmed`` in structured JSON.
    for review in per_file_structured.values():
        if review is not None:
            for name in ("critical_misformalizations", "lean_issues", "nitpicks"):
                for finding in getattr(review, name):
                    finding.confirmation_method = "unconfirmed"
                    finding.verification_status = "unverified"
    if cross_file_structured is not None:
        for name in _CROSS_FILE_CATEGORIES:
            for finding in getattr(cross_file_structured, name):
                finding.confirmation_method = "unconfirmed"
                finding.verification_status = "unverified"

    def should_drop(finding: Finding) -> Optional[str]:
        text = _finding_text(finding)
        # A suggested fix may legitimately say "if this does not typecheck";
        # it is not evidence that the finding itself is a build claim. Restrict
        # this mechanical filter to the report and its cited evidence.
        claim_text = " ".join((finding.description, finding.evidence)).lower()
        compiler_evidence = (
            finding.evidence_source in {"compiler", "kernel"}
            or bool(_COMPILER_EVIDENCE.search(
                " ".join((finding.evidence, finding.evidence_locator))
            ))
        )
        if (build_succeeded and _MECHANICAL_BUILD_CLAIM.search(claim_text)
                and not compiler_evidence):
            return "successful workflow build; no compiler/toolchain evidence supplied"

        path, line = _finding_location(finding.location)
        for old_path, keyword, old_line, _snippet in preexisting:
            if path != old_path.lower() or line is None or abs(line - old_line) > 1:
                continue
            # A suggested fix is remediation advice, not evidence that the PR
            # introduced a dependency. Otherwise a model could keep a
            # pre-existing `sorry` finding alive merely by writing
            # "update downstream callers" in its fix field.
            report_text = " ".join((finding.description, finding.evidence)).lower()
            if (re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE)
                    and not _PREEXISTING_DEPENDENCY_LANGUAGE.search(report_text)):
                return f"pre-existing `{keyword}` at {old_path}:{old_line}; no PR-introduced dependency/impact claimed"
        return None

    owners = []
    for review in per_file_structured.values():
        if review is not None:
            owners.extend((review, name) for name in ("critical_misformalizations", "lean_issues", "nitpicks"))
    if cross_file_structured is not None:
        owners.extend((cross_file_structured, name) for name in _CROSS_FILE_CATEGORIES)

    for owner, name in owners:
        kept = []
        for finding in getattr(owner, name):
            reason = should_drop(finding)
            if reason:
                notes.append(f"{finding.description} ({finding.location or 'no location'}): {reason}.")
            else:
                kept.append(finding)
        setattr(owner, name, kept)

    removed_duplicates = _deduplicate_finding_lists(per_file_structured, cross_file_structured)
    if removed_duplicates:
        notes.append(f"{removed_duplicates} exact duplicate finding(s) collapsed across review sections/chunks.")
    return notes


def _merge_file_reviews(reviews: List[FileReview]) -> Optional[FileReview]:
    """Combine per-section FileReviews into one: worst verdict, concatenated
    analyses, and deduplicated findings/checklist results."""
    valid = [r for r in reviews if r is not None]
    if not valid:
        return None

    worst = max((r.verdict for r in valid), key=lambda v: _VERDICT_RANK[v])

    def _dedup(findings: List[Finding]) -> List[Finding]:
        seen, out = set(), []
        for f in findings:
            key = (f.description.strip(), f.location.strip())
            if key not in seen:
                seen.add(key)
                out.append(f)
        return out

    # Dedup checklist results by item, preferring the most severe status.
    status_rank = {"satisfied": 0, "unclear": 1, "violated": 2}
    by_item: Dict[str, ChecklistResult] = {}
    for r in valid:
        for cr in r.checklist_results:
            prev = by_item.get(cr.item)
            if prev is None or status_rank.get(cr.status, 0) > status_rank.get(prev.status, 0):
                by_item[cr.item] = cr

    return FileReview(
        analysis="\n\n".join(r.analysis for r in valid if r.analysis),
        verdict=worst,
        checklist_results=list(by_item.values()),
        critical_misformalizations=_dedup([f for r in valid for f in r.critical_misformalizations]),
        lean_issues=_dedup([f for r in valid for f in r.lean_issues]),
        nitpicks=_dedup([f for r in valid for f in r.nitpicks]),
    )


def _run_file_review(provider: LLMProvider, prompt_file: str, base_replacements: Dict[str, str],
                     full_content: str, file_diff: str, external_parts: list,
                     review_model: str, context_label: str,
                     budget_parallelism: int = 1,
                     toolbox: Optional[LeanToolbox] = None) -> Tuple[Optional[FileReview], Optional[str]]:
    """Single Agent-B call for one (chunk of a) file. Returns (review, None) on
    success or (None, error_message) on failure."""
    replacements = dict(base_replacements)
    replacements["FULL_CONTENT"] = full_content
    replacements["FILE_DIFF"] = file_diff
    try:
        replacements = _fit_prompt_to_budget(
            prompt_file,
            replacements,
            max_chars=_call_prompt_char_budget(
                provider,
                thinking_budget=THINKING_BUDGET_HIGH,
                external_parts=external_parts,
                parallelism=budget_parallelism,
            ),
            context_label=context_label,
        )
        prompt_text = _load_prompt(prompt_file, replacements)
    except FileNotFoundError:
        return None, f"Error: Prompt template not found: {prompt_file}"

    # Split the rendered prompt into a cacheable stable prefix (checklist, repo
    # context, verdict rules — constant across the run's per-file calls) and the
    # volatile per-file suffix. The marker is dropped from what the model sees.
    cached_body, sep, volatile = prompt_text.partition(CACHE_SPLIT_MARKER)
    if sep:
        contents = _build_contents(volatile.lstrip("\n"), external_parts, cached_body=cached_body.rstrip())
    else:
        contents = _build_contents(prompt_text, external_parts)
    try:
        logging.info(f"Agent B is reviewing: {context_label}...")
        review = _call_provider(provider, review_model, contents, FileReview,
                                thinking_budget=THINKING_BUDGET_HIGH, toolbox=toolbox)
        return review, None
    except Exception as e:
        _reraise_if_fatal(e)  # budget/hard → re-raised to the handler that records + contains it
        # R6: the returned string is rendered into the PR comment — keep the exception
        # body (model-influenced / provider payload) out of it; full detail to the log.
        logging.error(f"Error during API call for {context_label}: {describe_exc(e)}")
        return None, f"An error occurred while analyzing `{context_label}`."


def analyze_file_changes_with_context(provider: LLMProvider, review_context: dict, file_path: str, file_diff: str, full_content: str, spec_checklist: str, external_parts: list, lean4_checklist: str, verdict_rules: str) -> Tuple[FileReview, str]:
    """Agent B (Code Reviewer): Returns (structured FileReview, formatted markdown).
    On error, returns (None, error_message).

    Files whose content exceeds MAX_FILE_REVIEW_CHARS are reviewed in
    declaration-aligned sections and merged, so large files are covered in full
    rather than failing the budget."""

    # Select the appropriate prompt depending on if we have a checklist from Agent A
    prompt_file = "review_code_with_spec.md" if spec_checklist else "review_file.md"
    additional_comments = review_context.get("additional_comments", "")
    additional_comments_section = ""
    if additional_comments and additional_comments.strip():
        additional_comments_section = f"""**Additional Reviewer Comments:**
---
{additional_comments}
---
"""

    cluster_context = review_context.get("cluster_context", "")
    cluster_section = ""
    if cluster_context:
        cluster_section = f"**Review Cluster Context (signatures of related files in this cluster):**\n---\n{cluster_context}\n---\n"

    # Per-file REPO_CONTEXT: drop changed files (they are each reviewed on their
    # own per-file pass, and sibling awareness is carried by cluster_context).
    # Falls back to the pre-rendered string if the structured inputs are absent
    # (e.g. callers that don't populate repo_files_by_path).
    repo_files_by_path = review_context.get("repo_files_by_path")
    if repo_files_by_path is not None:
        exclude = set(review_context.get("changed_files", set()))
        per_file_repo = _format_repo_files(repo_files_by_path, exclude=exclude)
        per_file_repo += review_context.get("repo_context_appendix", "")
    else:
        per_file_repo = review_context.get("repo_context", "")

    # Everything except FULL_CONTENT / FILE_DIFF, which vary per section.
    base_replacements = {
        "SPEC_CHECKLIST": spec_checklist,
        "REPO_CONTEXT": per_file_repo,
        "FILE_PATH": file_path,
        "ADDITIONAL_COMMENTS": additional_comments_section,
        "CLUSTER_CONTEXT": cluster_section,
        "LEAN4_CHECKLIST": lean4_checklist,
        "VERDICT_RULES": verdict_rules,
    }
    review_model = review_context.get("review_model")
    try:
        budget_parallelism = int(review_context.get("max_workers", 1))
    except (TypeError, ValueError):
        budget_parallelism = 1
    toolbox = _make_toolbox(file_path_to_module_name(file_path))

    # Small file: single call (the common case).
    if len(full_content) <= MAX_FILE_REVIEW_CHARS:
        review, err = _run_file_review(
            provider, prompt_file, base_replacements, full_content, file_diff,
            external_parts, review_model, file_path,
            budget_parallelism=budget_parallelism, toolbox=toolbox,
        )
        if review is None:
            return None, err
        return review, _format_file_review(review, file_path)

    # Large file: map-reduce over declaration-aligned sections. Only sections
    # that actually contain diff hunks are reviewed; each is given the full
    # file's signatures for context.
    chunks = _chunk_file_by_declarations(full_content, MAX_FILE_REVIEW_CHARS)
    hunks = _parse_diff_hunks(file_diff)
    header = _diff_header(file_diff)
    file_sigs = get_summary_context(file_path)

    reviews: List[FileReview] = []
    errors: List[str] = []
    reviewed_sections = 0
    for (start, end, text) in chunks:
        chunk_diff = _diff_for_range(header, hunks, start, end)
        if not chunk_diff:
            continue  # no changes in this section
        reviewed_sections += 1
        chunk_full = (
            f"[This is lines {start}-{end} of `{file_path}`, reviewed in sections because the "
            f"file is large. Signatures of the full file for context:]\n{file_sigs}\n\n"
            f"[Section content:]\n{text}"
        )
        review, err = _run_file_review(
            provider, prompt_file, base_replacements, chunk_full, chunk_diff,
            external_parts, review_model, f"{file_path}:{start}-{end}",
            budget_parallelism=budget_parallelism, toolbox=toolbox,
        )
        if review is not None:
            reviews.append(review)
        if err:
            errors.append(err)

    logging.info(f"Chunked review of {file_path}: {reviewed_sections} changed section(s), "
                 f"{len(reviews)} succeeded, {len(errors)} failed.")

    merged = _merge_file_reviews(reviews)
    if merged is None:
        return None, (f"An error occurred while analyzing `{file_path}` (chunked): "
                      + ("; ".join(errors) if errors else "no changed sections produced a review."))
    if errors:
        # Partial coverage — flag it so the deterministic verdict cannot certify
        # this file as Approved, and make the gap visible in the analysis.
        merged.coverage_incomplete = True
        note = (f"⚠️ Incomplete chunked review of `{file_path}`: "
                f"{len(errors)} of {reviewed_sections} changed section(s) could not be reviewed.")
        merged.analysis = (merged.analysis + "\n\n" + note) if merged.analysis else note
    return merged, _format_file_review(merged, file_path)

def _format_cross_file(analysis: CrossFileAnalysis) -> str:
    """Formats a structured CrossFileAnalysis into markdown."""
    sections = []

    if analysis.analysis:
        sections.append(f"**Cross-File Analysis:**\n{analysis.analysis}\n")

    def _fmt_findings(title: str, findings: list[Finding]) -> str:
        blocking = [f for f in findings if _has_blocking_grounding(f)]
        advisory = [f for f in findings if not _has_blocking_grounding(f)]
        if not blocking and not advisory:
            return f"**{title}:** None"
        lines = [f"**{title}:**"] if blocking else []
        for f in blocking:
            lines.extend(_finding_lines(f))
        if advisory:
            lines.append(f"<details><summary>💡 <b>{title} (advisory)</b></summary>")
            for f in advisory:
                lines.extend(_finding_lines(f, include_fix=False))
            lines.append("</details>")
        return "\n".join(lines)

    sections.append(_fmt_findings("Cross-File Composition Issues", analysis.composition_issues))
    sections.append(_fmt_findings("Axiom/Escape Hatch Impact", analysis.escape_hatch_impact))
    sections.append(_fmt_findings("External Dependency Issues", analysis.external_dependency_issues))
    sections.append(_fmt_findings("Missing Cross-File Verification", analysis.missing_cross_file_verification))
    return "\n\n".join(sections)


def analyze_cross_file(provider: LLMProvider, diff_by_file: Dict[str, str], spec_checklist: str, pre_check_findings: str, repo_context: str, additional_comments: str, external_parts: list, model_name: str) -> Tuple[CrossFileAnalysis, str]:
    """Cross-File Analysis Agent. Returns (structured CrossFileAnalysis, formatted markdown).
    On error returns (None, error_message)."""
    # Build full content of all changed Lean files using cache
    all_changed_contents = ""
    for file_path in diff_by_file:
        if not file_path.endswith('.lean'):
            continue
        content = file_cache.read(file_path)
        if content is not None:
            all_changed_contents += f"--- Start of {file_path} ---\n{content}\n--- End of {file_path} ---\n\n"

    all_diffs = "\n".join([f"--- {f} ---\n{d}" for f, d in diff_by_file.items()])

    additional_comments_section = ""
    if additional_comments and additional_comments.strip():
        additional_comments_section = f"**Additional Reviewer Comments:**\n---\n{additional_comments}\n---\n"

    replacements = {
        "SPEC_CHECKLIST": spec_checklist or "No specification checklist provided.",
        "PRE_CHECK_FINDINGS": pre_check_findings,
        "ALL_DIFFS": all_diffs,
        "ALL_CHANGED_CONTENTS": all_changed_contents,
        "DEPENDENCY_CONTEXT": repo_context,
        "ADDITIONAL_COMMENTS": additional_comments_section,
    }
    try:
        replacements = _fit_prompt_to_budget(
            "cross_file_analysis.md", replacements, context_label="cross-file",
            max_chars=_call_prompt_char_budget(
                provider,
                thinking_budget=THINKING_BUDGET_HIGH,
                external_parts=external_parts,
            ),
            trimmable=("DEPENDENCY_CONTEXT", "ALL_CHANGED_CONTENTS"),
        )
        prompt_text = _load_prompt("cross_file_analysis.md", replacements)
    except FileNotFoundError:
        logging.warning("cross_file_analysis.md not found, skipping cross-file analysis.")
        return None, ""

    contents = _build_contents(prompt_text, external_parts)

    try:
        logging.info("Cross-File Analysis Agent is analyzing composition and dependencies...")
        analysis = _call_provider(
            provider, model_name, contents, CrossFileAnalysis,
            thinking_budget=THINKING_BUDGET_HIGH,
        )
        formatted = _format_cross_file(analysis)
        return analysis, formatted
    except Exception as e:
        _reraise_if_fatal(e)  # budget/hard → re-raised to the handler that records + contains it
        logging.error(f"Error during cross-file analysis: {describe_exc(e)}")
        return None, "Cross-file analysis failed."


def find_dependent_files(lake_graph_str: str, changed_files: set, repo_files_by_path: Dict[str, str],
                         max_dependents: int) -> Dict[str, str]:
    """Map depth-1 dependents (unchanged modules that import a changed module) to
    the file content we already have on hand. Returns {path: content}, capped at
    `max_dependents`. Empty if the graph is unavailable or max_dependents <= 0."""
    if max_dependents <= 0 or not lake_graph_str:
        return {}
    try:
        graph = json.loads(lake_graph_str)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(graph, list):
        return {}
    graph = [
        module for module in graph
        if isinstance(module, dict)
        and isinstance(module.get("name"), str)
        and isinstance(module.get("imports", []), list)
        and all(isinstance(import_name, str) for import_name in module.get("imports", []))
    ]

    changed_modules = {file_path_to_module_name(f) for f in changed_files}
    dependent_modules = {
        m['name'] for m in graph
        if m.get('name') not in changed_modules
        and any(imp in changed_modules for imp in m.get('imports', []))
    }

    # First path seen for each module (content already read during discovery).
    path_by_module: Dict[str, str] = {}
    for path in repo_files_by_path:
        path_by_module.setdefault(file_path_to_module_name(path), path)

    dependents: Dict[str, str] = {}
    for mod in sorted(dependent_modules):
        path = path_by_module.get(mod)
        if path and path not in changed_files:
            dependents[path] = repo_files_by_path[path]
        if len(dependents) >= max_dependents:
            break
    return dependents


def analyze_dependent_impact(provider: LLMProvider, dependents: Dict[str, str], all_diffs: str,
                             spec_checklist: str, external_parts: list, model_name: str,
                             max_workers: int = 5) -> Optional[CrossFileAnalysis]:
    """Second-order pass: review each unchanged dependent for breakage caused by
    the PR's changes. Returns a merged CrossFileAnalysis carrying the breakages,
    or None if there are no dependents or nothing to report. Each dependent is
    reviewed independently and in parallel."""
    if not dependents:
        return None

    def review_one(path: str, content: str) -> Optional[CrossFileAnalysis]:
        replacements = _fit_prompt_to_budget(
            "dependent_impact.md",
            {
                "DEPENDENT_PATH": path,
                "DEPENDENT_CONTENT": content,
                "ALL_DIFFS": all_diffs,
                "SPEC_CHECKLIST": spec_checklist or "No specification checklist provided.",
            },
            max_chars=_call_prompt_char_budget(
                provider,
                thinking_budget=THINKING_BUDGET_LOW,
                external_parts=external_parts,
                parallelism=max_workers,
            ),
            context_label=f"dependent-impact:{path}",
            trimmable=("DEPENDENT_CONTENT", "ALL_DIFFS"),
        )
        prompt_text = _load_prompt("dependent_impact.md", replacements)
        contents = _build_contents(prompt_text, external_parts)
        return _call_provider(provider, model_name, contents, CrossFileAnalysis, thinking_budget=THINKING_BUDGET_LOW)

    results: List[CrossFileAnalysis] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {executor.submit(review_one, p, c): p for p, c in dependents.items()}
        for future in future_to_path:
            path = future_to_path[future]
            try:
                results.append(future.result())
            except Exception as e:
                _reraise_if_fatal(e)  # budget/hard → re-raised to the recording handler
                logging.warning(f"Dependent-impact review failed for {path}: {describe_exc(e)}")

    merged = CrossFileAnalysis(
        analysis="\n\n".join(r.analysis for r in results if r and r.analysis),
        composition_issues=[f for r in results if r for f in r.composition_issues],
        escape_hatch_impact=[f for r in results if r for f in r.escape_hatch_impact],
        external_dependency_issues=[f for r in results if r for f in r.external_dependency_issues],
        missing_cross_file_verification=[f for r in results if r for f in r.missing_cross_file_verification],
    )
    if not (merged.composition_issues or merged.escape_hatch_impact
            or merged.external_dependency_issues or merged.missing_cross_file_verification):
        return None
    return merged


def _merge_cross_file(base: Optional[CrossFileAnalysis], extra: CrossFileAnalysis) -> CrossFileAnalysis:
    """Fold dependent-impact findings into the cross-file result so they flow
    through verification, the verdict, and rendering via the existing path."""
    if base is None:
        return extra
    for cat in _CROSS_FILE_CATEGORIES:
        setattr(base, cat, getattr(base, cat) + getattr(extra, cat))
    if extra.analysis:
        base.analysis = (base.analysis + "\n\n" + extra.analysis) if base.analysis else extra.analysis
    return base


def _format_synthesis(summary: SynthesisSummary, precheck_summary: Optional[str] = None) -> str:
    """Formats a structured SynthesisSummary into markdown.

    Synthesis is a narrative aggregation step, not an additional source of
    verdict facts. Its findings are therefore labeled as context and use the
    same grounding/advisory split as per-file reviews; the deterministic
    verdict and basis rendered above remain authoritative.
    """
    parts = [f"**TL;DR:** {summary.tldr}\n"]
    # Mechanical facts are authoritative and must not be paraphrased by the
    # synthesis model. The optional argument is used by the orchestration path;
    # retaining the fallback keeps this formatter useful in isolated callers.
    exact_prechecks = precheck_summary if precheck_summary is not None else summary.precheck_summary
    parts.append(f"**Mechanical Pre-Check Results:** {exact_prechecks}\n")

    if summary.checklist_coverage:
        parts.append(f"**Checklist Coverage:** {summary.checklist_coverage}\n")

    if summary.cross_file_summary:
        parts.append(f"**Cross-File Issues:** {summary.cross_file_summary}\n")

    synthesized = [
        ("Critical Misformalizations", summary.critical_misformalizations),
        ("Key Lean 4 / Mathlib Issues", summary.key_lean_issues),
    ]
    for title, findings in synthesized:
        blocking = [f for f in findings if _has_blocking_grounding(f)]
        advisory = [f for f in findings if not _has_blocking_grounding(f)]
        if blocking:
            parts.append(f"**{title} (synthesis context; not verdict basis):**")
            for f in blocking:
                parts.extend(_finding_lines(f))
            parts.append("")
        if advisory:
            parts.append(f"<details><summary>💡 <b>{title} (advisory synthesis context)</b></summary>\n")
            for f in advisory:
                parts.extend(_finding_lines(f, include_fix=False))
            parts.append("\n</details>\n")

    parts.append("_The deterministic verdict and basis above are authoritative; this synthesis is context only._\n")
    parts.append(f"**Synthesis Agent Assessment:** {summary.overall_verdict}")
    return "\n".join(parts)


def synthesize_overall_summary(provider: LLMProvider, per_file_reviews: Dict[str, str], per_file_structured: Dict[str, 'FileReview'], spec_checklist: str, pre_check_findings: str, cross_file_analysis: str, verdict_rules: str, model_name: str) -> Tuple[SynthesisSummary, str]:
    """Generates a structured high-level summary. Returns (SynthesisSummary, formatted markdown).
    On error returns (None, error_message)."""
    if not per_file_reviews:
        return None, "No files were reviewed."

    formatted_reviews = "\n\n".join(f"### Review for `{file_path}`:\n{review_text}" for file_path, review_text in per_file_reviews.items())

    # Build compact structured summary for accurate counting/deduplication
    structured_data = {}
    for file_path, review in per_file_structured.items():
        if review is None:
            continue
        structured_data[file_path] = {
            "verdict": review.verdict,
            "critical_count": len(review.critical_misformalizations),
            "issue_count": len(review.lean_issues),
            "nitpick_count": len(review.nitpicks),
            "violated_checklist": [cr.item for cr in review.checklist_results if cr.status == "violated"],
            "unclear_checklist": [cr.item for cr in review.checklist_results if cr.status == "unclear"],
        }
    structured_json = json.dumps(structured_data, indent=2)

    replacements = {
        "PER_FILE_REVIEWS": formatted_reviews,
        "STRUCTURED_REVIEWS": structured_json,
        "SPEC_CHECKLIST": spec_checklist or "No explicit checklist provided.",
        "PRE_CHECK_FINDINGS": pre_check_findings or "No issues detected.",
        "CROSS_FILE_ANALYSIS": cross_file_analysis or "No cross-file analysis performed.",
        "VERDICT_RULES": verdict_rules,
    }
    try:
        # The verbose per-file reviews are the trimmable bulk; the compact
        # STRUCTURED_REVIEWS keeps counts/verdicts intact for the summary even if
        # the prose is trimmed on a very large PR.
        replacements = _fit_prompt_to_budget(
            "synthesize_summary.md", replacements, context_label="synthesis",
            max_chars=_call_prompt_char_budget(provider, thinking_budget=THINKING_BUDGET_LOW),
            trimmable=("PER_FILE_REVIEWS",),
        )
        prompt = _load_prompt("synthesize_summary.md", replacements)
    except FileNotFoundError:
        return None, "Error: Prompt template 'synthesize_summary.md' not found"

    try:
        logging.info("Synthesizing overall summary...")
        contents = _build_contents(prompt)
        summary = _call_provider(provider, model_name, contents, SynthesisSummary, thinking_budget=THINKING_BUDGET_LOW)
        formatted = _format_synthesis(summary, precheck_summary=pre_check_findings)
        return summary, formatted
    except Exception as e:
        _reraise_if_fatal(e)  # budget/hard → re-raised to the handler that records + contains it
        logging.error(f"Error during summary synthesis: {describe_exc(e)}")
        return None, "An error occurred while synthesizing the summary."


def _verify_one_finding(provider: LLMProvider, finding: Finding, context: str, model: str,
                        budget_parallelism: int = 1,
                        toolbox: Optional[LeanToolbox] = None,
                        external_parts: Optional[List[ContentPart]] = None) -> FindingVerdict:
    """Run the adversarial verifier on a single finding. Raises on API failure.
    With a toolbox, the verifier can check the claim against the Lean toolchain
    (e.g. actually elaborate the code a reviewer says won't typecheck)."""
    replacements = _fit_prompt_to_budget(
        "verify_finding.md",
        {
            "FINDING_DESCRIPTION": finding.description,
            "FINDING_LOCATION": finding.location or "(unspecified)",
            "FINDING_EVIDENCE": finding.evidence or "(none provided)",
            "FINDING_EVIDENCE_SOURCE": finding.evidence_source or "(unspecified)",
            "FINDING_EVIDENCE_LOCATOR": finding.evidence_locator or "(unspecified)",
            "FINDING_EVIDENCE_MEDIUM": finding.evidence_medium or "(unspecified)",
            "CONTEXT": context,
        },
        max_chars=_call_prompt_char_budget(
            provider,
            thinking_budget=THINKING_BUDGET_LOW,
            external_parts=external_parts,
            parallelism=budget_parallelism,
        ),
        context_label="verify",
        trimmable=("CONTEXT",),
    )
    prompt_text = _load_prompt("verify_finding.md", replacements)
    contents = _build_contents(prompt_text, external_parts)
    return _call_provider(provider, model, contents, FindingVerdict,
                          thinking_budget=THINKING_BUDGET_LOW, toolbox=toolbox)


def verify_findings(provider: LLMProvider, per_file_structured: Dict[str, FileReview],
                    cross_file_structured: Optional[CrossFileAnalysis], diff_by_file: Dict[str, str],
                    spec_checklist: str, model: str, max_workers: int = 5,
                    external_parts: Optional[List[ContentPart]] = None) -> List[Tuple[Finding, FindingVerdict]]:
    """Precision stage: independently (adversarially) verify each verdict-driving
    finding — the per-file critical misformalizations and Lean issues, plus all
    cross-file findings. Findings the verifier can *refute* are removed from the
    structured reviews in place; everything else is kept.

    Fail-open by design: any verifier error, or any verdict other than an
    explicit "refuted", keeps the finding — verification only ever *removes*
    false positives, never suppresses a finding it could not disprove.

    Returns the list of (finding, verdict) pairs that were dropped, for a
    transparency section in the review comment.
    """
    spec = spec_checklist or "No specification checklist provided."

    file_context: Dict[str, str] = {}

    def ctx_for_file(fp: str) -> str:
        if fp not in file_context:
            content = file_cache.read(fp) or "(file content unavailable)"
            diff = diff_by_file.get(fp, "(no diff)")
            file_context[fp] = (
                f"Specification checklist:\n{spec}\n\n"
                f"File under review: {fp}\n\nDiff:\n{diff}\n\nFull content:\n{content}"
            )
        return file_context[fp]

    cross_ctx_cache: List[Optional[str]] = [None]

    def ctx_for_cross() -> str:
        if cross_ctx_cache[0] is None:
            chunks = []
            for fp in diff_by_file:
                if fp.endswith('.lean'):
                    content = file_cache.read(fp)
                    if content is not None:
                        chunks.append(f"--- {fp} ---\n{content}")
            cross_ctx_cache[0] = f"Specification checklist:\n{spec}\n\nChanged files:\n" + "\n\n".join(chunks)
        return cross_ctx_cache[0]

    # Collect (finding, context, module) jobs. `module` scopes the Lean toolbox
    # to the finding's file (None for cross-file findings, which span files).
    jobs: List[Tuple[Finding, str, Optional[str]]] = []
    for fp, review in per_file_structured.items():
        if review is None:
            continue
        module = file_path_to_module_name(fp)
        for f in review.critical_misformalizations + review.lean_issues:
            jobs.append((f, ctx_for_file(fp), module))
    if cross_file_structured is not None:
        for cat in _CROSS_FILE_CATEGORIES:
            for f in getattr(cross_file_structured, cat):
                jobs.append((f, ctx_for_cross(), None))

    if not jobs:
        return []

    logging.info(f"Verification pass: checking {len(jobs)} finding(s)...")
    verdicts: Dict[int, Optional[FindingVerdict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_finding = {
            executor.submit(
                _verify_one_finding,
                provider, f, ctx, model, max_workers, _make_toolbox(module),
                external_parts,
            ): f
            for (f, ctx, module) in jobs
        }
        for future in future_to_finding:
            finding = future_to_finding[future]
            try:
                verdicts[id(finding)] = future.result()
            except Exception as e:
                _reraise_if_fatal(e)  # budget/hard → re-raised to the recording handler
                logging.warning(f"Verification errored for a finding (keeping it): {describe_exc(e)}")
                verdicts[id(finding)] = None  # fail-open

    refuted: List[Tuple[Finding, FindingVerdict]] = []

    def survives(f: Finding) -> bool:
        v = verdicts.get(id(f))
        if v is not None and v.verdict == "refuted":
            refuted.append((f, v))
            return False
        if v is not None and v.verdict == "confirmed":
            # PDF-backed evidence must be visually checked against the original
            # PDF supplied to this verifier. Text-only confirmation is not enough
            # because equations/layout may have been lost during extraction.
            if _is_pdf_evidence(f) and v.confirmation_method != "visual":
                logging.warning(
                    "Verifier confirmed a PDF finding without visual confirmation; keeping it advisory: %s",
                    f.description,
                )
            else:
                f.confirmation_method = v.confirmation_method if v.confirmation_method != "unconfirmed" else "text"
                f.verification_status = "confirmed"
                # Severity correction, DOWNWARD only: the verifier may down-rank
                # a confirmed-but-over-escalated finding without discarding the
                # underlying true fact. Never upward — a single model must not be
                # able to push a finding over the blocking severity bar without
                # the grounding the deterministic gate demands.
                if (v.corrected_severity
                        and _SEVERITY_RANK.get(v.corrected_severity, 99)
                        < _SEVERITY_RANK.get(f.severity, -1)):
                    logging.info(
                        "Verifier corrected severity %s -> %s for: %s",
                        f.severity, v.corrected_severity, f.description[:80],
                    )
                    f.severity = v.corrected_severity
        return True

    for fp, review in per_file_structured.items():
        if review is None:
            continue
        review.critical_misformalizations = [f for f in review.critical_misformalizations if survives(f)]
        review.lean_issues = [f for f in review.lean_issues if survives(f)]
    if cross_file_structured is not None:
        for cat in _CROSS_FILE_CATEGORIES:
            setattr(cross_file_structured, cat, [f for f in getattr(cross_file_structured, cat) if survives(f)])

    logging.info(f"Verification pass: {len(jobs)} checked, {len(refuted)} refuted and dropped.")
    return refuted


def _get_diff_lines(diff_text: str) -> set:
    """Returns the set of line numbers (in the new file) that appear in the diff.
    Used for mapping findings to GitHub Review API line annotations."""
    diff_lines = set()
    current_line = 0
    in_hunk = False

    for line in diff_text.splitlines():
        if line.startswith('@@'):
            m = re.search(r'\+(\d+)', line)
            if m:
                current_line = int(m.group(1)) - 1
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if line.startswith('+'):
            current_line += 1
            diff_lines.add(current_line)
        elif line.startswith('-'):
            pass  # deleted line, don't advance new-file counter
        elif line == r'\ No newline at end of file':
            # This unified-diff metadata is not a source line. Counting it as
            # context shifts every later line in the hunk and mis-anchors
            # GitHub inline comments.
            continue
        else:
            current_line += 1
            diff_lines.add(current_line)  # context line

    return diff_lines


def _build_line_annotations(per_file_structured: Dict[str, FileReview], diff_by_file: Dict[str, str]) -> List[Dict]:
    """Builds GitHub Review API comment annotations from structured reviews.
    Returns a list of {path, line, side, body} dicts using the modern API."""
    annotations = []

    for file_path, review in per_file_structured.items():
        if review is None:
            continue

        diff = diff_by_file.get(file_path, "")
        diff_lines = _get_diff_lines(diff)

        all_findings = []
        for f in review.critical_misformalizations:
            all_findings.append(("🔴 Critical", f))
        for f in review.lean_issues:
            all_findings.append(("🟡 Issue", f))
        # Inline annotations are reserved for findings that meet the grounding
        # contract. All other substantive reports remain in the advisory section
        # of the main review comment; they are not silently discarded.

        for severity, finding in all_findings:
            if not _has_blocking_grounding(finding):
                continue
            if not finding.location:
                continue

            location_path, _ = _finding_location(finding.location)
            normalized_file_path = file_path.strip().lower().removeprefix("./")
            if location_path.removeprefix("./") != normalized_file_path:
                # A model can cite another file in a cross-file explanation;
                # never attach that finding to the current file by accident.
                continue

            m = re.search(r':(\d+)(?:-(\d+))?', finding.location)
            if not m:
                continue

            start_line = int(m.group(1))
            end_line = int(m.group(2) or m.group(1))
            target_lines = sorted(line for line in diff_lines if start_line <= line <= end_line)
            if not target_lines:
                # Keep the finding in the main comment, but do not invent a
                # nearby anchor: inline feedback must identify exact changed
                # code to remain actionable.
                continue
            target_line = target_lines[0]

            body = f"**{severity}:** {finding.description}"
            if finding.confidence:
                body += f" _(confidence: {finding.confidence})_"
            if finding.evidence:
                body += f"\n\n**Evidence:** {finding.evidence}"
            if finding.evidence_source:
                body += f"\n\n**Evidence source:** `{finding.evidence_source}`"
            if finding.evidence_locator:
                body += f"\n\n**Evidence locator:** `{finding.evidence_locator}`"
            if finding.evidence_medium != "unknown":
                body += f"\n\n**Evidence medium:** `{finding.evidence_medium}`"
            if finding.confirmation_method != "unconfirmed":
                body += f"\n\n**Confirmation method:** `{finding.confirmation_method}`"
            if finding.verification_status == "confirmed":
                body += "\n\n**Verification:** independently confirmed"
            if finding.suggested_fix:
                body += f"\n\n**Suggested fix:** {finding.suggested_fix}"
            if finding.how_to_confirm:
                body += f"\n\n**How to confirm:** {finding.how_to_confirm}"

            annotations.append({
                "path": file_path,
                "line": target_line,
                "side": "RIGHT",
                "body": body
            })

    return annotations


def compute_deterministic_verdict(
    precheck_scan: Dict[str, list],
    per_file_structured: Dict[str, "FileReview"],
    cross_file_structured: Optional["CrossFileAnalysis"],
    review_incomplete: bool,
    verification_required: bool = False,
) -> Tuple[str, List[str]]:
    """Compute the authoritative overall verdict from mechanical facts and
    structured findings, rather than trusting the synthesis LLM to apply the
    verdict rules. Returns (verdict, reasons).

    Rules (worst wins):
      * Introduced, non-allow-listed escape hatch  -> Changes Requested (hard rule)
      * Any critical/high-severity, medium/high-confidence, source-grounded
        critical/Lean issue -> Changes Requested
      * Any critical/high-severity, medium/high-confidence, source-grounded
        cross-file issue   -> Changes Requested
      * Verification may refute and remove findings, but uncertainty does not
        erase a grounded issue; unconfirmed findings remain visible and can block
      * Docstring-only, low-confidence, or otherwise ungrounded findings remain advisory
      * Only nitpicks/advisory findings                -> Needs Minor Revisions
      * A file that could not be fully reviewed     -> at least Needs Minor
        Revisions (a coverage gap must never be certified as Approved)
      * Otherwise                                  -> Approved
    """
    verdict = "Approved"
    reasons: List[str] = []

    def bump(new_verdict: str) -> None:
        nonlocal verdict
        if _VERDICT_RANK[new_verdict] > _VERDICT_RANK[verdict]:
            verdict = new_verdict

    triggering = introduced_hatches_triggering_verdict(precheck_scan)
    if triggering:
        kws = ", ".join(sorted({f"`{kw}`" for _f, kw, _s in triggering}))
        bump("Changes Requested")
        reasons.append(f"Escape hatch(es) introduced in this PR ({kws}) — hard verdict rule.")

    # Low-confidence model reports remain visible in the comment, but do not
    # independently block a PR. This is the first R1 step toward separating
    # recall (showing a plausible issue) from precision (blocking only on a
    # sufficiently grounded issue); deterministic escape-hatch rules above are
    # unaffected by model confidence.
    def blocking(findings: list[Finding]) -> list[Finding]:
        return [f for f in findings if _has_blocking_grounding(f, verification_required)]

    n_critical = sum(len(blocking(r.critical_misformalizations)) for r in per_file_structured.values() if r)
    n_lean = sum(len(blocking(r.lean_issues)) for r in per_file_structured.values() if r)
    n_advisory_substantive = sum(
        sum(1 for f in r.critical_misformalizations + r.lean_issues if not _has_blocking_grounding(f, verification_required))
        for r in per_file_structured.values() if r
    )
    n_nit = sum(len(r.nitpicks) for r in per_file_structured.values() if r)
    if n_critical or n_lean:
        bump("Changes Requested")
        reasons.append(f"{n_critical} critical misformalization(s) and {n_lean} Lean/Mathlib issue(s) across files.")
    elif n_nit or n_advisory_substantive:
        bump("Needs Minor Revisions")
        advisory_parts = []
        if n_nit:
            advisory_parts.append(f"{n_nit} nitpick(s)")
        if n_advisory_substantive:
            advisory_parts.append(f"{n_advisory_substantive} unconfirmed substantive finding(s)")
        reasons.append(" and ".join(advisory_parts) + ".")

    if cross_file_structured is not None:
        cf = cross_file_structured
        cf_blocking = sum(
            len(blocking(getattr(cf, cat))) for cat in _CROSS_FILE_CATEGORIES
        )
        cf_advisory = sum(
            sum(1 for f in getattr(cf, cat) if not _has_blocking_grounding(f, verification_required))
            for cat in _CROSS_FILE_CATEGORIES
        )
        if cf_blocking:
            bump("Changes Requested")
            reasons.append(f"{cf_blocking} cross-file issue(s).")
        elif cf_advisory:
            bump("Needs Minor Revisions")
            reasons.append(f"{cf_advisory} unconfirmed cross-file issue(s) require review.")

    if review_incomplete:
        bump("Needs Minor Revisions")
        reasons.append(
            "One or more files could not be fully reviewed — this coverage gap "
            "prevents an 'Approved' certification."
        )

    if not reasons:
        reasons.append("No escape hatches, misformalizations, Lean issues, or cross-file issues detected.")
    return verdict, reasons


def _format_verdict_basis(verdict: str, reasons: List[str]) -> str:
    """Render the deterministic verdict and its basis for the PR comment."""
    lines = [f"**Verdict (deterministic): {verdict}**", "", "_Basis:_"]
    lines.extend(f"- {r}" for r in reasons)
    return "\n".join(lines)


def split_into_comments(body: str, max_size: int = MAX_GITHUB_COMMENT_SIZE) -> List[str]:
    """Split an oversized review into multiple comment-sized parts instead of
    truncating (which loses the tail and can sever markdown). Splits preferably
    at ``<details>`` section boundaries; an individual section larger than the
    limit is hard-sliced as a last resort. Continuation parts get a header."""
    if len(body) <= max_size:
        return [body]

    reserve = 80  # headroom for the continuation header
    limit = max(1, max_size - reserve)

    segments = [s for s in re.split(r'(?=\n<details>)', body) if s]
    blocks: List[str] = []
    for seg in segments:
        if len(seg) <= limit:
            blocks.append(seg)
        else:
            for i in range(0, len(seg), limit):
                blocks.append(seg[i:i + limit])

    parts: List[str] = []
    cur = ""
    for b in blocks:
        if not cur:
            cur = b
        elif len(cur) + len(b) <= limit:
            cur += b
        else:
            parts.append(cur)
            cur = b
    if cur:
        parts.append(cur)

    n = len(parts)
    if n > 1:
        parts = [parts[0]] + [
            f"> _AI review continued (part {i}/{n})_\n\n{p}"
            for i, p in enumerate(parts[1:], start=2)
        ]
    return parts


def main():
    parser = argparse.ArgumentParser(description="AI Code Reviewer")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--external-refs", default="")
    parser.add_argument("--spec-refs", default="", help="Comma-separated local paths (files or dirs) to specification/knowledge-base documents that should drive the formalization checklist.")
    parser.add_argument("--repo-context-refs", default="")
    parser.add_argument("--additional-comments", default="")
    parser.add_argument("--model", default="", help="Default OpenRouter model slug for all agents (e.g. anthropic/claude-opus-4.8)")
    parser.add_argument("--spec-model", default="", help="Model for Agent A (spec analysis). Falls back to --model")
    parser.add_argument("--triage-model", default="", help="Model for Triage agent. Falls back to --model")
    parser.add_argument("--review-model", default="", help="Model for Agent B (per-file review). Falls back to --model")
    parser.add_argument("--cross-file-model", default="", help="Model for cross-file analysis. Falls back to --model")
    parser.add_argument("--synthesis-model", default="", help="Model for synthesis. Falls back to --model")
    parser.add_argument("--verify-model", default="", help="Model for the verification pass. Falls back to --review-model, then --model. Use a DIFFERENT model than review for independent verification.")
    parser.add_argument("--skip-verification", action="store_true", help="Skip the adversarial verification pass over findings.")
    parser.add_argument("--thinking-budget", type=int, default=10240, help="Thinking token budget for deep-analysis agents. Triage and Synthesis use 1/5 of this.")
    parser.add_argument("--max-workers", type=int, default=5, help="Max parallel threads for per-file review.")
    parser.add_argument("--enable-web-search", action="store_true", help="Enable OpenRouter web-search grounding for agents (adds cost).")
    parser.add_argument("--no-lean-tools", action="store_true", help="Disable Lean toolchain access (lean_check/print/typecheck) for the reviewer and verifier.")
    args = parser.parse_args()

    # Validate inputs
    if args.thinking_budget < 0:
        logging.error("--thinking-budget must be a non-negative integer.")
        sys.exit(1)
    if args.max_workers < 1:
        logging.error("--max-workers must be at least 1.")
        sys.exit(1)

    # Resolve per-agent models (fall back to default)
    if not args.model:
        args.model = os.environ.get("MODEL", "")
    args.spec_model = args.spec_model or args.model
    args.triage_model = args.triage_model or args.model
    args.review_model = args.review_model or args.model
    args.cross_file_model = args.cross_file_model or args.model
    args.synthesis_model = args.synthesis_model or args.model
    # Verifier defaults to the review model, but a different model gives a more
    # independent check (avoids single-model self-agreement bias).
    args.verify_model = args.verify_model or os.environ.get("VERIFY_MODEL", "") or args.review_model
    verification_enabled = (
        not args.skip_verification
        and os.environ.get("VERIFY_FINDINGS", "true").lower() not in ("false", "0", "no")
    )

    # Configure thinking budgets
    global THINKING_BUDGET_HIGH, THINKING_BUDGET_LOW
    THINKING_BUDGET_HIGH = args.thinking_budget
    THINKING_BUDGET_LOW = max(1024, args.thinking_budget // 5)

    # Delete any stale output files before we run — and before EVERY early
    # exit below. The Lean build step executes PR-branch lakefile code in this
    # same workspace BEFORE review.py, and lean_tools run model-directed Lean
    # IO — either could plant a crafted review_annotations.json /
    # review_comments.json (posted verbatim by Post Review) or a
    # review_health.json (read by the shell step). This must precede the
    # no-Lean-files return and the API_KEY/budget exits: those paths still let
    # Post Review run, so a planted file would otherwise be posted verbatim.
    for _stale in ("review_annotations.json", "review_comments.json", REVIEW_HEALTH_FILE):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as _e:
            logging.warning(f"Could not remove stale {_stale}: {describe_exc(_e)}")

    diff, diff_errors = get_pr_diff(args.pr_number)
    if diff_errors and not diff:
        logging.error("Aborting review: Could not fetch PR diff. Errors:\n" + "\n".join(diff_errors))
        sys.exit(1)

    diff_by_file = split_diff_into_files(diff)
    lean_files = {f: d for f, d in diff_by_file.items() if f.endswith('.lean')}
    if not lean_files:
        print("### 🤖 AI Review\n\nNo Lean files were changed in this PR.")
        return

    context_warnings = []

    if not args.spec_refs:
        args.spec_refs = os.environ.get("SPEC_REFS", "")

    # Freeform ChatOps: `/review <anything>` arrives whole as additional
    # comments. URLs and repo paths it mentions become review context
    # automatically; the text itself still reaches the agents unmodified.
    instr_urls, instr_spec, instr_repo = extract_refs_from_instructions(args.additional_comments)
    if instr_urls or instr_spec or instr_repo:
        logging.info(
            f"References extracted from instructions: {len(instr_urls)} URL(s), "
            f"{len(instr_spec)} spec file(s), {len(instr_repo)} repo path(s)."
        )
    args.external_refs = _merge_csv(args.external_refs, instr_urls)
    args.spec_refs = _merge_csv(args.spec_refs, instr_spec)
    args.repo_context_refs = _merge_csv(args.repo_context_refs, instr_repo)

    # Per-item context records for the "References & context used" manifest. Collected
    # HERE — before the orchestration try — so a later abort still has whatever loaded
    # (the degraded comment renders the manifest too). Categorised at the loader so the
    # external+spec merge below can't blur which items were KB/spec vs external.
    context_records: List[ContextRecord] = []
    external_parts, external_errors = get_document_content(args.external_refs, records=context_records)
    spec_parts, spec_errors = get_local_reference_parts(args.spec_refs, records=context_records)
    # Local KB/spec docs join the shared reference prefix: they drive Agent A's
    # checklist and ground every downstream reviewer, just like external refs.
    external_parts = external_parts + spec_parts
    repo_files_by_path, repo_errors = get_repo_files_by_path(args.repo_context_refs, records=context_records)
    summary_context = get_summary_context(os.environ.get("SUMMARY_FILES", ""))

    # Appendix: non-file content (summary signatures, Lean toolchain info).
    # Kept separate from the file-content body so per-file reviewers can filter
    # the body without losing the appendix.
    repo_context_appendix = ""
    if summary_context:
        repo_context_appendix += f"\n\n--- Summary Context (type signatures only, from overflow files) ---\n{summary_context}\n"

    lean_info = os.environ.get("LEAN_INFO", "")
    if lean_info:
        repo_context_appendix += f"\n\n{lean_info}\n"
    elif os.environ.get("DISCOVERED_FILES", ""):
        context_warnings.append(
            "Lean toolchain analysis produced no output despite changed Lean files being present. "
            "Axiom dependencies and compiler diagnostics are unavailable for this review."
        )

    paper_lean_evidence = os.environ.get("PAPER_LEAN_EVIDENCE", "")
    if paper_lean_evidence:
        repo_context_appendix += f"\n\n{paper_lean_evidence}\n"
    elif args.spec_refs or args.external_refs:
        context_warnings.append(
            "The paper/Lean source index was unavailable despite specification references being configured. "
            "Paper fidelity must be checked from the supplied references directly."
        )

    build_status = os.environ.get(ENV_BUILD_STATUS, "").strip().lower()
    if not build_status:
        build_status = "success" if os.environ.get(ENV_BUILD_SUCCEEDED, "").lower() in ("1", "true", "yes") else "unavailable"
    if build_status == "success":
        repo_context_appendix += (
            "\n\n**Deterministic workflow fact:** the exact checked-out PR commit passed the "
            "workflow Lean build before this review ran. Do not report a bare claim that "
            "the changed code will not typecheck/build unless you provide new compiler or "
            "toolchain evidence for that claim.\n"
        )
    elif build_status == "failure":
        repo_context_appendix += (
            "\n\n**Deterministic workflow fact:** the exact checked-out PR commit failed the "
            "workflow Lean build. Compiler diagnostics below are authoritative for "
            "mechanical build claims; semantic findings still require independent analysis.\n"
        )
    else:
        repo_context_appendix += (
            "\n\n**Build-status note:** no workflow build status is available for this "
            "review. Treat mechanical build claims as requiring explicit compiler or "
            "toolchain evidence.\n"
        )

    # Append build output (warnings/errors captured from the explicit workflow
    # lake build). The action bounds the value before it reaches this process;
    # retain even a successful build's diagnostics because warnings can explain
    # a finding without implying that the build failed.
    build_output = os.environ.get("BUILD_OUTPUT", "")
    if build_output and build_output.strip():
        repo_context_appendix += f"\n\n**Lake Build Diagnostics (compiler output):**\n{build_output}\n"

    # Full repo_context used by cross-file analysis (no per-file filtering).
    repo_context = _format_repo_files(repo_files_by_path) + repo_context_appendix

    all_errors = external_errors + spec_errors + repo_errors
    if all_errors:
        logging.warning("Encountered non-critical errors. Review will proceed with partial context.")

    # API_KEY holds the OpenRouter key.
    api_key = os.getenv("API_KEY")
    if not api_key:
        logging.error("Error: API_KEY not set (expects an OpenRouter API key).")
        sys.exit(1)

    enable_web_search = args.enable_web_search or os.environ.get("ENABLE_WEB_SEARCH", "").lower() in ("1", "true", "yes")

    # Per-run spend ceiling (C3). Operator-config from the workflow/secrets env only.
    # Empty/whitespace == unset == disabled (the value a default action run sends);
    # a non-empty invalid value or a cost-only budget fails fast here, before any LLM
    # call, rather than shipping the feature dark or silently unbounded.
    try:
        budget = parse_run_budget(os.environ.get(ENV_MAX_RUN_TOKENS), os.environ.get(ENV_MAX_RUN_COST))
    except ValueError as e:
        logging.error(f"Invalid per-run budget configuration ({ENV_MAX_RUN_TOKENS}/{ENV_MAX_RUN_COST}): {e}")
        sys.exit(1)
    budget_mode = os.environ.get(ENV_BUDGET_MODE, "advisory").strip().lower() or "advisory"
    if budget_mode not in ("advisory", "hard"):
        logging.error(f"Invalid {ENV_BUDGET_MODE}: expected 'advisory' or 'hard', got {budget_mode!r}")
        sys.exit(1)
    global run_health
    run_health = RunHealth()

    hard_budget = budget if budget_mode == "hard" else None
    provider = create_provider(api_key, enable_web_search=enable_web_search, budget=hard_budget)
    # In advisory mode, the configured budget is still tracked and used by
    # _call_prompt_char_budget to right-size later prompts, but it never aborts
    # coverage. Hard mode preserves the old ceiling semantics.
    provider.prompt_budget = budget
    logging.info(f"Using LLM provider: {provider.name}"
                 + (" (web search enabled)" if enable_web_search else ""))
    if budget is not None:
        logging.info(
            f"Per-run budget active ({budget_mode}): max_tokens={budget.max_tokens} "
            f"max_cost={budget.max_cost}"
        )

    # Lean toolchain access for the reviewer + verifier: enabled by default, but
    # only when requested and `lake` is actually available in the runner.
    global LEAN_TOOLS_ENABLED
    lean_tools_requested = (
        not args.no_lean_tools
        and os.environ.get("LEAN_TOOLS", "true").lower() not in ("false", "0", "no")
    )
    LEAN_TOOLS_ENABLED = lean_tools_requested and lean_available()
    if lean_tools_requested and not LEAN_TOOLS_ENABLED:
        logging.warning("Lean tools requested but `lake` was not found; agents will run without them.")
    logging.info(f"Lean toolchain tools: {'enabled' if LEAN_TOOLS_ENABLED else 'disabled'}")

    # Partial-result accumulators, initialized BEFORE the orchestration try so the
    # top-level containment handler can render whatever completed if a fatal (budget
    # trip / hard LLM failure) aborts the run before these are reached inside the try.
    per_file_reviews, per_file_structured, review_errors = {}, {}, []
    cross_file_text, cross_file_structured = "", None
    refuted_findings, clusters, filtered_findings = [], [], []

    try:
        review_context = {
            "external_context": "[Multimodal Content Provided]",
            "repo_context": repo_context,
            "repo_files_by_path": repo_files_by_path,
            "repo_context_appendix": repo_context_appendix,
            "changed_files": set(diff_by_file.keys()),
            "additional_comments": args.additional_comments,
            "review_model": args.review_model,
            "max_workers": args.max_workers,
            "build_succeeded": build_status == "success",
        }
        
        lean4_checklist_path = os.path.join(ACTION_PATH, "prompts", "lean4_checklist.md")
        try:
            with open(lean4_checklist_path, "r") as f:
                lean4_checklist = f.read()
        except FileNotFoundError:
            logging.error(f"Error: lean4_checklist.md not found at {lean4_checklist_path}")
            sys.exit(1)

        verdict_rules_path = os.path.join(ACTION_PATH, "prompts", "verdict_rules.md")
        try:
            with open(verdict_rules_path, "r") as f:
                verdict_rules = f.read()
        except FileNotFoundError:
            logging.error(f"Error: verdict_rules.md not found at {verdict_rules_path}")
            sys.exit(1)

        all_diffs = "\n".join([f"--- {f} ---\n{d}" for f, d in diff_by_file.items()])

        # --- Multi-Agent Orchestration Step 0: Mechanical Pre-Checks ---
        precheck_scan = scan_escape_hatches(diff_by_file)
        pre_check_findings = format_prechecks(precheck_scan)
        has_findings = bool(
            precheck_scan["introduced"] or precheck_scan["preexisting"] or precheck_scan["large_files"]
        )
        logging.info(f"Pre-check complete: {'findings detected' if has_findings else 'clean'}.")

        # --- Multi-Agent Orchestration Step 1: Spec Analysis ---
        spec_checklist = analyze_specification(
            provider, external_parts, args.spec_model, all_diffs,
            summary_context=summary_context, lake_graph=get_lake_graph_str()
        )
        if spec_checklist:
            logging.info("Spec Analysis complete. Handing off checklist to Code Reviewers.")
        else:
            logging.info("No external specification provided or analysis failed. Proceeding with standard review.")

        # --- Multi-Agent Orchestration Step 1.5: Triage ---
        if len(lean_files) > 2:
            clusters = run_triage(provider, lean_files, spec_checklist, args.additional_comments, args.triage_model)
        elif len(lean_files) == 2:
            # Two files: single cluster without the overhead of triage
            files = list(lean_files.keys())
            clusters = [ReviewCluster(name="Changed files", files=files,
                                      review_question="Check type/interface consistency between these files.",
                                      priority="high")]
        else:
            clusters = [ReviewCluster(name=f, files=[f], review_question="", priority="medium")
                        for f in lean_files]

        # Ensure all Lean files are covered (triage might miss some)
        clustered_files = set()
        for c in clusters:
            clustered_files.update(c.files)
        unclustered = [f for f in lean_files if f not in clustered_files]
        if unclustered:
            clusters.append(ReviewCluster(name="Unclustered files", files=unclustered,
                                          review_question="Review these files independently.", priority="low"))

        # --- Multi-Agent Orchestration Step 2: Review per cluster ---
        def process_file(file_path, file_diff, review_ctx, cluster_context=""):
            """Reviews a single file, optionally with cluster context."""
            if not file_path.endswith(".lean"):
                logging.info(f"Skipping non-Lean file: {file_path}")
                return None, None, None

            full_content = file_cache.read(file_path) or ""

            augmented_ctx = dict(review_ctx)
            if cluster_context:
                augmented_ctx["cluster_context"] = cluster_context

            structured_review, formatted_text = analyze_file_changes_with_context(
                provider, augmented_ctx, file_path, file_diff, full_content,
                spec_checklist, external_parts, lean4_checklist, verdict_rules
            )

            return file_path, structured_review, formatted_text

        per_file_reviews = {}      # file_path -> formatted markdown
        per_file_structured = {}   # file_path -> FileReview (or None on error)
        review_errors = []
        cluster_info = {}          # file_path -> cluster name

        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            future_to_file = {}
            for cluster in clusters:
                # Build cluster context for multi-file clusters
                cluster_context = ""
                if len(cluster.files) > 1 and cluster.review_question:
                    # Build signatures of other cluster files for type-level awareness
                    cluster_file_paths = ','.join(
                        cf for cf in cluster.files
                        if cf in diff_by_file and cf.endswith('.lean')
                    )
                    cluster_sigs = get_summary_context(cluster_file_paths)
                    cluster_parts = [
                        f"**Cluster: {cluster.name}** (Priority: {cluster.priority})",
                        f"**Cross-file question:** {cluster.review_question}",
                    ]
                    if cluster.review_strategy:
                        cluster_parts.append(f"**Review strategy:** {cluster.review_strategy}")
                    if cluster.key_hypotheses:
                        cluster_parts.append("**Key hypotheses to verify:**")
                        for hyp in cluster.key_hypotheses:
                            cluster_parts.append(f"- {hyp}")
                    cluster_parts.append(f"**Type signatures of other files in this cluster:**\n{cluster_sigs}")
                    cluster_context = "\n".join(cluster_parts)

                for file_path in cluster.files:
                    if file_path in diff_by_file:
                        cluster_info[file_path] = cluster.name
                        future_to_file[executor.submit(
                            process_file, file_path, diff_by_file[file_path],
                            review_context, cluster_context
                        )] = file_path

            for future, submitted_path in future_to_file.items():
                try:
                    file_path, structured, formatted = future.result()
                except BudgetExceededError:
                    # Idempotent (STEP 5b): once the budget trips, the already-queued
                    # workers each raise at fresh entry — that burst is ONE budget event,
                    # not N failures. Record the skipped file and keep going; the marker
                    # plus forced review_incomplete carry the signal. Do NOT re-raise.
                    run_health.record_budget_trip(submitted_path)
                    continue
                except Exception as e:
                    if is_hard_llm_failure(e):
                        raise  # hard failure → top-level containment records + contains it
                    # R6: keep the exception body out of the PR-visible error line.
                    logging.error(f"File review thread failed for {submitted_path}: {describe_exc(e)}")
                    review_errors.append(f"Agent B failed for `{_safe_md_path(submitted_path)}`.")
                    continue
                if file_path:
                    per_file_reviews[file_path] = formatted
                    per_file_structured[file_path] = structured
                    if structured is None:
                        review_errors.append(f"Agent B failed for `{file_path}`")

        # --- Multi-Agent Orchestration Step 3: Cross-File Analysis ---
        cross_file_text = ""
        cross_file_structured = None
        if len(lean_files) > 1:
            try:
                cross_file_structured, cross_file_text = analyze_cross_file(
                    provider, diff_by_file, spec_checklist, pre_check_findings,
                    repo_context, args.additional_comments, external_parts,
                    args.cross_file_model
                )
                logging.info("Cross-file analysis complete.")
            except BudgetExceededError:
                # Budget trip degrades in place: keep the completed per-file reviews,
                # drop cross-file analysis, force review_incomplete. A HARD failure is
                # NOT caught here — it propagates to the top-level containment catch.
                run_health.record_budget_trip()
                cross_file_structured, cross_file_text = None, ""
                logging.warning("Cross-file analysis skipped: per-run budget exhausted.")
        else:
            logging.info("Single file PR — skipping cross-file analysis.")
            # Deterministic downstream impact note from the import graph
            lake_graph_str = get_lake_graph_str()
            if lake_graph_str:
                try:
                    lake_graph_data = json.loads(lake_graph_str)
                    single_file = list(lean_files.keys())[0]
                    single_module = file_path_to_module_name(single_file)
                    dependent_modules = [m['name'] for m in lake_graph_data
                                         if single_module in m.get('imports', []) and m['name'] != single_module]
                    if dependent_modules:
                        dep_list = ', '.join(f'`{d}`' for d in dependent_modules[:10])
                        suffix = f' and {len(dependent_modules) - 10} more' if len(dependent_modules) > 10 else ''
                        cross_file_text = (
                            f"**Downstream Impact Note:** This file is imported by "
                            f"{len(dependent_modules)} module(s): {dep_list}{suffix}. "
                            f"Changes to public API may affect these downstream consumers."
                        )
                except Exception as e:
                    logging.warning(f"Could not generate downstream impact note: {describe_exc(e)}")

        # --- Second-order: dependent-impact pass over unchanged consumers ---
        # Re-review the unchanged depth-1 importers of the changed files for
        # breakage the diff causes. Findings are folded into the cross-file
        # result so they flow through verification, the verdict, and rendering.
        try:
            dep_max = int(os.environ.get("DEPENDENT_IMPACT_MAX", "10"))
        except ValueError:
            dep_max = 10
        dependents = find_dependent_files(
            get_lake_graph_str(), set(lean_files), repo_files_by_path, dep_max,
        )
        if dependents:
            logging.info(f"Dependent-impact: reviewing {len(dependents)} unchanged consumer(s).")
            try:
                dep_result = analyze_dependent_impact(
                    provider, dependents, all_diffs, spec_checklist, external_parts,
                    args.cross_file_model, max_workers=args.max_workers,
                )
            except BudgetExceededError:
                run_health.record_budget_trip()
                dep_result = None
                logging.warning("Dependent-impact pass skipped: per-run budget exhausted.")
            if dep_result is not None:
                cross_file_structured = _merge_cross_file(cross_file_structured, dep_result)
                cross_file_text = _format_cross_file(cross_file_structured)

        # --- Deterministic finding hygiene (before verification/verdict) ---
        # These filters use only workflow-established facts. They do not judge
        # mathematical correctness; they prevent known classes of noisy output
        # from becoming inline comments or verdict-driving findings.
        filtered_findings = _filter_ungrounded_findings(
            per_file_structured,
            cross_file_structured,
            precheck_scan,
            build_status == "success",
        )
        if filtered_findings:
            for fp, r in per_file_structured.items():
                if r is not None:
                    per_file_reviews[fp] = _format_file_review(r, fp)
            if cross_file_structured is not None:
                cross_file_text = _format_cross_file(cross_file_structured)

        # --- Verification pass (precision stage) ---
        # Adversarially re-check each verdict-driving finding and drop the ones a
        # verifier can refute, BEFORE the verdict is computed or findings are
        # rendered. Runs on a (preferably different) model to avoid self-agreement.
        refuted_findings = []
        if verification_enabled:
            try:
                refuted_findings = verify_findings(
                    provider, per_file_structured, cross_file_structured,
                    diff_by_file, spec_checklist, args.verify_model,
                    max_workers=args.max_workers,
                    external_parts=external_parts,
                )
            except BudgetExceededError:
                # Verification runs BEFORE the verdict, so recording the trip here flips
                # review_incomplete (via run_health.degraded) before the verdict computes —
                # findings go out UNFILTERED but the run can no longer render Approved.
                run_health.record_budget_trip()
                logging.warning("Verification pass skipped: per-run budget exhausted; reporting unfiltered findings.")
            except Exception as e:
                if is_hard_llm_failure(e):
                    raise  # hard failure → top-level containment records + contains it
                logging.warning(f"Verification pass failed; reporting unfiltered findings: {describe_exc(e)}")
            verifier_confirmed = any(
                f.verification_status == "confirmed"
                for r in per_file_structured.values() if r
                for f in r.critical_misformalizations + r.lean_issues
            ) or any(
                f.verification_status == "confirmed"
                for cat in _CROSS_FILE_CATEGORIES
                for f in (getattr(cross_file_structured, cat) if cross_file_structured else [])
            )
            if refuted_findings or verifier_confirmed:
                # Re-render the affected outputs from the filtered structured data.
                for fp, r in per_file_structured.items():
                    if r is not None:
                        per_file_reviews[fp] = _format_file_review(r, fp)
                if cross_file_structured is not None:
                    cross_file_text = _format_cross_file(cross_file_structured)

        # --- Deterministic verdict (authoritative) ---
        # A file is only "covered" if its structured review came back; a missing
        # or failed review is a coverage gap that must block an Approved verdict.
        reviewed_ok = {fp for fp, r in per_file_structured.items() if r is not None}
        review_incomplete = (
            bool(review_errors)
            or any(fp not in reviewed_ok for fp in lean_files)
            or any(r.coverage_incomplete for r in per_file_structured.values() if r)
            or run_health.degraded  # R9: any hard LLM failure / budget trip forces incomplete —
                                    # compute_deterministic_verdict then cannot Approve an outage run.
        )
        det_verdict, det_reasons = compute_deterministic_verdict(
            precheck_scan, per_file_structured, cross_file_structured, review_incomplete,
            verification_required=verification_enabled,
        )
        logging.info(f"Deterministic verdict: {det_verdict}")

        # --- Multi-Agent Orchestration Step 4: Synthesis ---
        if len(lean_files) == 1:
            # Single-file PR: the per-file review IS the summary — skip the
            # synthesis agent. The deterministic downstream-impact note (if any)
            # is folded in directly rather than via an LLM pass.
            logging.info("Single-file PR — skipping synthesis (per-file review is the summary).")
            summary_text = ""
            if precheck_scan["introduced"]:
                summary_text += "**Pre-Check:** Escape hatches introduced in this PR (see details below).\n"
            if cross_file_text:
                summary_text += f"\n{cross_file_text}\n"
        else:
            # Synthesis runs AFTER the authoritative verdict, so a trip here must keep
            # the verdict standing (it is already computed): degrade the narrative in
            # place, record the failure (→ banner + review_incomplete), and never
            # contradict the verdict. Budget and hard failures both degrade here rather
            # than aborting a fully-reviewed run to a bare degraded comment.
            try:
                summary_structured, summary_text = synthesize_overall_summary(
                    provider, per_file_reviews, per_file_structured, spec_checklist,
                    pre_check_findings, cross_file_text, verdict_rules, args.synthesis_model
                )
                # The LLM writes the narrative, but the verdict is authoritative and
                # computed deterministically — override whatever the model chose.
                if summary_structured is not None:
                    summary_structured.overall_verdict = det_verdict
                    summary_text = _format_synthesis(summary_structured, precheck_summary=pre_check_findings)
            except Exception as e:
                if isinstance(e, BudgetExceededError):
                    run_health.record_budget_trip()
                elif is_hard_llm_failure(e):
                    run_health.record_hard_failure()
                else:
                    raise
                logging.warning(f"Synthesis degraded: {describe_exc(e)}")
                summary_text = "*Overall summary unavailable — the run degraded before synthesis completed; the per-file reviews above stand.*"

        # Synthesis (and its degradation) happens AFTER the verdict is computed, so a
        # failure there flips run_health.degraded without having flipped review_incomplete.
        # Re-derive the verdict when that occurs, so an outage can never leave an
        # "Approved" standing directly under the CAUTION banner (R9 for the post-verdict
        # path). Idempotent when degraded was already reflected before the verdict.
        if run_health.degraded and not review_incomplete:
            review_incomplete = True
            det_verdict, det_reasons = compute_deterministic_verdict(
                precheck_scan, per_file_structured, cross_file_structured, review_incomplete,
                verification_required=verification_enabled,
            )
            logging.info(f"Verdict re-derived after post-verdict degradation: {det_verdict}")

        # Format the final comment for printing to stdout. The authoritative
        # verdict + its basis lead so the reader sees it first.
        final_comment = (
            _review_comment_header(args.additional_comments)
            + ((_LOUD_BANNER + "\n" + _skipped_marker() + "\n") if run_health.degraded else "")
            + f"{_format_verdict_basis(det_verdict, det_reasons)}\n\n"
            f"**Overall Summary:**\n{summary_text}\n\n---\n"
        )

        if review_errors:
            final_comment += "\n**Errors during review:**\n"
            for err in review_errors:
                final_comment += f"- {err}\n"
            final_comment += "\n---\n"

        all_warnings = all_errors + context_warnings
        if all_warnings:
            final_comment += "\n<details><summary>**Context Warnings**</summary>\n\n"
            final_comment += "The following issues occurred while gathering context. The review proceeded with partial information:\n\n"
            for w in all_warnings:
                final_comment += f"- {w}\n"
            final_comment += "\n</details>\n"

        # What context the reviewers were actually given (external refs, KB/spec, repo
        # files). Failures already appear once above in Context Warnings; this lists what
        # loaded — the pilot's "did it pull the KB?" signal.
        final_comment += _render_reference_manifest(context_records)

        if has_findings:
            final_comment += f"\n<details><summary>🔍 **Mechanical Pre-Check Results**</summary>\n\n{pre_check_findings}\n</details>\n"

        if cross_file_text:
            final_comment += f"\n<details><summary>🔗 **Cross-File Analysis**</summary>\n\n{cross_file_text}\n</details>\n"

        if refuted_findings:
            final_comment += (
                f"\n<details><summary>🔎 **{len(refuted_findings)} finding(s) filtered by verification**</summary>\n\n"
            )
            final_comment += "Flagged by a reviewer but dropped after an independent verification pass refuted them:\n\n"
            for f, v in refuted_findings:
                loc = f" (`{f.location}`)" if f.location else ""
                final_comment += f"- ~~{f.description}~~{loc}\n  - Verifier: {v.reasoning}\n"
            final_comment += "\n</details>\n"

        if filtered_findings:
            final_comment += (
                "\n<details><summary>🧹 **Deterministic finding hygiene**</summary>\n\n"
                "The workflow removed the following model reports from the issue/inline path "
                "because they were contradicted by deterministic workflow facts or duplicated:\n\n"
            )
            final_comment += "\n".join(f"- {note}" for note in filtered_findings)
            final_comment += "\n\n</details>\n"

        # Group per-file reviews by cluster
        shown_files = set()
        for cluster in clusters:
            cluster_files = [f for f in cluster.files if f in per_file_reviews]
            if not cluster_files:
                continue
            if len(cluster.files) > 1:
                final_comment += f"\n#### Cluster: {cluster.name} ({cluster.priority})\n"
                if cluster.review_question:
                    final_comment += f"*{cluster.review_question}*\n"
            for file_path in cluster_files:
                final_comment += f"\n<details><summary>📄 **Review for `{_safe_md_path(file_path)}`**</summary>\n\n{per_file_reviews[file_path]}\n</details>\n"
                shown_files.add(file_path)

        # Show any files not in clusters (non-Lean files that were reviewed)
        for file_path, review_text in per_file_reviews.items():
            if file_path not in shown_files:
                final_comment += f"\n<details><summary>📄 **Review for `{_safe_md_path(file_path)}`**</summary>\n\n{review_text}\n</details>\n"

        # GitHub PR comments have a ~65536 char limit. Rather than truncate (and
        # lose the tail), split into multiple comment-sized parts: the first goes
        # to stdout as the primary comment, the rest are written for the action to
        # post as follow-ups so nothing is dropped.
        comments = split_into_comments(final_comment, MAX_GITHUB_COMMENT_SIZE)
        print(comments[0])
        if len(comments) > 1:
            try:
                with open('review_comments.json', 'w') as f:
                    json.dump(comments[1:], f)
                logging.info(
                    f"Review exceeded the comment size limit; split into {len(comments)} parts "
                    f"({len(comments) - 1} follow-up comment(s))."
                )
            except Exception as e:
                logging.warning(f"Failed to write overflow comments: {describe_exc(e)}")

        # Generate line-level annotations for GitHub Review API
        annotations = _build_line_annotations(per_file_structured, diff_by_file)
        if annotations:
            try:
                with open('review_annotations.json', 'w') as f:
                    json.dump(annotations, f, indent=2)
                logging.info(f"Wrote {len(annotations)} line annotations to review_annotations.json")
            except Exception as e:
                logging.warning(f"Failed to write annotations: {describe_exc(e)}")
    except BudgetExceededError:
        # Top-level containment (STEP 5a): a budget trip re-raised from an early
        # sequential phase (triage/spec/cross-file) that could not degrade in place.
        # Do NOT crash with no comment — render a degraded comment from whatever
        # completed, then exit 0 (or the loud-exit code, applied at the entrypoint).
        run_health.record_budget_trip()
        logging.error("Review aborted early: per-run budget exhausted; reporting as incomplete.")
        _emit_degraded_review(per_file_reviews, context_records)
    except Exception as e:
        # A hard spend/auth/quota failure re-raised to here. Anything NOT hard keeps
        # today's behaviour: propagate (a genuine bug must not be reported as a clean
        # review). This is the single recording site for a hard failure that aborts the
        # run — the leaf R3 sites only re-raise, so it is counted exactly once here.
        if not is_hard_llm_failure(e):
            raise
        run_health.record_hard_failure()
        logging.error(f"Review aborted: hard LLM failure; reporting as incomplete: {describe_exc(e)}")
        _emit_degraded_review(per_file_reviews, context_records)
    finally:
        logging.info(token_tracker.summary())

    # Health flag for the action's shell step (which emits the ::error:: annotation —
    # review.py's stdout is the PR-comment channel and must not print it itself).
    _write_review_health()

    # Loud-exit (R1): a non-zero process exit ONLY when explicitly opted in AND the
    # run degraded — computed here, outside any finally, so it cannot mask a traceback
    # and runs only after the comment above has been printed. Default OFF.
    loud_exit = os.environ.get(ENV_LOUD_EXIT, "").strip().lower() in ("1", "true", "yes")
    if loud_exit and run_health.degraded:
        logging.warning("LLM_LOUD_EXIT enabled and the run degraded — exiting non-zero.")
        return LOUD_EXIT_CODE
    return 0

if __name__ == "__main__":
    sys.exit(main())
