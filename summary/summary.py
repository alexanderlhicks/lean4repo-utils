import os
import re
import sys
import json
import base64
import hashlib
import bisect
import concurrent.futures
import logging
import subprocess
import threading
from datetime import datetime, timezone
from collections import defaultdict
from github import Github, Auth
from github.PullRequest import PullRequest
from github.Repository import Repository

from pydantic import BaseModel, Field

from leanrepo_common.lean_utils import is_in_comment
from leanrepo_common.llm_provider import (
    ContentPart, LLMProvider, TokenUsage, create_provider,
    RunHealth, BudgetExceededError, is_hard_llm_failure,
    parse_run_budget, describe_exc,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- Pydantic schemas for structured LLM output ---

class _ProseSummary(BaseModel):
    """Wrapper for agents that emit a single block of markdown/prose."""
    summary: str = Field(description="The requested summary or report, as markdown text.")


class _TriageSimple(BaseModel):
    """Triage output for small PRs: one flat list of files worth summarizing."""
    summarize: list[str] = Field(
        default_factory=list,
        description="File paths (exactly as provided in the input list) that SHOULD be summarized.",
    )


class _TriageTiered(BaseModel):
    """Triage output for large PRs: high- and low-priority tiers."""
    high: list[str] = Field(
        default_factory=list,
        description="File paths for detailed summarization (functional / proof-relevant changes).",
    )
    low: list[str] = Field(
        default_factory=list,
        description="File paths for brief mention only (trivial or low-signal changes).",
    )

# --- Constants ---
# Defaults for the diff-size budgets; both are overridable via action inputs
# (INPUT_MAX_FILE_DIFF_CHARS / INPUT_MAX_INSTRUCTIONS_DIFF_CHARS). Rough sizing
# guide: typical Lean averages ~50 chars/line (a mix of short tactic lines and
# longer signatures), and ~4 chars ≈ 1 token, so 1,000 lines ≈ 50,000 chars
# ≈ ~12k tokens.
#
# Per-file diff sent to the summarizer; above this it is truncated at a hunk
# boundary (with a coverage note). 60,000 chars ≈ ~1,200 lines ≈ ~15k tokens.
MAX_FILE_DIFF_CHARS = 60_000
# Whole-PR diff sent to the additional-instructions agent in one call; above
# this the analysis is skipped (a partial result would mislead). Must fit the
# model's context alongside the instructions file and the response. 400,000
# chars ≈ ~8,000 changed lines ≈ ~100k tokens (fits a ~128k-token model). The
# old 1.5M default exceeded most cheap models' context, so those calls failed
# rather than ran. Lower for a smaller-context model.
MAX_INSTRUCTIONS_DIFF_CHARS = 400_000
LARGE_PR_FILE_THRESHOLD = 50  # Files to summarize above which tiered mode activates
LARGE_PR_SYNTHESIS_THRESHOLD = 40  # Per-file summaries above which two-stage synthesis activates
COMMENT_IDENTIFIER = "<!-- lean-pr-summary-{{timestamp}} -->"
CACHE_IDENTIFIER = "<!-- lean-summary-cache: "
MAX_COMMENT_CHARS = 65_536  # GitHub's hard limit on an issue/PR comment body
MAX_LISTED_DECLS = 150  # Cap on individually-listed declarations (grouped by file) before an overflow note

# --- Global Provider and Token Tracker ---
_provider: LLMProvider = None  # Initialized in main()

# --- Per-run spend control + loud-on-failure (C3) ---
# Env-var NAMES the entrypoint reads (module constants so a test can assert
# action.yml wires the exact same names). Operator-config only — sourced from the
# workflow/secrets env, NEVER from the untrusted PR checkout.
ENV_MAX_RUN_TOKENS = "LLM_MAX_RUN_TOKENS"
ENV_MAX_RUN_COST = "LLM_MAX_RUN_COST"
ENV_LOUD_EXIT = "LLM_LOUD_EXIT"
# Process exit code when loud-exit is enabled AND the run degraded — applied only at
# the entrypoint via sys.exit(main()), after the comment is posted. Do NOT mark this
# action as a required check with loud-exit on.
LOUD_EXIT_CODE = 2
# Per-run health tracker; a fresh one is installed at the top of main(). Module-global
# so the ThreadPool worker paths can record into it (thread-safe).
run_health = RunHealth()

# FIXED loud-failure banner (no interpolation — it renders in the PR comment; dynamic
# content there would be an injection channel). summary.py posts a single comment and
# has no gating verdict, so — unlike review.py's re-raise-into-containment — each LLM
# site degrades IN PLACE and records into run_health; the banner below is prepended to
# the posted comment whenever run_health.degraded, so a spend/quota/auth failure stays
# loud (banner + ::error:: to stdout + optional loud-exit) instead of a silent green.
_LOUD_BANNER = (
    "> [!CAUTION]\n"
    "> **This summary did not complete normally.** One or more AI calls failed for a "
    "spend, quota, or authentication reason, or the per-run budget was exhausted. The "
    "summary below is PARTIAL — see the Actions log for details."
)
_LOUD_ANNOTATION = (
    "AI summary degraded: an LLM spend/quota/auth failure or per-run budget exhaustion "
    "left the summary incomplete — results are partial (see the run log)."
)


def _note_failure(exc: BaseException) -> None:
    """Record a hard/budget LLM failure into run_health WITHOUT re-raising. summary.py
    degrades in place and stays loud via run_health.degraded (banner + loud-exit); it
    does not crash the run, so nothing needs a top-level containment catch."""
    if isinstance(exc, BudgetExceededError):
        run_health.record_budget_trip()
    elif is_hard_llm_failure(exc):
        run_health.record_hard_failure()


def _safe_md_path(s: str) -> str:
    """Neutralise markdown-breaking chars in a PR-author-controlled path before it is
    rendered inside a code span in the bot's comment."""
    return s.replace("`", "").replace("\n", " ").replace("\r", " ")


def _summary_skipped_marker() -> str:
    if not run_health.skipped_files:
        return ""
    files = ", ".join(f"`{_safe_md_path(fp)}`" for fp in run_health.skipped_files)
    return f"\n> **Skipped (per-run budget):** {files}\n"


class TokenTracker:
    """Thread-safe cumulative token usage tracker."""
    def __init__(self):
        self._lock = threading.Lock()
        self.total_input = 0
        self.total_output = 0
        self.total_thinking = 0
        self.call_count = 0

    def record(self, usage: TokenUsage):
        with self._lock:
            self.call_count += 1
            self.total_input += usage.input_tokens
            self.total_output += usage.output_tokens
            self.total_thinking += usage.thinking_tokens

    def summary(self) -> str:
        with self._lock:
            total = self.total_input + self.total_output + self.total_thinking
            parts = [f"Token usage: {self.total_input:,} input + {self.total_output:,} output"]
            if self.total_thinking > 0:
                parts.append(f" + {self.total_thinking:,} thinking")
            parts.append(f" = {total:,} total across {self.call_count} API calls")
            return "".join(parts)

token_tracker = TokenTracker()


# --- AI Generation ---

def _reasoning_kwargs(reasoning_effort):
    """Map a reasoning_effort string to create_provider kwargs.

    Returns {"reasoning_default": {"effort": <level>}} for low/medium/high;
    {} for empty or unrecognized values (model default)."""
    effort = (reasoning_effort or "").strip().lower()
    if effort in ("low", "medium", "high"):
        return {"reasoning_default": {"effort": effort}}
    return {}


def _positive_int(value, default):
    """Parse a positive int from an action-input string; fall back to `default`
    on empty, missing, non-numeric, or non-positive input."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _call_llm(prompt, model_name, schema):
    """Calls the LLM provider with retry logic and token tracking, parsing the
    response into the given Pydantic schema.

    Returns the parsed schema instance. Raises on provider failure after retries.
    """
    parts = [ContentPart(type="text", data=prompt)]
    parsed, usage = _provider.generate_structured(
        model=model_name, contents=parts, schema=schema,
    )
    token_tracker.record(usage)
    return parsed


def _call_prose(prompt, model_name):
    """Convenience: run a prose-generating prompt, return the unwrapped string."""
    return _call_llm(prompt, model_name, _ProseSummary).summary

def _read_prompt_template(template_name: str) -> str:
    action_path = os.path.dirname(os.path.realpath(__file__))
    prompt_template_path = os.path.join(action_path, "prompts", template_name)
    try:
        with open(prompt_template_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        sys.exit(f"Error: Prompt template not found at {prompt_template_path}")

def split_diff_into_files(diff_content):
    """Splits a full git diff into a dictionary of per-file diffs."""
    files = {}
    file_diffs = re.split(r'^diff --git ', diff_content, flags=re.MULTILINE)
    for file_diff in file_diffs:
        if not file_diff.strip():
            continue
        # Re-add the split marker
        full_file_diff = "diff --git " + file_diff
        match = re.search(r'^diff --git a/(.+) b/(.+)', full_file_diff, flags=re.MULTILINE)
        if match:
            files[match.group(2)] = full_file_diff
    return files


def _count_diff_lines(file_diff):
    """Count added/removed content lines in a single-file diff.

    Skips the per-file header region (everything before the first `@@` hunk
    header) so a content line whose text starts with '++' or '--' is counted
    correctly and the `+++ b/...` / `--- a/...` headers are not."""
    added = removed = 0
    seen_hunk = False
    for line in file_diff.splitlines():
        if line.startswith("@@"):
            seen_hunk = True
            continue
        if not seen_hunk:
            continue
        if line.startswith('+'):
            added += 1
        elif line.startswith('-'):
            removed += 1
    return added, removed


def _truncate_file_diff(file_diff, max_chars=MAX_FILE_DIFF_CHARS):
    """Truncate a single-file diff, preferring a hunk boundary.

    Returns (text, truncated). When at least one hunk boundary falls within
    the budget we cut there so the model always receives whole hunks.
    Otherwise we fall back to the last newline before the budget — never a
    mid-line cut, which would feed the summarizer a malformed diff line."""
    if len(file_diff) <= max_chars:
        return file_diff, False

    hunk_markers = [m.start() for m in re.finditer(r"\n@@ ", file_diff)]
    candidate_markers = [pos for pos in hunk_markers if 0 < pos < max_chars]
    # Cut at the last in-budget hunk boundary only when ≥2 markers fit, so at
    # least one whole hunk is retained (cutting at a single early marker would
    # discard the entire body and keep only the file header).
    if len(candidate_markers) >= 2:
        return file_diff[:candidate_markers[-1]], True

    # Otherwise fall back to the last newline within budget — a partial final
    # hunk is still valid, readable diff; a mid-line cut is not.
    newline = file_diff.rfind("\n", 0, max_chars)
    if newline > 0:
        return file_diff[:newline + 1], True
    return file_diff[:max_chars], True

def summarize_file_diff(file_path, file_diff, model_name, prompt_template):
    """Generates a summary for a single file's diff (Map step)."""
    prompt = prompt_template.replace("{{FILE_PATH}}", file_path).replace("{{FILE_DIFF}}", file_diff)
    return _call_prose(prompt, model_name)

def synthesize_summary(per_file_summaries, model_name, pr_title, pr_body, pr_type_hint=""):
    """Synthesizes a final summary from per-file summaries (Reduce step)."""
    summaries_text = "\n".join(f"- {s}" for s in per_file_summaries)
    prompt_template = _read_prompt_template("synthesize_summary.md")
    prompt = prompt_template.replace("{{PR_TITLE}}", pr_title) \
                            .replace("{{PR_BODY}}", pr_body) \
                            .replace("{{PER_FILE_SUMMARIES}}", summaries_text) \
                            .replace("{{PR_TYPE_HINT}}", pr_type_hint)
    result = _call_prose(prompt, model_name)
    if not result:
        raise RuntimeError("Failed to synthesize PR summary from per-file summaries.")
    return result

def apply_additional_instructions(diff_content, instructions_content, model_name, prompt_template):
    """Applies deployment-supplied instructions to the diff.

    The instructions file is a project-controlled prompt-extension: it can
    encode a style guide (request a violation listing), a progress tracker
    (request a structured assessment), a doc/wiki cross-check, etc. The
    function is intentionally agnostic about output shape — the instructions
    themselves tell the agent what to produce.
    """
    if not instructions_content:
        return None
    prompt = prompt_template.replace("{{INSTRUCTIONS_CONTENT}}", instructions_content) \
                            .replace("{{DIFF_CONTENT}}", diff_content)
    return _call_prose(prompt, model_name)

_PROOF_RELEVANT_PATTERNS = re.compile(r'\b(sorry|admit|native_decide)\b')
_SORRY_RE = re.compile(r'\bsorry\b')

def _detect_proof_signals(file_diff):
    """Check if a file diff contains proof-relevant keywords in added/removed lines."""
    signals = set()
    for line in file_diff.splitlines():
        if not line.startswith(('+', '-')) or line.startswith(('+++', '---')):
            continue
        if _PROOF_RELEVANT_PATTERNS.search(line):
            signals.update(m.group() for m in _PROOF_RELEVANT_PATTERNS.finditer(line))
    return signals

# Unambiguously non-reviewable files: lockfiles, binaries/media, compiled
# artifacts. These are filtered deterministically (below) so they never reach
# the triage model or the summarizer — robust even when a cheap triage model
# would misjudge them. Anything genuinely ambiguous is left to the LLM.
_NOISE_BASENAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
    "cargo.lock", "uv.lock", "gemfile.lock", "lake-manifest.json", "flake.lock",
}
_NOISE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".woff", ".woff2", ".ttf", ".eot", ".olean", ".min.js", ".min.css",
)


def _is_noise_file(path):
    """True for files that never warrant a summary (lockfiles, binaries,
    compiled artifacts), identified purely by name/extension."""
    base = path.rsplit("/", 1)[-1].lower()
    return base in _NOISE_BASENAMES or base.endswith(_NOISE_EXTENSIONS)


def _build_file_list_str(file_paths, diff_by_file, annotate_signals=False):
    """Build a formatted file list with line counts for triage prompts.
    If annotate_signals is True, appends proof-relevant signal tags."""
    file_list_with_counts = []
    for fp in file_paths:
        diff = diff_by_file[fp]
        added, removed = _count_diff_lines(diff)
        entry = f"{fp} (+{added}/-{removed})"
        if annotate_signals:
            signals = _detect_proof_signals(diff)
            if signals:
                entry += f" [contains: {', '.join(sorted(signals))}]"
        file_list_with_counts.append(entry)
    return "\n".join(file_list_with_counts)

def triage_files(file_paths, diff_by_file, model_name):
    """Uses the AI to filter out noise files before summarization.
    For large PRs, returns (high_priority, low_priority) tuple.
    For normal PRs, returns (files_to_summarize, []) tuple."""
    if not file_paths:
        return [], []

    proof_signal_files = [fp for fp in file_paths if _detect_proof_signals(diff_by_file[fp])]
    proof_set = set(proof_signal_files)
    # Drop unambiguous noise deterministically before involving the LLM (a file
    # with proof signals is never treated as noise). Dropped files fall out of
    # both tiers and are reported as "filtered as noise" by the caller.
    candidates = [fp for fp in file_paths if fp in proof_set or not _is_noise_file(fp)]
    if not candidates:
        return [], []  # nothing worth triaging — skip the LLM call entirely

    use_tiered = len(candidates) > LARGE_PR_FILE_THRESHOLD
    file_list_str = _build_file_list_str(candidates, diff_by_file, annotate_signals=use_tiered)

    if use_tiered:
        prompt_template = _read_prompt_template("triage_tiered.md")
        schema = _TriageTiered
    else:
        prompt_template = _read_prompt_template("triage.md")
        schema = _TriageSimple
    prompt = prompt_template.replace("{{FILE_LIST}}", file_list_str)

    try:
        parsed = _call_llm(prompt, model_name, schema)
    except Exception as exc:
        _note_failure(exc)  # loud via run_health.degraded; degrade in place (no re-raise)
        print(f"Warning: Triage agent failed ({describe_exc(exc)}). Proceeding with all candidate files.")
        return candidates, []

    if use_tiered:
        high_set = set(parsed.high)
        low_set = set(parsed.low)
        for fp in proof_signal_files:
            if fp not in high_set:
                print(f"Promoting {fp} to high priority (contains proof-relevant signals).")
            low_set.discard(fp)
            high_set.add(fp)
        # Preserve original file order from candidates
        high = [f for f in candidates if f in high_set]
        low = [f for f in candidates if f in low_set]
        return high, low

    selected_set = {f for f in parsed.summarize if f in candidates}
    selected_set.update(proof_signal_files)
    selected = [f for f in candidates if f in selected_set]
    return selected, []


def _load_lean_source(path, revision=None):
    if revision:
        result = subprocess.run(
            ["git", "show", f"{revision}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else ""
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""

def synthesize_summary_staged(per_file_summaries, model_name, pr_title, pr_body, pr_type_hint=""):
    """Two-stage synthesis for large PRs: group by directory, synthesize groups, then global."""
    # Group summaries by top-level directory
    groups = defaultdict(list)
    for s in per_file_summaries:
        # Extract file path from "**path/to/file**: summary"
        match = re.match(r'\*\*([^*]+)\*\*:', s)
        if match:
            path = match.group(1)
            parts = path.split('/')
            group_key = parts[0] if len(parts) > 1 else "root"
        else:
            group_key = "other"
        groups[group_key].append(s)

    # Synthesize each directory group
    prompt_template = _read_prompt_template("synthesize_summary.md")
    group_summaries = []
    for group_key, summaries in sorted(groups.items()):
        if len(summaries) <= 3:
            # Small groups don't need their own synthesis
            group_summaries.extend(summaries)
        else:
            group_text = "\n".join(f"- {s}" for s in summaries)
            prompt = prompt_template.replace("{{PR_TITLE}}", pr_title) \
                                    .replace("{{PR_BODY}}", "") \
                                    .replace("{{PER_FILE_SUMMARIES}}", group_text) \
                                    .replace("{{PR_TYPE_HINT}}", f"This is a sub-summary for the `{group_key}/` directory. ")
            try:
                result = _call_prose(prompt, model_name)
            except Exception as exc:
                _note_failure(exc)
                print(f"Warning: Sub-synthesis for {group_key}/ failed ({describe_exc(exc)}). Falling back to raw summaries.")
                result = ""
            if result:
                group_summaries.append(f"**{group_key}/**: {result.strip()}")
            else:
                group_summaries.extend(summaries)

    # Final global synthesis
    return synthesize_summary(group_summaries, model_name, pr_title, pr_body, pr_type_hint)

# --- Diff Analysis ---
class DiffAnalyzer:
    """Parses a git diff to extract statistics and track 'sorry's."""

    def __init__(self, decl_keywords, base_revision=None):
        self.files_changed = set()
        self.lines_added = 0
        self.lines_removed = 0
        self.added_sorries = []
        self.removed_sorries = []
        self.affected_sorries = []
        self.added_decls = []
        self.removed_decls = []
        self.affected_decls = []
        self.warnings = []  # Lean quality signal warnings
        self._decl_keywords = decl_keywords
        self._base_revision = base_revision

        self._current_file = ""
        self._current_old_file = ""
        self._seen_hunk = False  # True once a @@ hunk header is seen for the current file
        self._old_line_num = 0
        self._new_line_num = 0
        self._comment_depth = 0  # Lean nested block comment depth (diff-local fallback)
        self._current_decl_header = ""
        self._current_decl_name = ""
        self._raw_added = {}
        self._raw_removed = {}
        self._raw_added_decls = {}
        self._raw_removed_decls = {}
        self._new_source_cache = {}
        self._old_source_cache = {}
        self._current_new_index = self._empty_index()
        self._current_old_index = self._empty_index()

        self._file_path_regex = re.compile(r'diff --git a/(.+) b/(.+)')
        self._hunk_header_regex = re.compile(r'@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@')
        keywords_regex_part = "|".join(re.escape(k) for k in self._decl_keywords)
        self._decl_line_regex = re.compile(
            r'^(?:@\[[^\]]+\]\s*)*'
            r'(?:(?:local|private|protected|noncomputable|unsafe|partial|scoped)\s+)*'
            r'(?P<keyword>{})\b(?:\s+|$)(?P<rest>.*)$'.format(keywords_regex_part)
        )
        self._name_extract_regex = re.compile(r'^(?P<name>[^\s\(\{{:\[]+)')

    @property
    def stats(self):
        return {
            "files_changed": len(self.files_changed),
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
        }

    def analyze(self, diff):
        """Analyze the diff, populating the result attributes (stats,
        added/removed/affected sorries and declarations, warnings). Returns
        self so callers can read those attributes directly."""
        for line in diff.splitlines():
            if self._parse_file_header(line) or self._parse_hunk_header(line):
                continue

            # Skip the per-file header region (the `--- a/...`, `+++ b/...`,
            # `index`, and mode lines that precede the first hunk). Once a hunk
            # header has been seen, every +/- line is real content — including a
            # line whose *content* happens to start with '++' or '--'.
            if not self._seen_hunk:
                continue

            # Line stats are collected for all files; Lean analysis only for .lean.
            if line.startswith('+'):
                self.lines_added += 1
            elif line.startswith('-'):
                self.lines_removed += 1
            if self._current_file.endswith(".lean"):
                self._process_line(line)

        self._categorize_sorries()
        self._categorize_decls()
        return self

    def _parse_file_header(self, line):
        match = self._file_path_regex.match(line)
        if match:
            self._current_old_file = match.group(1)
            self._current_file = match.group(2)
            self.files_changed.add(self._current_file)
            self._seen_hunk = False
            self._comment_depth = 0
            self._current_decl_header = ""
            self._current_decl_name = ""
            self._current_new_index = self._load_source_index(self._current_file, is_old=False)
            self._current_old_index = self._load_source_index(self._current_old_file, is_old=True)
            return True
        return False

    def _parse_hunk_header(self, line):
        match = self._hunk_header_regex.match(line)
        if match:
            self._seen_hunk = True
            self._old_line_num = int(match.group(1))
            self._new_line_num = int(match.group(3))
            return True
        return False

    @staticmethod
    def _empty_index():
        return {"starts": [], "decls": [], "comment_lines": frozenset(), "available": False}

    def _load_source_index(self, file_path, is_old):
        """Load and cache a full-source index for a file: declaration positions
        plus the set of line numbers that lie entirely within comments.

        The comment-line set is computed over the *whole* source file, so it is
        immune to the cross-hunk depth desync that affects diff-local comment
        tracking (a block comment opened or closed in an unshown region between
        hunks). `available` is False when the source could not be loaded, in
        which case callers fall back to diff-local tracking."""
        cache = self._old_source_cache if is_old else self._new_source_cache
        if file_path in cache:
            return cache[file_path]
        source = _load_lean_source(file_path, self._base_revision if is_old else None)
        decls, comment_lines = self._scan_source(source)
        index = {
            "starts": [d["line"] for d in decls],
            "decls": decls,
            "comment_lines": comment_lines,
            "available": bool(source),
        }
        cache[file_path] = index
        return index

    def _scan_source(self, source):
        """Single pass over a source file: collect declarations and the set of
        line numbers fully inside comments."""
        decls = []
        comment_lines = set()
        comment_depth = 0
        for line_num, line in enumerate(source.splitlines(), start=1):
            in_comment, comment_depth = is_in_comment(line, comment_depth)
            if in_comment:
                comment_lines.add(line_num)
                continue
            stripped = line.lstrip()
            decl_info = self._parse_declaration_line(stripped, line_num)
            if decl_info:
                decls.append(decl_info)
        return decls, comment_lines

    def _parse_declaration_line(self, stripped_content, line_num):
        match = self._decl_line_regex.match(stripped_content)
        if not match:
            return None
        keyword = match.group('keyword')
        rest = match.group('rest').lstrip()
        name_match = self._name_extract_regex.match(rest)
        if name_match:
            name = name_match.group('name')
        elif keyword == "example":
            name = f"example@L{line_num}"
        else:
            return None
        return {
            'name': name,
            'header': stripped_content.split(":=")[0].strip(),
            'line': line_num,
        }

    def _lookup_decl(self, line_num, index):
        starts, decls = index["starts"], index["decls"]
        if not starts:
            return None
        pos = bisect.bisect_right(starts, line_num) - 1
        if pos < 0:
            return None
        return decls[pos]

    def _set_current_decl_from_source(self, line):
        if line.startswith('-'):
            decl = self._lookup_decl(self._old_line_num, self._current_old_index)
        else:
            decl = self._lookup_decl(self._new_line_num, self._current_new_index)
            if not decl and not line.startswith('+'):
                decl = self._lookup_decl(self._old_line_num, self._current_old_index)
        if decl:
            self._current_decl_name = decl['name']
            self._current_decl_header = decl['header']

    # Patterns for Lean quality signals (only checked on added lines)
    _QUALITY_SIGNALS = [
        (re.compile(r'\badmit\b'), "admit", "`admit` bypasses proof checking"),
        (re.compile(r'\bnative_decide\b'), "native_decide", "`native_decide` bypasses the kernel — potential soundness concern"),
        (re.compile(r'^\s*#check\b'), "#check", "`#check` debug command left in code"),
        (re.compile(r'^\s*#eval\b'), "#eval", "`#eval` debug command left in code"),
        (re.compile(r'set_option\s+autoImplicit\s+true'), "autoImplicit", "`set_option autoImplicit true` re-enabled"),
    ]

    def _line_in_comment(self, line):
        """Whether this diff line's source line lies entirely within a comment,
        per the full-source index. Returns None when the relevant source is
        unavailable, signalling the caller to fall back to diff-local tracking.

        Added/context lines are checked against the new source (at the new line
        number); removed lines against the old source (at the old line number)."""
        if line.startswith('-'):
            index, line_num = self._current_old_index, self._old_line_num
        else:
            index, line_num = self._current_new_index, self._new_line_num
        if not index["available"]:
            return None
        return line_num in index["comment_lines"]

    def _process_line(self, line):
        # Strip diff marker to get the actual source line for comment detection
        content_line = line[1:] if line.startswith(('+', '-')) else line
        # Keep the diff-local depth coherent so it remains a usable fallback for
        # files whose source could not be loaded.
        fallback_in_comment, self._comment_depth = is_in_comment(content_line, self._comment_depth)
        source_in_comment = self._line_in_comment(line)
        in_comment = fallback_in_comment if source_in_comment is None else source_in_comment
        self._set_current_decl_from_source(line)

        if not in_comment:
            self._track_sorries_and_decls(line)
            if line.startswith('+'):
                self._check_quality_signals(line)

        if line.startswith('+'):
            self._new_line_num += 1
        elif line.startswith('-'):
            self._old_line_num += 1
        else:
            self._old_line_num += 1
            self._new_line_num += 1

    def _check_quality_signals(self, line):
        content = line[1:]  # Strip the '+' prefix
        # Block comments already filtered by _process_line; check inline -- comments
        comment_match = re.search(r'(?:^|\s)--', content)
        for pattern, name, message in self._QUALITY_SIGNALS:
            match = pattern.search(content)
            if match:
                if comment_match and match.start() > comment_match.start():
                    continue
                self.warnings.append({
                    'signal': name,
                    'message': message,
                    'file': self._current_file,
                    'line': self._new_line_num,
                })

    def _track_sorries_and_decls(self, line):
        # Strip diff markers and leading whitespace
        content = line[1:] if line.startswith(('+', '-', ' ')) else line
        stripped_content = content.lstrip()

        # Track declarations
        decl_line_num = self._new_line_num if line.startswith('+') else self._old_line_num
        decl_info = self._parse_declaration_line(stripped_content, decl_line_num)
        if decl_info:
            self._current_decl_name = decl_info['name']
            self._current_decl_header = decl_info['header']
            stable_id = f"{self._current_decl_name}@{self._current_file}"
            raw_decl = {
                'id': stable_id,
                'file': self._current_file,
                'name': self._current_decl_name,
                'header': self._current_decl_header,
                'line': decl_line_num,
            }
            if line.startswith('+'):
                self._raw_added_decls[stable_id] = raw_decl
            elif line.startswith('-'):
                self._raw_removed_decls[stable_id] = raw_decl

        # Track sorries. Match the `sorry` keyword on a word boundary so
        # identifiers that merely contain the substring (e.g. `sorryAx`,
        # `my_sorry_lemma`) don't register as proof obligations. This keeps
        # detection consistent with _PROOF_RELEVANT_PATTERNS used in triage.
        sorry_match = _SORRY_RE.search(content)
        if sorry_match and self._current_decl_name:
            # Inline comment guard: -- must be at line start or preceded by whitespace
            comment_match = re.search(r'(?:^|\s)--', content)
            if comment_match and sorry_match.start() > comment_match.start():
                return

            stable_id = f"{self._current_decl_name}@{self._current_file}"
            # Unique key for each sorry instance to avoid overwriting
            line_num = self._new_line_num if line.startswith('+') else self._old_line_num
            instance_key = f"{stable_id}#L{line_num}"

            sorry_info = {
                'id': stable_id,
                'file': self._current_file,
                'name': self._current_decl_name,
                'header': self._current_decl_header.split(":=")[0].strip() if self._current_decl_header else "unknown"
            }
            if line.startswith('+'):
                sorry_info['line'] = self._new_line_num
                self._raw_added[instance_key] = sorry_info
            elif line.startswith('-'):
                sorry_info['line'] = self._old_line_num
                self._raw_removed[instance_key] = sorry_info

    def _categorize_decls(self):
        added_ids, removed_ids = set(self._raw_added_decls.keys()), set(self._raw_removed_decls.keys())
        affected_ids = added_ids.intersection(removed_ids)
        for sid in affected_ids:
            self.affected_decls.append({'id': sid, 'file': self._raw_added_decls[sid]['file'], 'context': self._raw_added_decls[sid]['header'], 'old_line': self._raw_removed_decls[sid]['line'], 'new_line': self._raw_added_decls[sid]['line']})
        for sid in added_ids - affected_ids:
            a = self._raw_added_decls[sid]
            self.added_decls.append({'file': a['file'], 'header': a['header']})
        for sid in removed_ids - affected_ids:
            r = self._raw_removed_decls[sid]
            self.removed_decls.append({'file': r['file'], 'header': r['header']})

    def _categorize_sorries(self):
        added_by_id = defaultdict(list)
        removed_by_id = defaultdict(list)

        for info in self._raw_added.values():
            added_by_id[info['id']].append(info)

        for info in self._raw_removed.values():
            removed_by_id[info['id']].append(info)

        all_ids = set(added_by_id.keys()).union(removed_by_id.keys())

        for sid in all_ids:
            adds = added_by_id[sid]
            rems = removed_by_id[sid]

            # Match up to min(len(adds), len(rems)) as "affected"
            match_count = min(len(adds), len(rems))

            for i in range(match_count):
                added_info = adds[i]
                removed_info = rems[i]
                self.affected_sorries.append({
                    'id': added_info['id'],
                    'file': added_info['file'],
                    'context': added_info['header'],
                    'old_line': removed_info['line'],
                    'new_line': added_info['line']
                })

            # Remaining are purely added or removed
            for i in range(match_count, len(adds)):
                info = adds[i]
                self.added_sorries.append({'file': info['file'], 'header': info['header'], 'line': info['line']})

            for i in range(match_count, len(rems)):
                info = rems[i]
                self.removed_sorries.append({'file': info['file'], 'header': info['header'], 'line': info['line']})

# --- Caching ---
def _compute_config_fingerprint(model_name, prompt_template):
    """Hash the model name and prompt template so cache invalidates when either changes."""
    content = f"{model_name}\n{prompt_template}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]

class SummaryCache:
    """Handles caching of file diff summaries. Thread-safe."""
    def __init__(self, pr: PullRequest, config_fingerprint: str):
        self._lock = threading.Lock()
        self._config_fingerprint = config_fingerprint
        self._cache = self._load_from_comment(pr)

    def _load_from_comment(self, pr: PullRequest):
        comment = find_existing_comment(pr)
        if comment:
            if CACHE_IDENTIFIER not in comment.body:
                return {}
            try:
                payload = comment.body.split(CACHE_IDENTIFIER, 1)[1].split("-->", 1)[0].strip()
            except IndexError:
                return {}
            data = self._decode_cache(payload)
            if data is None:
                return {}
            # Invalidate entire cache if config fingerprint changed
            if data.get("_config") != self._config_fingerprint:
                print("Cache invalidated: model or prompt template changed.")
                return {}
            return data
        return {}

    @staticmethod
    def _decode_cache(payload):
        """Decode an embedded cache payload, or return None if unparseable.

        The payload is base64-encoded JSON: base64's standard alphabet cannot
        contain '-', so the '-->' HTML-comment terminator can never appear
        inside the payload and summaries are stored losslessly."""
        try:
            raw = base64.b64decode(payload, validate=True).decode("utf-8")
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None

    def get(self, file_path, file_diff_hash):
        with self._lock:
            if file_path in self._cache and isinstance(self._cache[file_path], dict) \
                    and self._cache[file_path].get('hash') == file_diff_hash:
                return self._cache[file_path]['summary']
            return None

    def update(self, file_path, file_diff_hash, summary):
        with self._lock:
            self._cache[file_path] = {'hash': file_diff_hash, 'summary': summary}

    def prune(self, valid_paths):
        """Drop cached entries for files not present in the current diff.

        The cache is reloaded from the PR comment each run; without pruning it
        accumulates entries for renamed/removed files indefinitely, bloating the
        comment (a single long-lived branch can push it past GitHub's size
        limit). `_config` is not a file entry and is re-added by to_json()."""
        valid = set(valid_paths)
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if k in valid}

    def to_json(self):
        """Return the embeddable cache payload (base64-encoded JSON).

        base64 is used so a summary containing '-->' can't truncate the
        HTML comment the payload lives in. See _decode_cache."""
        with self._lock:
            data = dict(self._cache)
            data["_config"] = self._config_fingerprint
            raw = json.dumps(data)
            return base64.b64encode(raw.encode("utf-8")).decode("ascii")

# --- Comment Formatting ---

def _format_stats_section(stats):
    return (
        "\n---\n\n**Statistics**\n\n"
        "| Metric | Count |\n"
        "| --- | --- |\n"
        f"| 📝 **Files Changed** | {stats['files_changed']} |\n"
        f"| ✅ **Lines Added** | {stats['lines_added']} |\n"
        f"| ❌ **Lines Removed** | {stats['lines_removed']} |\n"
    )

def _format_grouped_entries(entries, line_fmt, cap=MAX_LISTED_DECLS):
    """Render entries grouped by file for legibility and determinism.

    Files are sorted alphabetically and entries within each file are sorted by
    their rendered text, so the output is stable across runs (the underlying
    data comes from set iteration, which is not). Listing stops after `cap`
    entries with an overflow note, so a PR with hundreds of declarations does
    not produce an unreadable (or comment-overflowing) wall of text.

    `line_fmt(entry)` returns the per-entry markdown (without the leading bullet)."""
    total = len(entries)
    by_file = defaultdict(list)
    for e in entries:
        by_file[e['file']].append(e)
    out = []
    shown = 0
    for f in sorted(by_file):
        group_lines = sorted(line_fmt(e) for e in by_file[f])
        # Blank line after the file sub-header so the bullet list renders as a
        # list under strict CommonMark, not a paragraph continuation.
        out.append(f"\n`{f}` ({len(group_lines)})\n\n")
        for gl in group_lines:
            if shown >= cap:
                out.append(f"\n*…and {total - shown} more not listed.*\n")
                return "".join(out)
            out.append(f"*   {gl}\n")
            shown += 1
    return "".join(out)


def _format_decls_section(added, removed, affected):
    res = "\n---\n\n**Lean Declarations**\n\n"
    decl_line = lambda e: f"`{e['header']}`"  # noqa: E731
    if removed:
        res += f"<details><summary>✏️ <b>Removed:</b> {len(removed)} declaration(s)</summary>\n" + _format_grouped_entries(removed, decl_line) + "</details>\n"
    if added:
        res += f"<details><summary>✏️ <b>Added:</b> {len(added)} declaration(s)</summary>\n" + _format_grouped_entries(added, decl_line) + "</details>\n"
    if affected:
        res += f"<details><summary>✏️ <b>Affected:</b> {len(affected)} declaration(s) (line number changed)</summary>\n\n"
        for s in sorted(affected, key=lambda s: (s['file'], s['context'], s['new_line'])):
            res += f"*   `{s['context']}` in `{s['file']}` moved from L{s['old_line']} to L{s['new_line']}\n"
        res += "</details>\n"
    if not any([added, removed, affected]):
        res += "*   No declarations were added, removed, or affected.\n"
    return res

def _name_mentioned(name, text):
    """True if `name` appears in `text` as a standalone token rather than as a
    substring of a larger identifier. Prevents a short declaration name like
    `h` from matching inside `hash`. Lean identifier characters (word chars,
    `.`, `'`) on either side disqualify the match."""
    return re.search(r"(?<![\w'.])" + re.escape(name) + r"(?![\w'.])", text) is not None


def _find_related_issue(sorry_info, issues):
    """Find an issue related to a sorry entry by tracker ID, file path, or declaration name."""
    sid = sorry_info['id']
    file_path = sorry_info['file']
    # Extract the declaration name from the id (format: "name@file")
    decl_name = sid.split('@')[0] if '@' in sid else ""

    for issue in issues:
        if not issue.body:
            continue
        # Exact tracker ID match (strongest signal)
        if f"<!-- sorry-tracker-id: {sid} -->" in issue.body:
            return issue
        # Match on both file path and declaration name (good signal). The name
        # must appear as a standalone token, not as a substring of a longer
        # identifier.
        if decl_name and _name_mentioned(decl_name, issue.body) and file_path in issue.body:
            return issue
    return None

def _format_sorry_section(added, removed, affected, issues):
    res = "\n---\n\n**`sorry` Tracking**\n\n"
    sorry_line = lambda e: f"`{e['header']}` (L{e['line']})"  # noqa: E731
    if removed:
        res += f"<details><summary>✅ <b>Removed:</b> {len(removed)} `sorry`(s)</summary>\n" + _format_grouped_entries(removed, sorry_line) + "</details>\n"
    if added:
        res += f"<details><summary>❌ <b>Added:</b> {len(added)} `sorry`(s)</summary>\n" + _format_grouped_entries(added, sorry_line) + "</details>\n"
    if affected:
        res += f"<details><summary>✏️ <b>Affected:</b> {len(affected)} `sorry`(s) (line number changed)</summary>\n\n"
        for s in sorted(affected, key=lambda s: (s['file'], s['context'], s['new_line'])):
            related_issue = _find_related_issue(s, issues)
            issue_link = f" (Issue #{related_issue.number})" if related_issue else ""
            res += f"*   `{s['context']}` in `{s['file']}` moved from L{s['old_line']} to L{s['new_line']}{issue_link}\n"
        res += "</details>\n"
    if not any([added, removed, affected]):
        res += "*   No `sorry`s were added, removed, or affected.\n"
    return res

def _format_warnings_section(warnings):
    """Format Lean quality signal warnings."""
    if not warnings:
        return ""
    res = "\n---\n\n**Lean Quality Signals**\n\n"
    # Group by signal type
    by_signal = defaultdict(list)
    for w in warnings:
        by_signal[w['signal']].append(w)
    for signal, items in by_signal.items():
        message = items[0]['message']
        if len(items) == 1:
            w = items[0]
            res += f"*   ⚠️ {message} in `{w['file']}` (L{w['line']})\n"
        else:
            locations = ", ".join(f"`{w['file']}` L{w['line']}" for w in items)
            res += f"*   ⚠️ {message} — {len(items)} occurrence(s): {locations}\n"
    return res

def _format_sorry_delta(added, removed):
    """Format a top-level sorry delta status line."""
    n_added = len(added)
    n_removed = len(removed)
    delta = n_added - n_removed
    if n_added == 0 and n_removed == 0:
        return ""
    parts = []
    if n_removed:
        parts.append(f"{n_removed} removed")
    if n_added:
        parts.append(f"{n_added} added")
    detail = ", ".join(parts)
    if delta < 0:
        return f"> **`sorry` delta: {delta}** ({detail}) — net proof progress\n\n"
    elif delta > 0:
        return f"> **`sorry` delta: +{delta}** ({detail}) — proof obligations increased\n\n"
    else:
        return f"> **`sorry` delta: 0** ({detail}) — no net change\n\n"


def _format_coverage_section(partially_analyzed_files, instructions_skipped):
    if not partially_analyzed_files and not instructions_skipped:
        return ""

    res = "\n---\n\n**Coverage Notes**\n\n"
    if partially_analyzed_files:
        res += (
            f"*   AI file summarization partially analyzed {len(partially_analyzed_files)} file(s) because "
            f"their individual diffs exceeded the per-file size budget. Statistics and Lean signal tracking still cover the full PR.\n"
        )
        res += "<details><summary>Partially Analyzed Files</summary>\n\n"
        for item in partially_analyzed_files:
            res += f"*   `{item['file']}` (+{item['added']}/-{item['removed']})\n"
        res += "</details>\n"
    if instructions_skipped:
        res += "*   Additional-instructions analysis was skipped because the full diff exceeded the analysis size budget, and partial results would be misleading.\n"
    return res


def format_summary(ai_summary, stats, added, removed, affected, added_decls, removed_decls, affected_decls, issues, display_summaries, instructions_report, cache, warnings=None, title_note="", upstream_note="", partially_analyzed_files=None, instructions_skipped=False):
    """Formats the final summary comment in Markdown.

    GitHub rejects comment bodies over MAX_COMMENT_CHARS. To stay under the
    limit we shed content in increasing order of importance: first the embedded
    cache (regenerable — its loss just causes a full re-summarize next run),
    then the per-file summaries, then the additional-analysis section, and only
    as a last resort hard-truncate. The core summary, statistics, and `sorry`
    tracking are always preserved."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    comment_id = COMMENT_IDENTIFIER.replace("{{timestamp}}", timestamp)
    cache_html = f"{CACHE_IDENTIFIER}{cache.to_json()}-->\n\n" if cache else ""

    header = f"### 🤖 PR Summary\n\n{comment_id}\n\n"

    sorry_delta = _format_sorry_delta(added, removed)
    core = f"{title_note}{upstream_note}{sorry_delta}{ai_summary}\n"
    core += _format_stats_section(stats)
    core += _format_decls_section(added_decls, removed_decls, affected_decls)
    core += _format_sorry_section(added, removed, affected, issues)
    if warnings:
        core += _format_warnings_section(warnings)
    core += _format_coverage_section(partially_analyzed_files or [], instructions_skipped)

    instructions_section = ""
    if instructions_report:
        instructions_section = f"\n---\n\n<details><summary>📋 **Additional Analysis**</summary>\n\n{instructions_report}\n</details>\n"

    per_file_section = ""
    if display_summaries:
        per_file_section = "\n---\n\n<details><summary>📄 **Per-File Summaries**</summary>\n\n" + "".join(f"*   {s}\n" for s in display_summaries) + "</details>\n"

    footer = f"\n---\n\n*Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.*"

    def assemble(include_cache, include_per_file, include_instructions, note=""):
        parts = [header]
        if include_cache:
            parts.append(cache_html)
        parts.append(note)
        parts.append(core)
        if include_instructions:
            parts.append(instructions_section)
        if include_per_file:
            parts.append(per_file_section)
        parts.append(footer)
        return "".join(parts)

    candidate = assemble(True, True, True)
    if len(candidate) <= MAX_COMMENT_CHARS:
        return candidate

    omit_note = "> ℹ️ Some sections were omitted to fit GitHub's comment size limit.\n\n"
    # Shed in order: embedded cache, then per-file summaries, then additional analysis.
    for include_per_file, include_instructions in ((True, True), (False, True), (False, False)):
        note = omit_note if not (include_per_file and include_instructions) else ""
        candidate = assemble(False, include_per_file, include_instructions, note)
        if len(candidate) <= MAX_COMMENT_CHARS:
            return candidate

    # Last resort: hard-truncate the (cache-free, sections-dropped) body.
    trunc_note = "\n\n> ⚠️ Summary truncated to fit GitHub's comment size limit."
    return candidate[:MAX_COMMENT_CHARS - len(trunc_note)].rstrip() + trunc_note

def find_sorry_issues(repo: Repository):
    """Finds all open issues with the 'proof wanted' label."""
    try:
        return list(repo.get_issues(state="open", labels=["proof wanted"]))
    except Exception as e:
        print(f"Warning: Could not fetch issues. {describe_exc(e)}")
        return []

# --- PR Title Validation ---
_CONVENTIONAL_COMMIT_RE = re.compile(
    r'^(?P<type>feat|fix|doc|docs|style|refactor|chore|ci|test|perf|build|revert)'
    r'(?:\((?P<scope>[^)]+)\))?:\s+(?P<subject>.+)$'
)

def validate_pr_title(title):
    """Validate PR title against conventional commit format.
    Returns (is_valid, parsed_type, message)."""
    if not title:
        return True, None, None  # No title to validate
    match = _CONVENTIONAL_COMMIT_RE.match(title)
    if match:
        return True, match.group('type'), None
    return False, None, f"PR title does not follow conventional commit format `type[(scope)]: subject`. Got: `{title}`"

# --- GitHub Interaction ---
def get_github_objects(token, repo_name, pr_number):
    """Initializes and returns the GitHub repo and PR objects."""
    auth = Auth.Token(token)
    g = Github(auth=auth)
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    return repo, pr

def find_existing_comment(pr: PullRequest):
    """Finds a comment previously posted by this action."""
    comment_regex = re.compile(COMMENT_IDENTIFIER.replace("{{timestamp}}", ".*?"))
    return next((c for c in pr.get_issue_comments() if comment_regex.search(c.body)), None)

def post_github_comment(pr: PullRequest, summary: str):
    """Finds and updates an existing comment or creates a new one."""
    existing_comment = find_existing_comment(pr)
    if existing_comment:
        existing_comment.edit(summary)
        print("Updated existing comment.")
    else:
        pr.create_issue_comment(summary)
        print("Created a new comment.")

# --- Main Execution ---
def main():
    """Main execution block."""
    global _provider

    # Initialize LLM provider (OpenRouter-backed). API_KEY holds the OpenRouter key.
    api_key = os.getenv("API_KEY")
    if not api_key:
        sys.exit("Error: API_KEY not set (expects an OpenRouter API key).")

    # Optional global reasoning effort, applied to every API call. Default off
    # (empty) preserves the model's own default. OpenRouter accepts low/medium/high.
    reasoning_effort = os.environ.get("INPUT_REASONING_EFFORT", "")
    provider_kwargs = _reasoning_kwargs(reasoning_effort)
    if reasoning_effort.strip() and not provider_kwargs:
        print(f"Warning: ignoring unrecognized reasoning_effort '{reasoning_effort.strip()}' (expected low/medium/high).")

    # Per-run spend ceiling (C3). Operator-config from the workflow/secrets env only.
    # Empty/whitespace == unset == disabled; a non-empty invalid or cost-only value
    # fails fast here, before any LLM call.
    try:
        budget = parse_run_budget(os.environ.get(ENV_MAX_RUN_TOKENS), os.environ.get(ENV_MAX_RUN_COST))
    except ValueError as e:
        sys.exit(f"Error: invalid per-run budget configuration ({ENV_MAX_RUN_TOKENS}/{ENV_MAX_RUN_COST}): {e}")
    global run_health
    run_health = RunHealth()

    _provider = create_provider(api_key, budget=budget, **provider_kwargs)
    logging.info(f"Using LLM provider: {_provider.name}")
    if budget is not None:
        logging.info(f"Per-run budget active: max_tokens={budget.max_tokens} max_cost={budget.max_cost}")
    if provider_kwargs:
        logging.info(f"Reasoning effort: {reasoning_effort.strip().lower()}")

    # Model is an OpenRouter slug, e.g. "anthropic/claude-opus-4.8".
    model_name = os.environ.get("INPUT_MODEL", 'anthropic/claude-haiku-4.5')
    keywords = [k.strip() for k in os.environ.get("INPUT_LEAN_KEYWORDS", 'def,abbrev,example,theorem,opaque,lemma,instance,constant,axiom').split(',')]
    instructions_path = os.environ.get("INPUT_ADDITIONAL_INSTRUCTIONS_PATH")
    validate_title = os.environ.get("INPUT_VALIDATE_TITLE", "false").lower() == "true"
    upstream_path = os.environ.get("INPUT_UPSTREAM_PATH", "")
    max_file_diff_chars = _positive_int(os.environ.get("INPUT_MAX_FILE_DIFF_CHARS"), MAX_FILE_DIFF_CHARS)
    max_instructions_diff_chars = _positive_int(os.environ.get("INPUT_MAX_INSTRUCTIONS_DIFF_CHARS"), MAX_INSTRUCTIONS_DIFF_CHARS)

    try:
        with open("pr.diff", "r") as f:
            diff = f.read()
    except FileNotFoundError:
        sys.exit("Error: pr.diff not found.")

    analyzer = DiffAnalyzer(keywords, base_revision=os.environ.get("MERGE_BASE"))
    analyzer.analyze(diff)

    repo, pr, issues, pr_title, pr_body = None, None, [], "", ""
    if "GITHUB_TOKEN" in os.environ:
        try:
            repo, pr = get_github_objects(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"], int(os.environ["PR_NUMBER"]))
            issues, pr_title, pr_body = find_sorry_issues(repo), pr.title, pr.body or ""
        except Exception as e:
            sys.exit(f"Error: Could not initialize GitHub API — cannot post PR comment: {e}")

    # Title validation and upstream path detection
    title_note = ""
    pr_type_hint = ""
    if validate_title and pr_title:
        is_valid, parsed_type, message = validate_pr_title(pr_title)
        if not is_valid:
            title_note = f"> ⚠️ {message}\n\n"
        elif parsed_type:
            pr_type_hint = f"The PR type is `{parsed_type}`. "

    upstream_note = ""
    if upstream_path:
        upstream_files = [f for f in analyzer.files_changed if f.startswith(upstream_path)]
        if upstream_files:
            upstream_note = f"> ℹ️ This PR modifies {len(upstream_files)} file(s) under `{upstream_path}` — consider whether a corresponding upstream PR is needed.\n\n"

    instructions_content = ""
    if instructions_path:
        try:
            with open(instructions_path, "r") as f:
                instructions_content = f.read()
        except FileNotFoundError:
            print(f"Warning: Additional instructions file not found at {instructions_path}")

    diff_by_file = split_diff_into_files(diff)
    all_files = list(diff_by_file.keys())
    high_priority, low_priority = triage_files(all_files, diff_by_file, model_name) if all_files else ([], [])
    files_to_summarize = high_priority
    if low_priority:
        print(f"Triage agent selected {len(high_priority)} high-priority, {len(low_priority)} low-priority, skipped {len(all_files) - len(high_priority) - len(low_priority)} files.")
    else:
        print(f"Triage agent selected {len(files_to_summarize)}/{len(all_files)} files to summarize.")

    synthesis_inputs = []
    display_summaries = []
    instructions_report = None
    partially_analyzed_files = []
    instructions_skipped = False

    summarize_template = _read_prompt_template("summarize_file.md")
    config_fp = _compute_config_fingerprint(model_name, summarize_template)
    cache = SummaryCache(pr, config_fp) if pr else None
    instructions_template = _read_prompt_template("additional_instructions.md") if instructions_content else ""

    # Collect summaries keyed by file path so we can assemble in deterministic order
    summary_by_file = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        instructions_future = None
        if instructions_content:
            if len(diff) <= max_instructions_diff_chars:
                instructions_future = executor.submit(apply_additional_instructions, diff, instructions_content, model_name, instructions_template)
            else:
                instructions_skipped = True

        future_to_file = {}
        for fp in files_to_summarize:
            fd, was_truncated = _truncate_file_diff(diff_by_file[fp], max_file_diff_chars)
            file_diff_hash = hashlib.sha256(fd.encode()).hexdigest()
            cached_summary = cache.get(fp, file_diff_hash) if cache else None
            if was_truncated:
                added_count, removed_count = _count_diff_lines(diff_by_file[fp])
                partially_analyzed_files.append({'file': fp, 'added': added_count, 'removed': removed_count})

            if cached_summary:
                print(f"Cache hit for {fp}")
                summary_by_file[fp] = cached_summary
            else:
                print(f"Cache miss for {fp}. Queuing summarization.")
                future = executor.submit(summarize_file_diff, fp, fd, model_name, summarize_template)
                future_to_file[future] = (fp, file_diff_hash)

        for future in concurrent.futures.as_completed(future_to_file):
            fp, file_diff_hash = future_to_file[future]
            try:
                res = future.result()
                if res:
                    summary = res.strip()
                    if cache:
                        cache.update(fp, file_diff_hash, summary)
                    summary_by_file[fp] = summary
                    run_health.record_fresh_success()  # a genuinely fresh (non-cache) generation succeeded (R5)
                else:
                    print(f"Warning: Summarization for {fp} returned no result.")
                    summary_by_file[fp] = "*Summary unavailable — AI generation failed after retries.*"
            except BudgetExceededError:
                # Idempotent skip: once the budget trips, already-queued workers each
                # raise at fresh entry — one budget event, recorded per file, not N
                # failures. The marker + prepended banner carry the signal.
                run_health.record_budget_trip(fp)
                summary_by_file[fp] = "*Summary unavailable — per-run budget exhausted.*"
            except Exception as exc:
                if is_hard_llm_failure(exc):
                    run_health.record_hard_failure()
                # R6: this string is rendered into the comment AND fed back into the
                # synthesis prompt — keep the exception body out of both; log the detail.
                print(f"Warning: Summarization for {fp} generated an exception: {describe_exc(exc)}")
                summary_by_file[fp] = "*Summary unavailable — AI generation failed.*"

        if instructions_future:
            try:
                instructions_report = instructions_future.result()
            except Exception as exc:
                _note_failure(exc)
                print(f"Warning: Additional-instructions analysis generated an exception: {describe_exc(exc)}")

    # Assemble per-file summaries in original file order for deterministic output
    for fp in files_to_summarize:
        if fp in summary_by_file:
            entry = f"**{fp}**: {summary_by_file[fp]}"
            synthesis_inputs.append(entry)
            display_summaries.append(entry)

    # Add low-priority files as brief mentions (no AI call)
    for fp in low_priority:
        added_count, removed_count = _count_diff_lines(diff_by_file[fp])
        entry = f"**{fp}**: *(minor changes, +{added_count}/-{removed_count})*"
        synthesis_inputs.append(entry)
        display_summaries.append(entry)

    # Never silently drop files: list anything triaged out entirely, so the
    # file count reconciles and nothing is invisible to a reviewer. Kept out of
    # synthesis_inputs so the overview isn't padded with noise.
    summarized = set(files_to_summarize) | set(low_priority)
    dropped = [fp for fp in all_files if fp not in summarized]
    if dropped:
        preview = ", ".join(f"`{fp}`" for fp in dropped[:12])
        if len(dropped) > 12:
            preview += f", …(+{len(dropped) - 12} more)"
        display_summaries.append(
            f"*{len(dropped)} file(s) filtered as noise (lockfiles, generated, or trivial): {preview}*"
        )

    try:
        # Use two-stage synthesis for very large PRs
        if len(synthesis_inputs) > LARGE_PR_SYNTHESIS_THRESHOLD:
            print(f"Large PR detected ({len(synthesis_inputs)} summaries). Using two-stage synthesis.")
            ai_summary = synthesize_summary_staged(synthesis_inputs, model_name, pr_title, pr_body, pr_type_hint)
        else:
            ai_summary = synthesize_summary(synthesis_inputs, model_name, pr_title, pr_body, pr_type_hint)
    except Exception as e:
        _note_failure(e)
        print(f"Error synthesizing final summary: {describe_exc(e)}")
        # Fallback if synthesis fails
        ai_summary = "Failed to generate AI summary. Please check the per-file summaries and statistics below."

    if cache:
        cache.prune(all_files)

    final_summary = format_summary(
        ai_summary,
        analyzer.stats,
        analyzer.added_sorries,
        analyzer.removed_sorries,
        analyzer.affected_sorries,
        analyzer.added_decls,
        analyzer.removed_decls,
        analyzer.affected_decls,
        issues,
        display_summaries,
        instructions_report,
        cache,
        analyzer.warnings,
        title_note,
        upstream_note,
        partially_analyzed_files,
        instructions_skipped,
    )

    # Loud-on-failure: prepend the fixed banner + budget skip-marker when the run
    # degraded, and emit a GitHub ::error:: annotation. summary.py's stdout is NOT the
    # comment channel (it posts via the API), so printing the workflow command here is
    # safe and lands in the checks UI.
    if run_health.degraded:
        final_summary = _LOUD_BANNER + "\n" + _summary_skipped_marker() + "\n\n" + final_summary
        print(f"::error::{_LOUD_ANNOTATION}")

    if pr:
        post_github_comment(pr, final_summary)
    elif "GITHUB_TOKEN" in os.environ:
        sys.exit("Error: GitHub PR object is unavailable — cannot post comment.")
    else:
        print("No GITHUB_TOKEN set. Printing summary to stdout:\n", final_summary)

    logging.info(token_tracker.summary())

    # Loud-exit (R1): non-zero process exit ONLY when explicitly opted in AND the run
    # degraded. Computed after the comment is posted; applied at the entrypoint, never
    # in a finally. Default OFF.
    loud_exit = os.environ.get(ENV_LOUD_EXIT, "").strip().lower() in ("1", "true", "yes")
    if loud_exit and run_health.degraded:
        logging.warning("LLM_LOUD_EXIT enabled and the run degraded — exiting non-zero.")
        return LOUD_EXIT_CODE
    return 0

if __name__ == "__main__":
    sys.exit(main())
