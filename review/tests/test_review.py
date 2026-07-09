"""Unit tests for review.py core functions."""

import pytest
import sys
import os
import json
import contextlib
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review
from review import (
    split_diff_into_files,
    _extract_added_lines,
    _fetch_url_content,
    _is_in_string,
    _normalize_external_url,
    run_mechanical_prechecks,
    scan_escape_hatches,
    introduced_hatches_triggering_verdict,
    _get_diff_lines,
    _load_prompt,
    _fit_replacements_to_budget,
    _format_repo_files,
    _validate_url,
    _check_ip_safe,
    _resolve_and_validate,
    _build_line_annotations,
    _pin_address,
    _pinned_dns,
    _chunk_file_by_declarations,
    _parse_diff_hunks,
    _diff_header,
    _diff_for_range,
    _merge_file_reviews,
    _finding_lines,
    get_local_reference_parts,
    extract_refs_from_instructions,
    _merge_csv,
)
from leanrepo_common.lean_utils import is_in_comment
# --- split_diff_into_files ---

class TestSplitDiffIntoFiles:
    def test_basic_split(self):
        diff = """diff --git a/Foo.lean b/Foo.lean
--- a/Foo.lean
+++ b/Foo.lean
@@ -1,3 +1,4 @@
 import Bar
+import Baz
 def foo := 1
diff --git a/Bar.lean b/Bar.lean
--- a/Bar.lean
+++ b/Bar.lean
@@ -1,2 +1,2 @@
-def bar := 1
+def bar := 2
"""
        result = split_diff_into_files(diff)
        assert "Foo.lean" in result
        assert "Bar.lean" in result
        assert len(result) == 2

    def test_empty_diff(self):
        assert split_diff_into_files("") == {}

    def test_rename(self):
        diff = """diff --git a/Old.lean b/New.lean
similarity index 90%
rename from Old.lean
rename to New.lean
--- a/Old.lean
+++ b/New.lean
@@ -1 +1 @@
-old content
+new content
"""
        result = split_diff_into_files(diff)
        assert "New.lean" in result
        assert "Old.lean" not in result

    def test_non_lean_files_included(self):
        diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""
        result = split_diff_into_files(diff)
        assert "README.md" in result


# --- _extract_added_lines ---

class TestExtractAddedLines:
    def test_basic(self):
        diff = """+++ b/Foo.lean
@@ -1,3 +1,4 @@
 import Bar
+import Baz
+import Qux
 def foo := 1
"""
        added = _extract_added_lines(diff)
        assert "import Baz" in added
        assert "import Qux" in added
        assert "import Bar" not in added

    def test_ignores_diff_header(self):
        diff = """+++ b/Foo.lean
@@ -1 +1 @@
+new line
"""
        added = _extract_added_lines(diff)
        assert added == ["new line"]
        # +++ line should not appear
        assert "++ b/Foo.lean" not in added


# --- is_in_comment (now from lean_utils) ---

class TestIsInComment:
    """Tests for lean_utils.is_in_comment with nested block comment support."""

    def test_single_line_comment(self):
        is_comment, depth = is_in_comment("  -- this is a comment", 0)
        assert is_comment is True
        assert depth == 0

    def test_not_comment(self):
        is_comment, depth = is_in_comment("def foo := 1", 0)
        assert is_comment is False
        assert depth == 0

    def test_block_comment_start(self):
        is_comment, depth = is_in_comment("/- start of block", 0)
        assert is_comment is True
        assert depth == 1

    def test_inside_block_comment(self):
        is_comment, depth = is_in_comment("  still in block", 1)
        assert is_comment is True
        assert depth == 1

    def test_block_comment_end(self):
        is_comment, depth = is_in_comment("  end of block -/", 1)
        assert is_comment is True
        assert depth == 0

    def test_single_line_block_comment(self):
        is_comment, depth = is_in_comment("/- single line -/", 0)
        assert is_comment is True
        assert depth == 0

    def test_nested_comment_preserves_outer(self):
        """Closing inner /- -/ should NOT close the outer block."""
        # depth=2 means we're inside /- /- ... here
        is_comment, depth = is_in_comment("  inner close -/", 2)
        assert is_comment is True
        assert depth == 1  # still inside the outer comment


# --- _is_in_string ---

class TestIsInString:
    def test_keyword_in_string(self):
        assert _is_in_string("sorry", 'let msg := "sorry about that"') is True

    def test_keyword_outside_string(self):
        assert _is_in_string("sorry", "  sorry") is False

    def test_keyword_both(self):
        # "sorry" appears both in a string and outside — should return False
        assert _is_in_string("sorry", 'let x := "sorry"; sorry') is False

    def test_no_strings(self):
        assert _is_in_string("axiom", "axiom myAxiom : True") is False


# --- run_mechanical_prechecks ---

class TestMechanicalPrechecks:
    # Diffs use realistic `@@` hunk headers (as `gh pr diff` emits): the scan
    # classifies hatches by matching full-file line numbers against the diff's
    # added lines.
    def test_no_findings(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("def foo := 1\n")
        diff = "@@ -0,0 +1 @@\n+def foo := 1\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "No escape hatches" in result

    def test_sorry_in_diff(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        diff = "@@ -0,0 +1 @@\n+theorem foo : True := sorry\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "sorry" in result
        assert "introduced" in result.lower()

    def test_sorry_in_comment_ignored(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("-- sorry this is a comment\ndef foo := 1\n")
        diff = "@@ -0,0 +1,2 @@\n+-- sorry this is a comment\n+def foo := 1\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        # sorry in a line comment should not be flagged as introduced
        assert "**`sorry`** introduced" not in result

    def test_hatch_in_block_comment_not_flagged(self, tmp_path):
        """Regression: a keyword inside a `/- -/` block comment whose opener is
        an unchanged *context* line must NOT be treated as introduced live code
        (that would spuriously force a Changes Requested verdict)."""
        lean_file = tmp_path / "A.lean"
        lean_file.write_text(
            "def real := 1\n/- big comment\nstill comment mentioning sorry\n-/\ndef after := 2\n"
        )
        # The `/-` opener is a context line; only the comment body is added.
        diff = "@@ -2,2 +2,3 @@\n /- big comment\n+still comment mentioning sorry\n -/\n"
        scan = scan_escape_hatches({str(lean_file): diff})
        assert scan["introduced"] == []
        assert introduced_hatches_triggering_verdict(scan) == []

    def test_non_lean_file_skipped(self, tmp_path):
        md_file = tmp_path / "README.md"
        md_file.write_text("sorry\n")
        result = run_mechanical_prechecks({str(md_file): "@@ -0,0 +1 @@\n+sorry\n"})
        assert "No escape hatches" in result

    def test_large_file_warning(self, tmp_path):
        lean_file = tmp_path / "Big.lean"
        lean_file.write_text("def x := 1\n" * 2000)
        diff = "@@ -0,0 +1 @@\n+def x := 1\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "Large file" in result

    def test_introduced_hatch_not_also_listed_preexisting(self, tmp_path):
        """A sorry added in the diff must not appear again under pre-existing."""
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        diff = "@@ -0,0 +1 @@\n+theorem foo : True := sorry\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "introduced" in result
        assert "Pre-existing escape hatches" not in result

    def test_preexisting_reported_when_not_in_diff(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem old : True := sorry\ndef added := 1\n")
        # Only line 2 is added; the sorry on line 1 is an unchanged context line.
        diff = "@@ -1,1 +1,2 @@\n theorem old : True := sorry\n+def added := 1\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "Pre-existing escape hatches" in result
        assert "introduced" not in result.lower()

    def test_large_file_not_under_hard_verdict_header(self, tmp_path):
        """Regression: a pre-existing oversized file must not be reported as an
        escape hatch 'introduced in this PR'."""
        lean_file = tmp_path / "Big.lean"
        lean_file.write_text("def x := 1\n" * 2000)
        diff = "@@ -0,0 +1 @@\n+def x := 1\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "Large file" in result
        # The large-file note lives in its own neutral section, not the
        # hard-verdict "introduced" section.
        assert "triggers hard verdict rule" not in result

    def test_scan_structure_and_verdict_helper(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        diff = "@@ -0,0 +1 @@\n+theorem foo : True := sorry\n"
        scan = scan_escape_hatches({str(lean_file): diff})
        assert len(scan["introduced"]) == 1
        assert scan["introduced"][0][1] == "sorry"
        assert introduced_hatches_triggering_verdict(scan)

    def test_allowlist_suppresses_verdict_trigger(self, tmp_path, monkeypatch):
        monkeypatch.setattr(review, "ESCAPE_HATCH_ALLOWLIST", {"opaque"})
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("opaque foo : Nat\n")
        diff = "@@ -0,0 +1 @@\n+opaque foo : Nat\n"
        scan = scan_escape_hatches({str(lean_file): diff})
        assert len(scan["introduced"]) == 1
        # Allow-listed hatch is still reported but does not trigger the verdict.
        assert introduced_hatches_triggering_verdict(scan) == []


class TestDnsPinning:
    def test_pin_address_substitutes_matching_host(self):
        assert _pin_address(("example.com", 443), "example.com", "93.184.216.34") == ("93.184.216.34", 443)

    def test_pin_address_passes_through_other_host(self):
        assert _pin_address(("other.com", 443), "example.com", "1.2.3.4") == ("other.com", 443)

    def test_pin_address_preserves_extra_fields(self):
        assert _pin_address(("h", 80, 0, ("h", 80)), "h", "9.9.9.9") == ("9.9.9.9", 80, 0, ("h", 80))

    def test_pinned_dns_restores_original(self):
        import urllib3.util.connection as c
        orig = c.create_connection
        with _pinned_dns("example.com", "1.2.3.4"):
            assert c.create_connection is not orig
        assert c.create_connection is orig

    def test_pinned_dns_substitutes_within_context(self):
        import urllib3.util.connection as c
        captured = {}
        orig = c.create_connection
        try:
            c.create_connection = lambda address, *a, **k: captured.setdefault("addr", address)
            with _pinned_dns("example.com", "5.6.7.8"):
                c.create_connection(("example.com", 443))
            assert captured["addr"] == ("5.6.7.8", 443)
        finally:
            c.create_connection = orig

    def test_fetch_pins_validated_ip(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(review, "_resolve_and_validate", lambda url: (True, "", {"9.9.9.9"}))

        @contextlib.contextmanager
        def fake_pin(host, ip):
            calls["host"], calls["ip"] = host, ip
            yield
        monkeypatch.setattr(review, "_pinned_dns", fake_pin)

        class FakeSession:
            def get(self, url, timeout, headers, allow_redirects):
                return SimpleNamespace(
                    status_code=200, headers={},
                    raise_for_status=lambda: None, content=b"x", text="x",
                )
        monkeypatch.setattr(review.requests, "Session", lambda: FakeSession())

        _resp, _final = _fetch_url_content("https://example.com/doc")
        assert calls == {"host": "example.com", "ip": "9.9.9.9"}


# --- _get_diff_lines ---

class TestGetDiffLines:
    def test_basic_lines(self):
        diff = """@@ -1,3 +1,4 @@
 context line 1
+added line
 context line 2
 context line 3
"""
        lines = _get_diff_lines(diff)
        assert 1 in lines   # context
        assert 2 in lines   # added
        assert 3 in lines   # context
        assert 4 in lines   # context

    def test_empty_diff(self):
        assert _get_diff_lines("") == set()

    def test_deleted_lines_not_included(self):
        diff = """@@ -1,3 +1,2 @@
 context
-deleted line
 remaining
"""
        lines = _get_diff_lines(diff)
        assert 1 in lines
        assert 2 in lines
        # Only 2 lines in new file, no line 3


# --- _load_prompt ---

class TestLoadPrompt:
    def test_basic_replacement(self, tmp_path):
        """Test that _load_prompt correctly substitutes placeholders."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        prompt_file = prompts_dir / "test_prompt.md"
        prompt_file.write_text("Hello {{NAME}}, your role is {{ROLE}}.")

        import review
        original_path = review.ACTION_PATH
        try:
            review.ACTION_PATH = str(tmp_path)
            result = _load_prompt("test_prompt.md", {"NAME": "Alice", "ROLE": "reviewer"})
            assert result == "Hello Alice, your role is reviewer."
        finally:
            review.ACTION_PATH = original_path


# --- URL Validation ---

class TestValidateUrl:
    def test_valid_https(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
        )
        is_safe, _ = _validate_url("https://arxiv.org/pdf/2301.12345.pdf")
        assert is_safe is True

    def test_valid_http(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
        )
        is_safe, _ = _validate_url("http://example.com/file.pdf")
        assert is_safe is True

    def test_blocked_localhost(self):
        is_safe, reason = _validate_url("http://localhost:8080/secret")
        assert is_safe is False
        assert "localhost" in reason.lower()

    def test_blocked_private_ip(self):
        is_safe, reason = _validate_url("http://192.168.1.1/admin")
        assert is_safe is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_blocked_loopback(self):
        is_safe, reason = _validate_url("http://127.0.0.1/metadata")
        assert is_safe is False

    def test_blocked_non_http_scheme(self):
        is_safe, reason = _validate_url("file:///etc/passwd")
        assert is_safe is False
        assert "scheme" in reason.lower()

    def test_blocked_metadata_endpoint(self):
        is_safe, reason = _validate_url("http://metadata.google.internal/v1/instance")
        assert is_safe is False

    def test_no_hostname(self):
        is_safe, reason = _validate_url("http://")
        assert is_safe is False

    def test_blocked_private_ip_via_dns_resolution(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("169.254.169.254", 0))]
        )
        is_safe, reason, _ = _resolve_and_validate("https://example.com/spec.pdf")
        assert is_safe is False
        assert "private" in reason.lower() or "cloud metadata" in reason.lower()


class TestCheckIpSafe:
    def test_public_ip(self):
        is_safe, _ = _check_ip_safe("93.184.216.34")
        assert is_safe is True

    def test_private_ip(self):
        is_safe, reason = _check_ip_safe("192.168.1.1")
        assert is_safe is False
        assert "private" in reason.lower()

    def test_aws_metadata_ip(self):
        is_safe, reason = _check_ip_safe("169.254.169.254")
        assert is_safe is False

    def test_azure_metadata_ip(self):
        is_safe, reason = _check_ip_safe("168.63.129.16")
        assert is_safe is False
        assert "cloud metadata" in reason.lower()

    def test_alibaba_metadata_ip(self):
        is_safe, reason = _check_ip_safe("100.100.100.200")
        assert is_safe is False
        assert "cloud metadata" in reason.lower()

    def test_loopback(self):
        is_safe, reason = _check_ip_safe("127.0.0.1")
        assert is_safe is False


class TestResolveAndValidate:
    def test_safe_url(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
        )
        is_safe, reason, ips = _resolve_and_validate("https://example.com/file.pdf")
        assert is_safe is True
        assert "93.184.216.34" in ips

    def test_dns_resolves_to_private(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("10.0.0.1", 0))]
        )
        is_safe, reason, _ = _resolve_and_validate("https://example.com/file.pdf")
        assert is_safe is False

    def test_ip_url_skips_dns(self):
        is_safe, reason, ips = _resolve_and_validate("https://93.184.216.34/file.pdf")
        assert is_safe is True
        assert "93.184.216.34" in ips


class TestExternalFetch:
    def test_normalize_github_blob_url(self):
        result = _normalize_external_url("https://github.com/org/repo/blob/main/Foo.lean")
        assert result == "https://raw.githubusercontent.com/org/repo/main/Foo.lean"

    def test_redirect_revalidated_before_following(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
            if host == "example.com"
            else [(None, None, None, None, ("169.254.169.254", 0))]
        )

        class FakeSession:
            def get(self, url, timeout, headers, allow_redirects):
                if url == "https://example.com/start":
                    return SimpleNamespace(
                        status_code=302,
                        headers={"Location": "http://metadata.google.internal/secret"},
                        raise_for_status=lambda: None,
                    )
                raise AssertionError(f"Unexpected fetch: {url}")

        monkeypatch.setattr("review.requests.Session", lambda: FakeSession())

        with pytest.raises(ValueError, match="Blocked unsafe URL"):
            _fetch_url_content("https://example.com/start")


# --- Retry Logic ---

# --- REPO_CONTEXT rendering and exclusion ---

class TestFormatRepoFiles:
    def test_empty_dict_returns_placeholder(self):
        out = _format_repo_files({})
        assert "No repository context files" in out

    def test_renders_all_files_without_exclude(self):
        files = {"A.lean": "aaa", "B.lean": "bbb"}
        out = _format_repo_files(files)
        assert "content from A.lean" in out
        assert "content from B.lean" in out
        assert "aaa" in out and "bbb" in out

    def test_excludes_named_files(self):
        files = {"A.lean": "aaa", "B.lean": "bbb", "C.lean": "ccc"}
        out = _format_repo_files(files, exclude={"A.lean", "C.lean"})
        assert "B.lean" in out
        assert "A.lean" not in out
        assert "C.lean" not in out
        assert "bbb" in out
        assert "aaa" not in out

    def test_exclude_everything_returns_placeholder(self):
        files = {"A.lean": "aaa"}
        out = _format_repo_files(files, exclude={"A.lean"})
        # Sentinel so the model knows context is intentionally empty, not missing.
        assert "No repository context files" in out or "after excluding" in out

    def test_changed_files_siblings_excluded(self):
        """The core Change-1 invariant: when reviewing one changed file, the
        other changed files are not duplicated into REPO_CONTEXT (they are
        reviewed on their own per-file pass)."""
        files = {f"Compose/F{i}.lean": f"body-{i}" for i in range(5)}
        changed = set(files.keys())
        out = _format_repo_files(files, exclude=changed)
        for path in files:
            assert path not in out
        # Size of rendered context collapses to the placeholder when all
        # discovered files are also changed.
        assert len(out) < 200


# --- Prompt-size budget ---

class TestFitPromptToBudget:
    TEMPLATE = (
        "HEADER\n"
        "File: {{FILE_PATH}}\n"
        "Diff:\n{{FILE_DIFF}}\n"
        "Content:\n{{FULL_CONTENT}}\n"
        "Repo:\n{{REPO_CONTEXT}}\n"
        "FOOTER\n"
    )

    def _base(self, repo_chars=0, content_chars=100):
        return {
            "FILE_PATH": "Foo.lean",
            "FILE_DIFF": "a" * 50,
            "FULL_CONTENT": "f" * content_chars,
            "REPO_CONTEXT": "r" * repo_chars,
        }

    def test_returns_unchanged_when_under_budget(self):
        reps = self._base(repo_chars=100)
        out = _fit_replacements_to_budget(self.TEMPLATE, reps, max_chars=10_000)
        assert out == reps  # identical dict

    def test_truncates_repo_context_when_over_budget(self):
        reps = self._base(repo_chars=5_000)
        out = _fit_replacements_to_budget(self.TEMPLATE, reps, max_chars=3_000)
        # REPO_CONTEXT is trimmed; other fields untouched.
        assert len(out["REPO_CONTEXT"]) < 5_000
        assert "truncated to fit context window" in out["REPO_CONTEXT"]
        assert out["FILE_DIFF"] == reps["FILE_DIFF"]
        assert out["FULL_CONTENT"] == reps["FULL_CONTENT"]
        # Rendered result must fit.
        rendered = self.TEMPLATE
        for k, v in out.items():
            rendered = rendered.replace("{{" + k + "}}", v)
        assert len(rendered) <= 3_000

    def test_drops_repo_context_entirely_when_still_over(self):
        # FULL_CONTENT alone exceeds the budget — REPO_CONTEXT can't save it,
        # but we still mark REPO_CONTEXT omitted and warn.
        reps = self._base(repo_chars=2_000, content_chars=5_000)
        out = _fit_replacements_to_budget(self.TEMPLATE, reps, max_chars=1_000)
        assert "omitted" in out["REPO_CONTEXT"]
        # FULL_CONTENT is preserved; we don't trim the file under review.
        assert out["FULL_CONTENT"] == reps["FULL_CONTENT"]

    def test_handles_missing_trimmable_key(self):
        reps = {
            "FILE_PATH": "Foo.lean",
            "FILE_DIFF": "a" * 50,
            "FULL_CONTENT": "f" * 10_000,
            # REPO_CONTEXT deliberately missing — mirrors cross-file path which
            # uses DEPENDENCY_CONTEXT instead.
        }
        out = _fit_replacements_to_budget(self.TEMPLATE, reps, max_chars=1_000)
        # Nothing to trim; returns dict with FULL_CONTENT intact.
        assert out["FULL_CONTENT"] == reps["FULL_CONTENT"]
        assert "REPO_CONTEXT" not in out or out["REPO_CONTEXT"] == ""

    def test_trims_dependency_context(self):
        template = "D:\n{{DEPENDENCY_CONTEXT}}\nC:\n{{FULL_CONTENT}}\n"
        reps = {
            "DEPENDENCY_CONTEXT": "d" * 5_000,
            "FULL_CONTENT": "f" * 100,
        }
        out = _fit_replacements_to_budget(template, reps, max_chars=2_000)
        assert "truncated" in out["DEPENDENCY_CONTEXT"] or "omitted" in out["DEPENDENCY_CONTEXT"]
        assert out["FULL_CONTENT"] == reps["FULL_CONTENT"]

    def test_warning_logged_when_trimming(self, caplog):
        reps = self._base(repo_chars=5_000)
        with caplog.at_level("WARNING"):
            _fit_replacements_to_budget(
                self.TEMPLATE, reps, max_chars=3_000, context_label="Foo.lean"
            )
        assert any("Foo.lean" in rec.message and "REPO_CONTEXT" in rec.message
                   for rec in caplog.records)


# --- Pydantic Schema Tests ---

class TestPydanticSchemas:
    def test_file_review_schema(self):
        from review import FileReview, Finding
        review = FileReview(
            analysis="The code defines a ring homomorphism. Key risk: missing commutativity hypothesis.",
            verdict="Approved",
            checklist_results=[],
            critical_misformalizations=[],
            lean_issues=[Finding(description="test", location="Foo.lean:1")],
            nitpicks=[]
        )
        assert review.verdict == "Approved"
        assert "ring homomorphism" in review.analysis
        assert len(review.lean_issues) == 1

    def test_file_review_analysis_optional(self):
        from review import FileReview
        review = FileReview(verdict="Approved")
        assert review.analysis == ""

    def test_spec_checklist_schema(self):
        from review import SpecChecklist, ChecklistItem, ReferenceMappingEntry
        checklist = SpecChecklist(
            reference_mapping=[
                ReferenceMappingEntry(
                    paper_result="Theorem 3.1",
                    mathematical_content="For all n >= 1, the bound holds with error <= 1/n",
                    status="Present"
                )
            ],
            items=[
                ChecklistItem(
                    concept="Completeness",
                    verification_steps=["Check hypotheses"],
                    severity="Critical"
                )
            ]
        )
        assert len(checklist.reference_mapping) == 1
        assert checklist.items[0].severity == "Critical"

    def test_cross_file_analysis_schema(self):
        from review import CrossFileAnalysis, Finding
        analysis = CrossFileAnalysis(
            composition_issues=[Finding(description="type mismatch", location="A.lean -> B.lean")],
            escape_hatch_impact=[],
            external_dependency_issues=[],
            missing_cross_file_verification=[]
        )
        assert len(analysis.composition_issues) == 1

    def test_triage_result_schema(self):
        from review import TriageResult, ReviewCluster
        triage = TriageResult(clusters=[
            ReviewCluster(
                name="Sumcheck chain",
                files=["A.lean", "B.lean"],
                review_question="Do types match?",
                priority="critical",
                review_strategy="Check that error bounds compose across the sumcheck chain.",
                key_hypotheses=["Output type of Steps.lean matches input of CoreInteraction.lean"]
            )
        ])
        assert triage.clusters[0].priority == "critical"
        assert "error bounds" in triage.clusters[0].review_strategy
        assert len(triage.clusters[0].key_hypotheses) == 1

    def test_triage_strategy_optional(self):
        from review import ReviewCluster
        cluster = ReviewCluster(name="test", files=["A.lean"], review_question="", priority="low")
        assert cluster.review_strategy == ""
        assert cluster.key_hypotheses == []

    def test_cross_file_analysis_has_analysis(self):
        from review import CrossFileAnalysis
        analysis = CrossFileAnalysis(
            analysis="Traced chain: A.lean -> B.lean -> C.lean. Type flow is consistent.",
        )
        assert "Traced chain" in analysis.analysis


# --- Structured Synthesis Input ---

class TestStructuredSynthesisInput:
    def test_structured_data_serialization(self):
        """Verify structured review data is correctly serialized for synthesis."""
        from review import FileReview, Finding, ChecklistResult

        reviews = {
            "Foo.lean": FileReview(
                verdict="Changes Requested",
                checklist_results=[
                    ChecklistResult(item="Completeness", status="violated", explanation="Missing hypothesis"),
                    ChecklistResult(item="Soundness", status="satisfied", explanation="OK"),
                ],
                critical_misformalizations=[Finding(description="Wrong bound")],
                lean_issues=[Finding(description="Issue 1"), Finding(description="Issue 2")],
                nitpicks=[]
            ),
            "Bar.lean": FileReview(
                verdict="Approved",
                checklist_results=[],
                critical_misformalizations=[],
                lean_issues=[],
                nitpicks=[Finding(description="Naming")]
            ),
        }

        # Build structured data the same way synthesize_overall_summary does
        structured = {}
        for fp, fr in reviews.items():
            structured[fp] = {
                "verdict": fr.verdict,
                "critical_count": len(fr.critical_misformalizations),
                "issue_count": len(fr.lean_issues),
                "nitpick_count": len(fr.nitpicks),
                "violated_checklist": [cr.item for cr in fr.checklist_results if cr.status == "violated"],
                "unclear_checklist": [cr.item for cr in fr.checklist_results if cr.status == "unclear"],
            }

        assert structured["Foo.lean"]["verdict"] == "Changes Requested"
        assert structured["Foo.lean"]["critical_count"] == 1
        assert structured["Foo.lean"]["issue_count"] == 2
        assert structured["Foo.lean"]["violated_checklist"] == ["Completeness"]
        assert structured["Bar.lean"]["verdict"] == "Approved"
        assert structured["Bar.lean"]["nitpick_count"] == 1

        # Verify it serializes to valid JSON
        json_str = json.dumps(structured, indent=2)
        parsed = json.loads(json_str)
        assert parsed["Foo.lean"]["critical_count"] == 1


class TestMainFlow:
    def test_main_exits_early_when_no_lean_files_changed(self, monkeypatch, capsys):
        import review

        monkeypatch.setattr(review, "get_pr_diff", lambda pr_number: ("diff --git a/README.md b/README.md\n", []))

        def fail_create_provider(*args, **kwargs):
            raise AssertionError("Provider setup should not run for non-Lean PRs")

        monkeypatch.setattr(review, "create_provider", fail_create_provider)
        monkeypatch.setattr(sys, "argv", ["review.py", "--pr-number", "123"])

        review.main()
        output = capsys.readouterr().out
        assert "No Lean files were changed in this PR." in output


# --- _build_line_annotations ---

class TestBuildLineAnnotations:
    """The diff touches new-file lines 6 and 7."""

    _DIFF = (
        "diff --git a/Foo.lean b/Foo.lean\n"
        "--- a/Foo.lean\n"
        "+++ b/Foo.lean\n"
        "@@ -1,2 +5,3 @@\n"
        " context\n"
        "+added line 6\n"
        "+added line 7\n"
    )

    def _review(self, **kwargs):
        from review import FileReview, Finding  # noqa: F401
        return FileReview(verdict="Needs Minor Revisions", analysis="", **kwargs)

    def _finding(self, location):
        from review import Finding
        return Finding(description="d", location=location, suggested_fix="")

    def test_in_diff_line_annotated(self):
        review = self._review(critical_misformalizations=[self._finding("Foo.lean:6")])
        ann = _build_line_annotations({"Foo.lean": review}, {"Foo.lean": self._DIFF})
        assert len(ann) == 1
        assert ann[0]["line"] == 6
        assert ann[0]["side"] == "RIGHT"

    def test_nearby_line_snaps_into_diff(self):
        # Line 9 is not in the diff, but 7 (=9-2) is within the ±5 window.
        review = self._review(lean_issues=[self._finding("Foo.lean:9")])
        ann = _build_line_annotations({"Foo.lean": review}, {"Foo.lean": self._DIFF})
        assert len(ann) == 1
        assert ann[0]["line"] == 7

    def test_far_line_dropped(self):
        review = self._review(nitpicks=[self._finding("Foo.lean:100")])
        ann = _build_line_annotations({"Foo.lean": review}, {"Foo.lean": self._DIFF})
        assert ann == []

    def test_location_without_line_number_dropped(self):
        review = self._review(critical_misformalizations=[self._finding("Foo.lean")])
        ann = _build_line_annotations({"Foo.lean": review}, {"Foo.lean": self._DIFF})
        assert ann == []

    def test_empty_location_dropped(self):
        review = self._review(critical_misformalizations=[self._finding("")])
        ann = _build_line_annotations({"Foo.lean": review}, {"Foo.lean": self._DIFF})
        assert ann == []

    def test_range_location_uses_first_line(self):
        review = self._review(lean_issues=[self._finding("Foo.lean:6-20")])
        ann = _build_line_annotations({"Foo.lean": review}, {"Foo.lean": self._DIFF})
        assert len(ann) == 1
        assert ann[0]["line"] == 6

    def test_none_review_skipped(self):
        ann = _build_line_annotations({"Foo.lean": None}, {"Foo.lean": self._DIFF})
        assert ann == []


# --- grounding: evidence/confidence rendering + local spec refs ---

class TestFindingRendering:
    def test_evidence_and_confidence_rendered(self):
        f = review.Finding(description="wrong hyp", location="F.lean:3",
                           evidence="Paper Thm 3.2 requires commutativity", confidence="high",
                           suggested_fix="add [CommRing R]")
        lines = _finding_lines(f)
        text = "\n".join(lines)
        assert "wrong hyp" in text
        assert "confidence: high" in text
        assert "Evidence: Paper Thm 3.2 requires commutativity" in text
        assert "Suggested fix: add [CommRing R]" in text

    def test_defaults_have_confidence_no_evidence_line(self):
        f = review.Finding(description="d")
        text = "\n".join(_finding_lines(f))
        assert "confidence: medium" in text
        assert "Evidence:" not in text


class TestLocalReferenceParts:
    def test_text_file_becomes_text_part(self, tmp_path):
        p = tmp_path / "spec.md"
        p.write_text("# Spec\nThe ring must be commutative.\n")
        parts, errors = get_local_reference_parts(str(p))
        assert errors == []
        assert len(parts) == 1
        assert parts[0].type == "text"
        assert "commutative" in parts[0].data

    def test_pdf_file_becomes_pdf_part(self, tmp_path):
        p = tmp_path / "paper.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        parts, errors = get_local_reference_parts(str(p))
        assert len(parts) == 1
        assert parts[0].type == "pdf"

    def test_missing_path_reports_error(self):
        parts, errors = get_local_reference_parts("/no/such/spec.md")
        assert parts == []
        assert errors and "Could not find" in errors[0]

    def test_directory_expanded(self, tmp_path):
        (tmp_path / "a.md").write_text("alpha")
        (tmp_path / "b.txt").write_text("beta")
        (tmp_path / "blueprint.tex").write_text("gamma-blueprint")  # LaTeX blueprint
        (tmp_path / "ignore.py").write_text("nope")
        parts, errors = get_local_reference_parts(str(tmp_path))
        datas = " ".join(p.data for p in parts if p.type == "text")
        assert "alpha" in datas and "beta" in datas
        assert "gamma-blueprint" in datas   # .tex picked up (blueprints)
        assert "nope" not in datas

    def test_empty_returns_nothing(self):
        assert get_local_reference_parts("") == ([], [])


# --- chunked map-reduce review ---

class TestChunkHelpers:
    _FILE = "def a := 1\ndef b := 2\ndef c := 3\ndef d := 4\n"

    def test_empty_file_no_chunks(self):
        assert _chunk_file_by_declarations("", 100) == []

    def test_small_file_single_chunk_covers_all(self):
        chunks = _chunk_file_by_declarations(self._FILE, 10_000)
        assert len(chunks) == 1
        assert chunks[0][0] == 1 and chunks[0][1] == 4
        assert chunks[0][2] == self._FILE

    def test_splits_at_declaration_boundaries_no_gaps(self):
        chunks = _chunk_file_by_declarations(self._FILE, 15)  # ~1 decl (11 chars) each
        # Reassembled chunks must equal the original file exactly (no gaps/overlap).
        assert "".join(c[2] for c in chunks) == self._FILE
        # Ranges are contiguous.
        for prev, nxt in zip(chunks, chunks[1:]):
            assert nxt[0] == prev[1] + 1

    def test_oversized_single_declaration_is_own_chunk(self):
        big = "def huge := " + "x" * 500 + "\n"
        chunks = _chunk_file_by_declarations(big, 50)
        assert len(chunks) == 1
        assert chunks[0][2] == big

    def test_preamble_grouped_before_first_decl(self):
        content = "import Foo\nimport Bar\ndef a := 1\n"
        chunks = _chunk_file_by_declarations(content, 10_000)
        assert "".join(c[2] for c in chunks) == content

    def test_parse_diff_hunks_ranges(self):
        diff = (
            "diff --git a/F.lean b/F.lean\n--- a/F.lean\n+++ b/F.lean\n"
            "@@ -2,3 +2,4 @@\n context\n+added\n"
            "@@ -20 +21 @@\n-old\n+new\n"
        )
        hunks = _parse_diff_hunks(diff)
        assert len(hunks) == 2
        assert hunks[0][0] == 2 and hunks[0][1] == 5   # +2,4 -> lines 2..5
        assert hunks[1][0] == 21 and hunks[1][1] == 21

    def test_diff_for_range_selects_overlapping(self):
        diff = (
            "diff --git a/F.lean b/F.lean\n--- a/F.lean\n+++ b/F.lean\n"
            "@@ -2 +2 @@\n+b\n"
            "@@ -4 +4 @@\n+d\n"
        )
        header = _diff_header(diff)
        hunks = _parse_diff_hunks(diff)
        assert "diff --git" in header
        # Range covering only line 2 selects the first hunk, not the second.
        sliced = _diff_for_range(header, hunks, 1, 2)
        assert "+b" in sliced and "+d" not in sliced
        # Non-overlapping range selects nothing.
        assert _diff_for_range(header, hunks, 10, 12) == ""

    def test_merge_file_reviews(self):
        r1 = review.FileReview(verdict="Approved", analysis="a1",
                               nitpicks=[review.Finding(description="n1")])
        r2 = review.FileReview(verdict="Changes Requested", analysis="a2",
                               critical_misformalizations=[review.Finding(description="c1")],
                               nitpicks=[review.Finding(description="n1")])  # dup nitpick
        merged = _merge_file_reviews([r1, None, r2])
        assert merged.verdict == "Changes Requested"     # worst wins
        assert len(merged.critical_misformalizations) == 1
        assert len(merged.nitpicks) == 1                 # deduplicated
        assert "a1" in merged.analysis and "a2" in merged.analysis

    def test_merge_all_none_returns_none(self):
        assert _merge_file_reviews([None, None]) is None


class TestChunkedFileReview:
    _FILE = "def a := 1\ndef b := 2\ndef c := 3\ndef d := 4\n"
    _DIFF = (
        "diff --git a/F.lean b/F.lean\n--- a/F.lean\n+++ b/F.lean\n"
        "@@ -2 +2 @@\n-def b := 0\n+def b := 2\n"
        "@@ -4 +4 @@\n-def d := 0\n+def d := 4\n"
    )

    def test_large_file_chunked_and_merged(self, monkeypatch):
        monkeypatch.setattr(review, "MAX_FILE_REVIEW_CHARS", 15)
        calls = {"n": 0}

        def fake_gen(model, contents, schema, thinking_budget=None):
            calls["n"] += 1
            fr = review.FileReview(verdict="Approved",
                                   nitpicks=[review.Finding(description=f"n{calls['n']}")])
            return fr, review.TokenUsage()

        provider = SimpleNamespace(generate_structured=fake_gen, name="fake")
        merged, formatted = review.analyze_file_changes_with_context(
            provider, {"review_model": "m"}, "F.lean", self._DIFF, self._FILE, "", [], "CHK", "VR")
        assert calls["n"] == 2               # only the 2 changed sections reviewed
        assert len(merged.nitpicks) == 2
        assert merged.coverage_incomplete is False
        assert "Review for" not in formatted  # sanity: formatted markdown returned

    def test_partial_chunk_failure_flags_incomplete(self, monkeypatch):
        monkeypatch.setattr(review, "MAX_FILE_REVIEW_CHARS", 15)
        calls = {"n": 0}

        def fake_gen(model, contents, schema, thinking_budget=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return review.FileReview(verdict="Approved"), review.TokenUsage()

        provider = SimpleNamespace(generate_structured=fake_gen, name="fake")
        merged, _ = review.analyze_file_changes_with_context(
            provider, {"review_model": "m"}, "F.lean", self._DIFF, self._FILE, "", [], "CHK", "VR")
        assert merged is not None
        assert merged.coverage_incomplete is True
        assert "Incomplete chunked review" in merged.analysis

    def test_all_chunks_fail_returns_none(self, monkeypatch):
        monkeypatch.setattr(review, "MAX_FILE_REVIEW_CHARS", 15)

        def fake_gen(model, contents, schema, thinking_budget=None):
            raise RuntimeError("boom")

        provider = SimpleNamespace(generate_structured=fake_gen, name="fake")
        merged, err = review.analyze_file_changes_with_context(
            provider, {"review_model": "m"}, "F.lean", self._DIFF, self._FILE, "", [], "CHK", "VR")
        assert merged is None
        assert "chunked" in err


# --- Lean tool wiring ---

class TestLeanToolWiring:
    def _capturing_provider(self, captured, result):
        def gen(model, contents, schema, thinking_budget=None, tools=None, tool_runner=None, max_tool_rounds=4):
            captured["tools"] = tools
            captured["runner"] = tool_runner
            return result, review.TokenUsage()
        return SimpleNamespace(generate_structured=gen, name="fake")

    def test_make_toolbox_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(review, "LEAN_TOOLS_ENABLED", False)
        assert review._make_toolbox("M") is None

    def test_make_toolbox_enabled_scopes_module(self, monkeypatch):
        monkeypatch.setattr(review, "LEAN_TOOLS_ENABLED", True)
        tb = review._make_toolbox("Proj.Foo")
        assert tb is not None and tb.module == "Proj.Foo"

    def test_call_provider_passes_tools_when_toolbox_given(self):
        captured = {}
        provider = self._capturing_provider(captured, review.FindingVerdict(verdict="confirmed", reasoning="r"))
        review._call_provider(provider, "m", [review.ContentPart("text", "x")],
                              review.FindingVerdict, toolbox=review.LeanToolbox(module="M"))
        assert captured["tools"] is not None and len(captured["tools"]) == 4
        assert callable(captured["runner"])

    def test_call_provider_no_toolbox_no_tools(self):
        captured = {}
        provider = self._capturing_provider(captured, review.FindingVerdict(verdict="confirmed", reasoning="r"))
        review._call_provider(provider, "m", [review.ContentPart("text", "x")],
                              review.FindingVerdict, toolbox=None)
        assert captured["tools"] is None
        assert captured["runner"] is None

    def test_per_file_reviewer_uses_tools_when_enabled(self, monkeypatch):
        monkeypatch.setattr(review, "LEAN_TOOLS_ENABLED", True)
        captured = {}
        provider = self._capturing_provider(captured, review.FileReview(verdict="Approved"))
        review.analyze_file_changes_with_context(
            provider, {"review_model": "m"}, "Foo.lean",
            "@@ -0,0 +1 @@\n+def x := 1\n", "def x := 1\n", "", [], "CHK", "VR")
        assert captured["tools"] is not None

    def test_verifier_uses_tools_when_enabled(self, monkeypatch):
        monkeypatch.setattr(review, "LEAN_TOOLS_ENABLED", True)
        monkeypatch.setattr(review.file_cache, "read", lambda p: "content")
        captured = {}
        provider = self._capturing_provider(captured, review.FindingVerdict(verdict="confirmed", reasoning="r"))
        r = review.FileReview(verdict="Changes Requested",
                              critical_misformalizations=[review.Finding(description="x")])
        review.verify_findings(provider, {"A.lean": r}, None, {"A.lean": "d"}, "", "m", max_workers=2)
        assert captured["tools"] is not None


# --- triage robustness ---

class TestTriageRobustness:
    def test_triage_template_trims_diffs_keeps_signatures(self):
        """Over budget, the triage template drops the bulky diffs but preserves
        the type signatures it can still cluster from."""
        reps = {
            "DEPENDENCY_GRAPH": "graph",
            "ALL_DIFFS": "D" * 5000,
            "SPEC_CHECKLIST": "spec",
            "ADDITIONAL_COMMENTS": "",
            "CHANGED_FILE_SIGNATURES": "signatures-here",
        }
        out = review._fit_prompt_to_budget("triage.md", reps, max_chars=800, trimmable=("ALL_DIFFS",))
        assert len(out["ALL_DIFFS"]) < 5000
        assert out["CHANGED_FILE_SIGNATURES"] == "signatures-here"

    def test_run_triage_marks_diffs_trimmable(self, monkeypatch):
        """run_triage budget-guards with ALL_DIFFS as the trimmable key so a huge
        PR degrades instead of failing to a per-file fallback."""
        monkeypatch.delenv("LAKE_GRAPH", raising=False)
        captured = {}

        def fake_fit(template_name, replacements, context_label="", trimmable=None):
            captured["template"] = template_name
            captured["trimmable"] = trimmable
            return replacements
        monkeypatch.setattr(review, "_fit_prompt_to_budget", fake_fit)

        def gen(model, contents, schema, thinking_budget=None):
            return review.TriageResult(clusters=[
                review.ReviewCluster(name="c", files=["A.lean"], review_question="q", priority="high"),
            ]), review.TokenUsage()
        provider = SimpleNamespace(generate_structured=gen, name="fake")

        clusters = review.run_triage(provider, {"A.lean": "+x\n"}, "", "", "m")
        assert clusters[0].name == "c"
        assert captured["template"] == "triage.md"
        assert captured["trimmable"] == ("ALL_DIFFS",)


# --- dependent-impact pass (second-order) ---

class TestDependentImpact:
    def test_find_dependents_maps_paths(self):
        graph = [
            {"name": "Proj.A", "imports": ["Proj.Base"]},    # dependent of the changed file
            {"name": "Proj.Base", "imports": []},            # the changed file
            {"name": "Proj.C", "imports": ["Proj.Other"]},   # unrelated
        ]
        repo = {"Proj/A.lean": "contentA", "Proj/C.lean": "contentC"}
        deps = review.find_dependent_files(json.dumps(graph), {"Proj/Base.lean"}, repo, 10)
        assert deps == {"Proj/A.lean": "contentA"}

    def test_respects_cap(self):
        graph = [{"name": f"P.D{i}", "imports": ["P.Base"]} for i in range(5)]
        graph.append({"name": "P.Base", "imports": []})
        repo = {f"P/D{i}.lean": f"c{i}" for i in range(5)}
        deps = review.find_dependent_files(json.dumps(graph), {"P/Base.lean"}, repo, 2)
        assert len(deps) == 2

    def test_no_graph_or_zero_max_empty(self):
        assert review.find_dependent_files("", {"A.lean"}, {"A.lean": "x"}, 10) == {}
        assert review.find_dependent_files("[]", {"A.lean"}, {}, 0) == {}

    def test_analyze_merges_breakages(self):
        def gen(model, contents, schema, thinking_budget=None):
            return review.CrossFileAnalysis(
                composition_issues=[review.Finding(description="breaks X")]), review.TokenUsage()
        provider = SimpleNamespace(generate_structured=gen, name="fake")
        res = review.analyze_dependent_impact(provider, {"Dep.lean": "content"}, "diffs", "", [], "m", max_workers=2)
        assert res is not None
        assert [f.description for f in res.composition_issues] == ["breaks X"]

    def test_analyze_no_findings_returns_none(self):
        def gen(*a, **k):
            return review.CrossFileAnalysis(), review.TokenUsage()
        provider = SimpleNamespace(generate_structured=gen, name="fake")
        assert review.analyze_dependent_impact(provider, {"Dep.lean": "c"}, "d", "", [], "m") is None

    def test_analyze_no_dependents_none(self):
        assert review.analyze_dependent_impact(SimpleNamespace(name="fake"), {}, "d", "", [], "m") is None

    def test_merge_into_none_returns_extra(self):
        extra = review.CrossFileAnalysis(composition_issues=[review.Finding(description="b")])
        assert review._merge_cross_file(None, extra) is extra

    def test_merge_extends_lists_and_analysis(self):
        base = review.CrossFileAnalysis(composition_issues=[review.Finding(description="a")], analysis="A")
        extra = review.CrossFileAnalysis(composition_issues=[review.Finding(description="b")], analysis="B")
        merged = review._merge_cross_file(base, extra)
        assert [f.description for f in merged.composition_issues] == ["a", "b"]
        assert "A" in merged.analysis and "B" in merged.analysis


# --- verify_findings (precision stage) ---

class TestVerifyFindings:
    def _review(self, **kw):
        return review.FileReview(verdict="Changes Requested", **kw)

    def _provider(self, verdict_map):
        """Fake provider that reads the finding description out of the rendered
        verifier prompt and returns the mapped verdict (default 'uncertain')."""
        def gen(model, contents, schema, thinking_budget=None):
            text = contents[-1].data
            verdict = "uncertain"
            for desc, verd in verdict_map.items():
                if desc in text:
                    verdict = verd
                    break
            return review.FindingVerdict(verdict=verdict, reasoning="because"), review.TokenUsage()
        return SimpleNamespace(generate_structured=gen, name="fake")

    def test_refuted_dropped_others_kept(self, monkeypatch):
        monkeypatch.setattr(review.file_cache, "read", lambda p: "content")
        r = self._review(
            critical_misformalizations=[review.Finding(description="bad hyp")],
            lean_issues=[review.Finding(description="ok issue")],
        )
        provider = self._provider({"bad hyp": "refuted", "ok issue": "confirmed"})
        refuted = review.verify_findings(provider, {"A.lean": r}, None, {"A.lean": "diff"}, "", "m", max_workers=2)
        assert [f.description for f, _ in refuted] == ["bad hyp"]
        assert r.critical_misformalizations == []
        assert [f.description for f in r.lean_issues] == ["ok issue"]

    def test_verifier_error_keeps_finding(self, monkeypatch):
        monkeypatch.setattr(review.file_cache, "read", lambda p: "c")
        r = self._review(critical_misformalizations=[review.Finding(description="bad")])

        def gen(*a, **k):
            raise RuntimeError("boom")
        provider = SimpleNamespace(generate_structured=gen, name="fake")
        refuted = review.verify_findings(provider, {"A.lean": r}, None, {"A.lean": "d"}, "", "m", max_workers=2)
        assert refuted == []                       # fail-open
        assert len(r.critical_misformalizations) == 1

    def test_uncertain_keeps_finding(self, monkeypatch):
        monkeypatch.setattr(review.file_cache, "read", lambda p: "c")
        r = self._review(lean_issues=[review.Finding(description="maybe")])
        provider = self._provider({"maybe": "uncertain"})
        refuted = review.verify_findings(provider, {"A.lean": r}, None, {"A.lean": "d"}, "", "m")
        assert refuted == []
        assert len(r.lean_issues) == 1

    def test_cross_file_finding_refuted(self, monkeypatch):
        monkeypatch.setattr(review.file_cache, "read", lambda p: "c")
        cf = review.CrossFileAnalysis(composition_issues=[review.Finding(description="mismatch")])
        provider = self._provider({"mismatch": "refuted"})
        refuted = review.verify_findings(provider, {}, cf, {"A.lean": "d"}, "", "m")
        assert len(refuted) == 1
        assert cf.composition_issues == []

    def test_nitpicks_not_verified(self, monkeypatch):
        monkeypatch.setattr(review.file_cache, "read", lambda p: "c")
        r = self._review(nitpicks=[review.Finding(description="nit")])
        called = {"n": 0}

        def gen(model, contents, schema, thinking_budget=None):
            called["n"] += 1
            return review.FindingVerdict(verdict="refuted", reasoning=""), review.TokenUsage()
        provider = SimpleNamespace(generate_structured=gen, name="fake")
        refuted = review.verify_findings(provider, {"A.lean": r}, None, {"A.lean": "d"}, "", "m")
        assert refuted == []
        assert called["n"] == 0        # nitpicks are not verdict-driving; not checked
        assert len(r.nitpicks) == 1

    def test_no_findings_no_calls(self):
        called = {"n": 0}

        def gen(*a, **k):
            called["n"] += 1
            return review.FindingVerdict(verdict="confirmed", reasoning=""), review.TokenUsage()
        provider = SimpleNamespace(generate_structured=gen, name="fake")
        refuted = review.verify_findings(provider, {"A.lean": self._review()}, None, {}, "", "m")
        assert refuted == []
        assert called["n"] == 0


# --- compute_deterministic_verdict ---

class TestDeterministicVerdict:
    _CLEAN = {"introduced": [], "preexisting": [], "large_files": []}

    def _fr(self, **kw):
        return review.FileReview(verdict="Approved", **kw)

    def test_clean_approved(self):
        v, _ = review.compute_deterministic_verdict(self._CLEAN, {"A.lean": self._fr()}, None, False)
        assert v == "Approved"

    def test_introduced_hatch_forces_changes_requested(self):
        scan = {"introduced": [("A.lean", "sorry", "x")], "preexisting": [], "large_files": []}
        v, reasons = review.compute_deterministic_verdict(scan, {"A.lean": self._fr()}, None, False)
        assert v == "Changes Requested"
        assert any("Escape hatch" in r for r in reasons)

    def test_allowlisted_hatch_does_not_force(self, monkeypatch):
        monkeypatch.setattr(review, "ESCAPE_HATCH_ALLOWLIST", {"sorry"})
        scan = {"introduced": [("A.lean", "sorry", "x")], "preexisting": [], "large_files": []}
        v, _ = review.compute_deterministic_verdict(scan, {"A.lean": self._fr()}, None, False)
        assert v == "Approved"

    def test_critical_finding_changes_requested(self):
        fr = self._fr(critical_misformalizations=[review.Finding(description="bad")])
        v, _ = review.compute_deterministic_verdict(self._CLEAN, {"A.lean": fr}, None, False)
        assert v == "Changes Requested"

    def test_only_nitpicks_minor(self):
        fr = self._fr(nitpicks=[review.Finding(description="style")])
        v, _ = review.compute_deterministic_verdict(self._CLEAN, {"A.lean": fr}, None, False)
        assert v == "Needs Minor Revisions"

    def test_cross_file_issue_changes_requested(self):
        cf = review.CrossFileAnalysis(composition_issues=[review.Finding(description="mismatch")])
        v, _ = review.compute_deterministic_verdict(self._CLEAN, {"A.lean": self._fr()}, cf, False)
        assert v == "Changes Requested"

    def test_incomplete_review_blocks_approved(self):
        v, reasons = review.compute_deterministic_verdict(self._CLEAN, {"A.lean": None}, None, True)
        assert v == "Needs Minor Revisions"
        assert any("coverage gap" in r for r in reasons)

    def test_incomplete_does_not_downgrade_changes_requested(self):
        fr = self._fr(critical_misformalizations=[review.Finding(description="bad")])
        v, _ = review.compute_deterministic_verdict(self._CLEAN, {"A.lean": fr}, None, True)
        assert v == "Changes Requested"


# --- split_into_comments (multi-comment output) ---

class TestSplitIntoComments:
    def test_small_returns_single(self):
        assert review.split_into_comments("hello", 100) == ["hello"]

    def test_splits_and_respects_size(self):
        header = "### Review\n\nsummary\n"
        detail = "\n<details><summary>x</summary>\n" + ("y" * 30) + "\n</details>\n"
        body = header + detail * 10
        parts = review.split_into_comments(body, 200)
        assert len(parts) > 1
        assert all(len(p) <= 200 for p in parts)
        assert parts[0].startswith("### Review")
        assert "continued (part 2/" in parts[1]

    def test_oversized_single_section_hard_sliced(self):
        body = "\n<details>" + ("z" * 500) + "</details>"
        parts = review.split_into_comments(body, 120)
        assert len(parts) > 1
        assert all(len(p) <= 120 for p in parts)

    def test_all_content_preserved(self):
        body = "HEAD" + "".join(f"\n<details>seg{i}{'q' * 50}</details>" for i in range(20))
        parts = review.split_into_comments(body, 300)
        assert len(parts) > 1
        rebuilt = parts[0]
        for p in parts[1:]:
            rebuilt += p.split("\n\n", 1)[1]   # drop the continuation header
        assert rebuilt == body


# --- _build_contents prompt-cache prefix ---

class TestBuildContents:
    def test_caches_prefix_without_external_refs(self):
        c = review._build_contents("PROMPT")
        assert c[0].data == review.OPERATING_CONTRACT
        assert c[0].cache is True            # contract cached even with no external refs
        assert c[-1].data == "PROMPT" and c[-1].cache is False
        assert sum(1 for p in c if p.cache) == 1

    def test_breakpoint_on_last_external(self):
        ext = [review.ContentPart("text", "REF1"),
               review.ContentPart("pdf", b"x", mime_type="application/pdf")]
        c = review._build_contents("PROMPT", ext)
        cached = [p for p in c if p.cache]
        assert len(cached) == 1              # exactly one breakpoint
        assert cached[0].type == "pdf"       # at the end of the stable prefix
        assert c[-1].data == "PROMPT" and c[-1].cache is False
        assert ext[1].cache is False         # original parts not mutated

    def test_prefix_identical_across_calls(self):
        a = review._build_contents("FILE-A")
        b = review._build_contents("FILE-B")
        assert [(p.type, p.data, p.cache) for p in a[:-1]] == \
               [(p.type, p.data, p.cache) for p in b[:-1]]


# --- per-file review prompt-cache split ---

class TestPerFileCacheSplit:
    def _capture_provider(self, captured):
        def gen(model, contents, schema, thinking_budget=None, **kw):
            captured.setdefault("contents", []).append(contents)
            return review.FileReview(verdict="Approved"), review.TokenUsage()
        return SimpleNamespace(generate_structured=gen, name="fake")

    def test_stable_prefix_cached_volatile_not(self, monkeypatch):
        monkeypatch.setattr(review, "LEAN_TOOLS_ENABLED", False)
        captured = {}
        provider = self._capture_provider(captured)
        ctx = {"review_model": "m", "repo_context": "REPOCTX", "additional_comments": ""}
        review.analyze_file_changes_with_context(
            provider, ctx, "Foo.lean", "@@ -0,0 +1 @@\n+def foo := 1\n", "def foo := 1\n",
            "", [], "LEAN4CHK", "VERDICTRULES")
        contents = captured["contents"][0]
        cached = [p for p in contents if p.cache]
        assert len(cached) == 1                     # single cache breakpoint
        prefix = cached[0].data
        assert "REPOCTX" in prefix and "LEAN4CHK" in prefix and "VERDICTRULES" in prefix
        volatile = contents[-1]
        assert volatile.cache is False
        assert "def foo := 1" in volatile.data and "Foo.lean" in volatile.data
        # the split marker never reaches the model
        assert all(review.CACHE_SPLIT_MARKER not in p.data for p in contents if isinstance(p.data, str))

    def test_prefix_identical_across_files(self, monkeypatch):
        monkeypatch.setattr(review, "LEAN_TOOLS_ENABLED", False)
        captured = {}
        provider = self._capture_provider(captured)
        ctx = {"review_model": "m", "repo_context": "REPOCTX", "additional_comments": ""}
        for fp, body in [("A.lean", "def a := 1\n"), ("B.lean", "def b := 2\n")]:
            review.analyze_file_changes_with_context(
                provider, ctx, fp, f"@@ -0,0 +1 @@\n+{body}", body, "", [], "L4", "VR")
        c0, c1 = captured["contents"]
        prefix0 = [p for p in c0 if p.cache][0].data
        prefix1 = [p for p in c1 if p.cache][0].data
        assert prefix0 == prefix1                    # identical stable prefix → cache hit


# --- main() orchestration (mocked provider/agents) ---

class TestMainOrchestration:
    _SINGLE = (
        "diff --git a/Foo.lean b/Foo.lean\n"
        "--- a/Foo.lean\n+++ b/Foo.lean\n"
        "@@ -1,2 +1,3 @@\n import Bar\n+theorem t : True := trivial\n def f := 1\n"
    )
    _TWO = _SINGLE + (
        "diff --git a/Bar.lean b/Bar.lean\n"
        "--- a/Bar.lean\n+++ b/Bar.lean\n"
        "@@ -1,1 +1,2 @@\n def g := 1\n+def h := 2\n"
    )

    def _common_patches(self, monkeypatch, diff):
        import review
        fr = review.FileReview(verdict="Approved", analysis="fine")
        monkeypatch.setattr(review, "get_pr_diff", lambda pr: (diff, []))
        monkeypatch.setattr(review, "create_provider", lambda key, **kw: SimpleNamespace(name="fake"))
        monkeypatch.setattr(review, "analyze_specification", lambda *a, **k: "")
        calls = {"review": 0}

        def fake_review(provider, ctx, fp, fd, fc, spec, ext, chk, vr):
            calls["review"] += 1
            return fr, f"FORMATTED-REVIEW::{fp}"

        monkeypatch.setattr(review, "analyze_file_changes_with_context", fake_review)
        monkeypatch.setenv("API_KEY", "sk-test")
        # Keep env clean of stray context from other tests.
        for var in ("SUMMARY_FILES", "LEAN_INFO", "DISCOVERED_FILES", "BUILD_OUTPUT", "LAKE_GRAPH"):
            monkeypatch.delenv(var, raising=False)
        return review, calls

    def _run(self, monkeypatch, tmp_path, capsys):
        import review
        monkeypatch.setattr(sys, "argv",
                            ["review.py", "--pr-number", "1", "--model", "anthropic/claude-haiku-4.5"])
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            review.main()
        finally:
            os.chdir(cwd)
        return capsys.readouterr().out

    def test_single_file_skips_synthesis(self, monkeypatch, tmp_path, capsys):
        review, calls = self._common_patches(monkeypatch, self._SINGLE)
        out = self._run(monkeypatch, tmp_path, capsys)
        assert "🤖 AI Review" in out
        assert "FORMATTED-REVIEW::Foo.lean" in out
        assert "Review for `Foo.lean`" in out
        assert "Verdict (deterministic): Approved" in out   # authoritative headline
        assert calls["review"] == 1

    def test_two_files_run_cross_file_and_synthesis(self, monkeypatch, tmp_path, capsys):
        review, calls = self._common_patches(monkeypatch, self._TWO)
        cfa = review.CrossFileAnalysis(analysis="traced")
        monkeypatch.setattr(review, "analyze_cross_file",
                            lambda *a, **k: (cfa, "CROSS-FILE-TEXT"))
        synth = review.SynthesisSummary(tldr="t", precheck_summary="p", overall_verdict="Approved")
        monkeypatch.setattr(review, "synthesize_overall_summary",
                            lambda *a, **k: (synth, "SYNTH-SUMMARY"))
        out = self._run(monkeypatch, tmp_path, capsys)
        # Summary is re-rendered from the structured object so the authoritative
        # verdict is reflected (the pre-formatted "SYNTH-SUMMARY" is superseded).
        assert "**TL;DR:** t" in out
        assert "Verdict (deterministic): Approved" in out
        assert "CROSS-FILE-TEXT" in out
        assert "FORMATTED-REVIEW::Foo.lean" in out
        assert "FORMATTED-REVIEW::Bar.lean" in out
        assert calls["review"] == 2

    def test_verification_filters_finding_and_relaxes_verdict(self, monkeypatch, tmp_path, capsys):
        """End-to-end: a critical finding the verifier refutes is dropped, the
        deterministic verdict relaxes from Changes Requested to Approved, and the
        refuted finding is disclosed in the transparency section."""
        fr = review.FileReview(
            verdict="Changes Requested",
            critical_misformalizations=[review.Finding(description="dubious claim")],
        )
        monkeypatch.setattr(review, "get_pr_diff", lambda pr: (self._SINGLE, []))
        monkeypatch.setattr(review, "analyze_specification", lambda *a, **k: "")
        monkeypatch.setattr(review, "analyze_file_changes_with_context",
                            lambda *a, **k: (fr, review._format_file_review(fr, "Foo.lean")))

        # The fake provider is only reached by the verification pass here; it
        # refutes every finding it is shown. (**kwargs tolerates the tools/
        # tool_runner arguments passed when Lean tools are enabled.)
        def gen(model, contents, schema, thinking_budget=None, **kwargs):
            return review.FindingVerdict(verdict="refuted", reasoning="not actually an issue"), review.TokenUsage()
        monkeypatch.setattr(review, "create_provider",
                            lambda key, **kw: SimpleNamespace(name="fake", generate_structured=gen))
        monkeypatch.setenv("API_KEY", "sk-test")
        for var in ("SUMMARY_FILES", "LEAN_INFO", "DISCOVERED_FILES", "BUILD_OUTPUT", "LAKE_GRAPH"):
            monkeypatch.delenv(var, raising=False)

        out = self._run(monkeypatch, tmp_path, capsys)
        assert "filtered by verification" in out
        assert "Verdict (deterministic): Approved" in out


def _status_error(status, message="error"):
    """A REAL openai SDK status error (clean-room; no fixture copied from
    openai-python's test suite)."""
    import httpx
    from openai import APIStatusError
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return APIStatusError(message, response=httpx.Response(status, request=req), body=None)


class TestC3Containment:
    """C3 [2/2] acceptance + orchestration tests, driven through review.main() with a
    stub provider and the REAL ThreadPoolExecutor / containment path. Assertions target
    the rendered comment, the health flag, and the process exit code — never a leaf
    re-raise. The banner/exit assertions would FAIL on pre-C3 HEAD (which fails open)."""

    _SINGLE = TestMainOrchestration._SINGLE
    _TWO = TestMainOrchestration._TWO

    def _run(self, monkeypatch, tmp_path, capsys, extra_argv=(), env=None):
        argv = ["review.py", "--pr-number", "1", "--model", "anthropic/claude-haiku-4.5", *extra_argv]
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setenv("API_KEY", "sk-test")
        for var in ("SUMMARY_FILES", "LEAN_INFO", "DISCOVERED_FILES", "BUILD_OUTPUT",
                    "LAKE_GRAPH", "LLM_MAX_RUN_TOKENS", "LLM_MAX_RUN_COST", "LLM_LOUD_EXIT"):
            monkeypatch.delenv(var, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(review, "get_pr_diff", lambda pr: (self._TWO, []))
        monkeypatch.setattr(review, "create_provider", lambda key, **kw: SimpleNamespace(name="fake"))
        monkeypatch.setattr(review, "analyze_specification", lambda *a, **k: "")
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            rc = review.main()
        finally:
            os.chdir(cwd)
        out = capsys.readouterr().out
        health = None
        hp = tmp_path / "review_health.json"
        if hp.exists():
            health = json.loads(hp.read_text())
        return out, rc, health

    def test_all_hard_failures_are_loud_not_green(self, monkeypatch, tmp_path, capsys):
        # acceptance #3: every per-file call 402s → the run stays LOUD (banner + health
        # flag + non-Approved), NOT a silent green. Exit 0 without loud-exit.
        monkeypatch.setattr(review, "analyze_file_changes_with_context",
                            lambda *a, **k: (_ for _ in ()).throw(_status_error(402)))
        out, rc, health = self._run(monkeypatch, tmp_path, capsys)
        assert "did not complete normally" in out          # the fixed banner
        assert "Verdict (deterministic): Approved" not in out
        assert health is not None and health["degraded"] is True
        # Counted exactly once at the top-level containment (the leaf sites only
        # re-raise) — a single aborting failure must not inflate the telemetry.
        assert health["hard_failures"] == 1
        assert rc == 0                                      # loud-exit OFF by default

    def test_reference_manifest_lists_loaded_context_end_to_end(self, monkeypatch, tmp_path, capsys):
        # Integration: a real spec/KB file flows through the loader's records channel
        # into the rendered manifest (proves loader→render wiring, not just the renderer).
        spec = tmp_path / "kb_page.md"
        spec.write_text("# BCIKS20\nProximity gaps for Reed-Solomon codes.\n")
        fr = review.FileReview(verdict="Approved", analysis="fine")
        monkeypatch.setattr(review, "analyze_file_changes_with_context", lambda *a, **k: (fr, "FORMATTED::ok"))
        monkeypatch.setattr(review, "analyze_cross_file", lambda *a, **k: (None, ""))
        monkeypatch.setattr(review, "synthesize_overall_summary",
                            lambda *a, **k: (review.SynthesisSummary(tldr="t", precheck_summary="p", overall_verdict="Approved"), "S"))
        monkeypatch.setattr(review, "verify_findings", lambda *a, **k: [])
        # One real KB file (loads → manifest) + one missing (fails → Context Warnings).
        out, rc, health = self._run(
            monkeypatch, tmp_path, capsys,
            env={"SPEC_REFS": f"{spec},{tmp_path / 'missing_kb.md'}"})
        assert "References &amp; context used" in out
        assert "kb_page.md" in out                         # loaded → listed in the manifest
        assert "Knowledge base / specification" in out
        # Single source, no double-listing: the failed ref appears exactly once, in the
        # Context Warnings block — never also in the manifest.
        assert "Context Warnings" in out
        assert out.count("missing_kb.md") == 1
        assert rc == 0

    def test_synthesis_only_failure_cannot_render_approved(self, monkeypatch, tmp_path, capsys):
        # A hard failure confined to synthesis (which runs AFTER the verdict) must not
        # leave an "Approved" verdict standing under the CAUTION banner — the verdict is
        # re-derived once degraded flips post-verdict.
        fr = review.FileReview(verdict="Approved", analysis="fine")
        monkeypatch.setattr(review, "analyze_file_changes_with_context",
                            lambda *a, **k: (fr, "FORMATTED::ok"))
        monkeypatch.setattr(review, "analyze_cross_file", lambda *a, **k: (None, ""))
        monkeypatch.setattr(review, "verify_findings", lambda *a, **k: [])
        monkeypatch.setattr(review, "synthesize_overall_summary",
                            lambda *a, **k: (_ for _ in ()).throw(_status_error(402)))
        out, rc, health = self._run(monkeypatch, tmp_path, capsys)
        assert "did not complete normally" in out
        assert "Verdict (deterministic): Approved" not in out   # not Approved under the banner
        assert health["degraded"] is True and health["hard_failures"] == 1
        assert rc == 0

    def test_loud_exit_returns_nonzero_after_comment(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(review, "analyze_file_changes_with_context",
                            lambda *a, **k: (_ for _ in ()).throw(_status_error(402)))
        out, rc, health = self._run(monkeypatch, tmp_path, capsys, env={"LLM_LOUD_EXIT": "1"})
        assert "did not complete normally" in out          # comment STILL printed
        assert rc == review.LOUD_EXIT_CODE and rc != 0      # ...then a non-zero exit

    def test_budget_trip_partial_results_and_skip_marker(self, monkeypatch, tmp_path, capsys):
        # acceptance #1: file 1 completes and records; a later file trips the budget →
        # graceful degrade: partial result kept, skipped file listed, non-Approved, exit 0.
        fr = review.FileReview(verdict="Approved", analysis="fine")

        def fake_review(provider, ctx, fp, fd, fc, spec, ext, chk, vr):
            if fp == "Foo.lean":
                return fr, f"FORMATTED::{fp}"
            raise review.BudgetExceededError(usage=review.TokenUsage(input_tokens=10**9))
        monkeypatch.setattr(review, "analyze_file_changes_with_context", fake_review)
        out, rc, health = self._run(monkeypatch, tmp_path, capsys, extra_argv=["--max-workers", "1"])
        assert "FORMATTED::Foo.lean" in out                 # partial result preserved
        assert "did not complete normally" in out           # banner fires
        assert "Bar.lean" in out and "Skipped (per-run budget)" in out
        assert "Verdict (deterministic): Approved" not in out
        assert health["budget_exceeded"] is True and rc == 0

    def test_verify_budget_trip_forces_incomplete_R9(self, monkeypatch, tmp_path, capsys):
        # R9: per-file reviews all Approve, but a budget trip during verification (which
        # runs BEFORE the verdict) must flip review_incomplete so the run cannot Approve.
        fr = review.FileReview(verdict="Approved", analysis="fine")
        monkeypatch.setattr(review, "analyze_file_changes_with_context",
                            lambda *a, **k: (fr, "FORMATTED::ok"))
        monkeypatch.setattr(review, "analyze_cross_file", lambda *a, **k: (None, ""))
        monkeypatch.setattr(review, "synthesize_overall_summary",
                            lambda *a, **k: (review.SynthesisSummary(tldr="t", precheck_summary="p", overall_verdict="Approved"), "S"))
        monkeypatch.setattr(review, "verify_findings",
                            lambda *a, **k: (_ for _ in ()).throw(review.BudgetExceededError()))
        out, rc, health = self._run(monkeypatch, tmp_path, capsys)
        assert "Verdict (deterministic): Approved" not in out   # R9: outage can't Approve
        assert "did not complete normally" in out
        assert health["degraded"] is True and rc == 0

    def test_exception_body_not_leaked_R6(self, monkeypatch, tmp_path, capsys):
        # R6 invariant: a (non-fatal) exception body must not reach the PR comment.
        monkeypatch.setattr(review, "analyze_file_changes_with_context",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SENTINEL_LEAK_9999")))
        out, rc, health = self._run(monkeypatch, tmp_path, capsys)
        assert "SENTINEL_LEAK_9999" not in out
        # Contrast with the 402 case: a plain (non-spend/auth) error degrades GRACEFULLY
        # — review_errors flag it, but it is NOT a loud outage (no banner, health clean).
        assert "did not complete normally" not in out
        assert (health is None or health["degraded"] is False)
        assert rc == 0

    def test_clean_run_has_no_banner(self, monkeypatch, tmp_path, capsys):
        # No false alarm: a fully-successful run shows no banner and exits 0 even if
        # loud-exit is enabled.
        fr = review.FileReview(verdict="Approved", analysis="fine")
        monkeypatch.setattr(review, "analyze_file_changes_with_context",
                            lambda *a, **k: (fr, "FORMATTED::ok"))
        monkeypatch.setattr(review, "analyze_cross_file", lambda *a, **k: (None, ""))
        monkeypatch.setattr(review, "synthesize_overall_summary",
                            lambda *a, **k: (review.SynthesisSummary(tldr="t", precheck_summary="p", overall_verdict="Approved"), "S"))
        monkeypatch.setattr(review, "verify_findings", lambda *a, **k: [])
        out, rc, health = self._run(monkeypatch, tmp_path, capsys, env={"LLM_LOUD_EXIT": "1"})
        assert "did not complete normally" not in out
        assert (health is None or health["degraded"] is False) and rc == 0


class TestC3ActionWiring:
    """Static + cross-boundary checks on review/action.yml and the entrypoint. No CI
    exercises the action, so these guard the wiring: names matching Python constants,
    env-only threading (no expression-injection), the heredoc rc-capture, the Post
    Review condition, and the security drive-bys."""

    def _action(self):
        import yaml
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "action.yml")
        with open(p) as f:
            return yaml.safe_load(f), open(p).read()

    def _run_step(self, doc):
        return next(s for s in doc["runs"]["steps"] if s.get("id") == "run_review")

    def test_new_inputs_declared(self):
        doc, _ = self._action()
        for name in ("llm_max_run_tokens", "llm_max_run_cost", "llm_loud_exit"):
            assert name in doc["inputs"]

    def test_env_names_match_python_constants(self):
        # Cross-boundary: the env keys the action sets must be exactly the names
        # review.py reads — a LLM_MAX_RUN_TOKEN vs _TOKENS typo would ship it dark.
        doc, _ = self._action()
        env = self._run_step(doc)["env"]
        assert review.ENV_MAX_RUN_TOKENS in env
        assert review.ENV_MAX_RUN_COST in env
        assert review.ENV_LOUD_EXIT in env
        assert env[review.ENV_MAX_RUN_TOKENS] == "${{ inputs.llm_max_run_tokens }}"

    def test_budget_inputs_never_interpolated_into_run_body(self):
        # Expression-injection guard: the budget inputs must reach Python via env only,
        # never spliced into a run: script.
        doc, _ = self._action()
        for s in doc["runs"]["steps"]:
            run = s.get("run", "")
            assert "inputs.llm_max_run" not in run
            assert "inputs.llm_loud_exit" not in run

    def test_run_step_captures_rc_and_closes_heredoc(self):
        doc, _ = self._action()
        run = self._run_step(doc)["run"]
        assert "|| rc=$?" in run                       # errexit must not abort early
        assert 'echo "$EOF_MARKER" >> $GITHUB_OUTPUT' in run
        assert "exit $rc" in run
        # rc-capture must come before the EOF echo, or review_text is unterminated.
        assert run.index("|| rc=$?") < run.index('echo "$EOF_MARKER"')

    def test_shell_emits_error_from_health_file(self):
        doc, _ = self._action()
        run = self._run_step(doc)["run"]
        assert "review_health.json" in run and "::error::" in run

    def test_post_review_condition_and_checkout_hardening(self):
        doc, raw = self._action()
        post = next(s for s in doc["runs"]["steps"] if s.get("name") == "Post Review")
        cond = post["if"]
        assert "!cancelled()" in cond and "outcome != 'skipped'" in cond
        checkout = next(s for s in doc["runs"]["steps"] if s.get("name") == "Checkout repository")
        assert checkout["with"]["persist-credentials"] is False
        assert any("add-mask" in s.get("run", "") for s in doc["runs"]["steps"])

    def test_s1_resolve_before_checkout_and_ref_threaded(self):
        # S1: the resolve step must run BEFORE checkout and the checkout must pin the
        # resolved head SHA (else issue_comment lands on base — the wrong-code bug).
        doc, _ = self._action()
        steps = doc["runs"]["steps"]
        names = [s.get("name") for s in steps]
        assert names.index("Resolve PR head SHA") < names.index("Checkout repository")
        resolve = next(s for s in steps if s.get("name") == "Resolve PR head SHA")
        assert resolve.get("id") == "resolve_head"
        checkout = next(s for s in steps if s.get("name") == "Checkout repository")
        assert checkout["with"]["ref"] == "${{ steps.resolve_head.outputs.head_sha }}"
        # S1 hardening from C3 must still hold on the same step.
        assert checkout["with"]["persist-credentials"] is False
        assert checkout["with"]["fetch-depth"] == 0

    def test_s1_diff_and_discovery_pinned_to_resolved_shas(self):
        doc, _ = self._action()
        run_env = self._run_step(doc)["env"]
        assert run_env.get("PR_HEAD_SHA") == "${{ steps.resolve_head.outputs.head_sha }}"
        assert run_env.get("PR_BASE_SHA") == "${{ steps.resolve_head.outputs.base_sha }}"
        discover = next(s for s in doc["runs"]["steps"] if s.get("id") == "discover_files")
        assert discover["env"].get("PR_HEAD_SHA") == "${{ steps.resolve_head.outputs.head_sha }}"

    def test_s1_resolve_step_routes_pr_number_via_env_not_run_body(self):
        # Expression-injection guard: pr_number must reach the resolve script via an env
        # var, never interpolated into the run: body (the file's own standard).
        doc, _ = self._action()
        resolve = next(s for s in doc["runs"]["steps"] if s.get("id") == "resolve_head")
        assert resolve["env"].get("PR_NUMBER") == "${{ inputs.pr_number }}"
        assert "${{ inputs.pr_number }}" not in resolve["run"]
        assert "inputs.pr_number" not in resolve["run"]

    def test_s1_post_review_anchor_threaded_not_reresolved(self):
        doc, _ = self._action()
        post = next(s for s in doc["runs"]["steps"] if s.get("name") == "Post Review")
        assert post["env"].get("REVIEW_HEAD_SHA") == "${{ steps.resolve_head.outputs.head_sha }}"
        assert "process.env.REVIEW_HEAD_SHA" in post["with"]["script"]

    def test_entrypoint_is_sys_exit_main(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "review.py")).read()
        assert "sys.exit(main())" in src

    def test_no_sys_exit_inside_any_finally(self):
        # sys.exit in a finally masks tracebacks and (in review/action.yml) would kill
        # the comment. Assert the AST has none in either tool.
        import ast
        for fn in ("review.py", "summary.py"):
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                fn.split(".")[0], fn)
            tree = ast.parse(open(path).read(), fn)
            for node in ast.walk(tree):
                if isinstance(node, ast.Try):
                    for stmt in node.finalbody:
                        for sub in ast.walk(stmt):
                            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                                    and sub.func.attr == "exit"
                                    and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "sys"):
                                raise AssertionError(f"sys.exit inside a finally in {fn}")


class TestS1ResolveScript:
    """resolve_pr_head.sh (S1) — behavioral: stub `gh` on PATH and drive the script,
    not a YAML string match. Covers the happy path and the fail-closed exits."""

    def _script(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resolve_pr_head.sh")

    def _run(self, tmp_path, gh_line, repo="o/r", pr="42", gh_exit=0):
        import subprocess
        binp = tmp_path / "bin"
        binp.mkdir(exist_ok=True)
        # Stub `gh` on PATH: echo the canned line, then exit with gh_exit so we can drive
        # the API-failure branch (the most security-critical fail-closed guarantee).
        (binp / "gh").write_text(f"#!/usr/bin/env bash\necho '{gh_line}'\nexit {gh_exit}\n")
        (binp / "gh").chmod(0o755)
        out = tmp_path / "gh_output.txt"
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}", GITHUB_OUTPUT=str(out), GH_TOKEN="x")
        proc = subprocess.run(["bash", self._script(), repo, pr], env=env,
                              capture_output=True, text=True)
        return proc.returncode, (out.read_text() if out.exists() else "")

    def test_happy_path_writes_both_shas(self, tmp_path):
        rc, output = self._run(tmp_path, "abc123head def456base")
        assert rc == 0
        assert "head_sha=abc123head" in output
        assert "base_sha=def456base" in output

    def test_fail_closed_empty_pr(self, tmp_path):
        rc, output = self._run(tmp_path, "h b", pr="   ")
        assert rc != 0
        assert "head_sha=" not in output          # never a partial/base fallback

    def test_fail_closed_null_sha(self, tmp_path):
        rc, output = self._run(tmp_path, "null null")
        assert rc != 0
        assert "head_sha=" not in output

    def test_fail_closed_on_gh_api_error(self, tmp_path):
        # The single most important S1 guarantee: a failing `gh api` (404/auth/network)
        # aborts under `set -euo pipefail` and NEVER falls through to a base checkout.
        rc, output = self._run(tmp_path, "anything at all", gh_exit=1)
        assert rc != 0
        assert "head_sha=" not in output and "base_sha=" not in output

    def test_fail_closed_on_partial_null_base(self, tmp_path):
        # A resolved head but a null base must still fail closed (both are validated
        # before anything is written).
        rc, output = self._run(tmp_path, "abc123head null")
        assert rc != 0
        assert "head_sha=" not in output

    def test_fail_closed_non_numeric_pr(self, tmp_path):
        # Defence-in-depth numeric guard: a path-separator-bearing pr never reaches gh.
        rc, output = self._run(tmp_path, "h b", pr="42/../../issues/1")
        assert rc != 0
        assert "head_sha=" not in output


class TestReviewLabelling:
    """Guided/unguided header + reviewed-at stamp (drops the old Initial/subsequent axis)."""

    def test_unguided_header(self, monkeypatch):
        monkeypatch.delenv("PR_HEAD_SHA", raising=False)
        h = review._review_comment_header("")
        assert h.startswith("### 🤖 AI Review\n")
        assert "with additional instructions" not in h
        assert "Unguided review" in h

    def test_guided_header_lays_out_instructions(self, monkeypatch):
        monkeypatch.delenv("PR_HEAD_SHA", raising=False)
        h = review._review_comment_header("Check commutativity.\nAlso the base case.")
        assert "### 🤖 AI Review (with additional instructions)" in h
        assert "> Check commutativity." in h
        assert "> Also the base case." in h         # multi-line instructions blockquoted
        assert "Unguided review" not in h

    def test_reviewed_at_stamp_from_head_sha(self, monkeypatch):
        monkeypatch.setenv("PR_HEAD_SHA", "abcdef1234567890fedcba")
        h = review._review_comment_header("")
        assert "Reviewed at commit `abcdef123456`" in h   # short, 12 chars


class TestReferenceManifest:
    """The 'References & context used' manifest (deterministic render + load-status channel)."""

    def _rec(self, ref, cat, ok):
        return review.ContextRecord(ref, cat, ok)

    def test_render_groups_loaded_by_category(self):
        recs = [self._rec("https://eprint.iacr.org/2020/654", "external", True),
                self._rec("docs/kb/papers/BCIKS20.md", "spec", True),
                self._rec("ArkLib/Foo.lean", "repo", True),
                self._rec("missing.md", "spec", False)]
        out = review._render_reference_manifest(recs)
        assert "References &amp; context used" in out
        assert "eprint.iacr.org/2020/654" in out
        assert "docs/kb/papers/BCIKS20.md" in out
        assert "ArkLib/Foo.lean" in out
        # failures are NOT duplicated here — they live once in Context Warnings.
        assert "missing.md" not in out

    def test_empty_or_all_failed_renders_nothing(self):
        assert review._render_reference_manifest([]) == ""
        assert review._render_reference_manifest(None) == ""
        assert review._render_reference_manifest([self._rec("x", "spec", False)]) == ""

    def test_repo_list_is_capped(self):
        recs = [self._rec(f"f{i}.lean", "repo", True) for i in range(40)]
        out = review._render_reference_manifest(recs)
        assert f"and {40 - review._MANIFEST_REPO_CAP} more" in out

    def test_attacker_path_backticks_neutralised(self):
        out = review._render_reference_manifest([self._rec("evil`.lean", "repo", True)])
        assert "evil.lean" in out and "evil`.lean" not in out  # backtick stripped

    def test_safe_md_path_strips_backticks_and_newlines(self):
        # Both named injection vectors (backtick to escape the code span, newline to
        # start a new markdown line) must be neutralised.
        s = review._safe_md_path("a`b\nc\rd")
        assert "`" not in s and "\n" not in s and "\r" not in s
        assert s == "ab c d"

    def test_loader_records_additive_contract_preserved(self):
        # The records channel must not change the (parts, errors) return.
        recs = []
        assert review.get_local_reference_parts("", records=recs) == ([], [])
        assert recs == []
        assert review.get_document_content("", records=recs) == ([], [])


class TestPinnedDiff:
    """get_pr_diff / discovery pin to the resolved SHAs (S1) when present, else fall back."""

    def test_get_pr_diff_uses_git_when_pinned(self, monkeypatch):
        seen = {}
        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(stdout="diff --git a/A.lean b/A.lean\n@@ -1 +1 @@\n+x\n")
        monkeypatch.setattr(review.subprocess, "run", fake_run)
        monkeypatch.setenv("PR_BASE_SHA", "base1")
        monkeypatch.setenv("PR_HEAD_SHA", "head1")
        review.get_pr_diff("42")
        assert seen["cmd"][:2] == ["git", "diff"]
        assert "base1...head1" in seen["cmd"][2]

    def test_get_pr_diff_falls_back_to_gh_when_unpinned(self, monkeypatch):
        seen = {}
        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(stdout="diff --git a/A.lean b/A.lean\n@@ -1 +1 @@\n+x\n")
        monkeypatch.setattr(review.subprocess, "run", fake_run)
        monkeypatch.delenv("PR_BASE_SHA", raising=False)
        monkeypatch.delenv("PR_HEAD_SHA", raising=False)
        review.get_pr_diff("42")
        assert seen["cmd"][:3] == ["gh", "pr", "diff"]

    def test_discover_changed_files_pinned_to_shas(self, monkeypatch):
        # The discovery half of the "diff + discovery pinned to ONE SHA" S1 claim.
        import discover_files
        seen = {}
        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(stdout="ArkLib/A.lean\nREADME.md\n", returncode=0)
        monkeypatch.setattr(discover_files.subprocess, "run", fake_run)
        monkeypatch.setenv("PR_BASE_SHA", "b1")
        monkeypatch.setenv("PR_HEAD_SHA", "h1")
        files = discover_files.get_changed_lean_files("42")
        assert seen["cmd"][:3] == ["git", "diff", "--name-only"]
        assert "b1...h1" in seen["cmd"][3]
        assert files == ["ArkLib/A.lean"]          # only .lean kept

    def test_discover_changed_files_falls_back_when_unpinned(self, monkeypatch):
        import discover_files
        seen = {}
        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(stdout="ArkLib/A.lean\n", returncode=0)
        monkeypatch.setattr(discover_files.subprocess, "run", fake_run)
        monkeypatch.delenv("PR_BASE_SHA", raising=False)
        monkeypatch.delenv("PR_HEAD_SHA", raising=False)
        discover_files.get_changed_lean_files("42")
        assert seen["cmd"][:3] == ["gh", "pr", "diff"]


# --- extract_refs_from_instructions (freeform /review parsing) ---

class TestExtractRefsFromInstructions:
    def test_empty_and_plain_text(self):
        assert extract_refs_from_instructions("") == ([], [], [])
        urls, spec, repo = extract_refs_from_instructions(
            "Please double-check the commutativity hypothesis in the main theorem."
        )
        assert urls == [] and spec == [] and repo == []

    def test_extracts_urls(self):
        urls, _, _ = extract_refs_from_instructions(
            "Compare with https://arxiv.org/pdf/2301.12345.pdf and "
            "https://eprint.iacr.org/2024/001 please."
        )
        assert urls == [
            "https://arxiv.org/pdf/2301.12345.pdf",
            "https://eprint.iacr.org/2024/001",
        ]

    def test_url_trailing_punctuation_stripped(self):
        urls, _, _ = extract_refs_from_instructions(
            "See (https://example.com/paper.pdf), or https://example.com/spec."
        )
        assert urls == ["https://example.com/paper.pdf", "https://example.com/spec"]

    def test_duplicate_urls_deduped(self):
        urls, _, _ = extract_refs_from_instructions(
            "https://example.com/a and again https://example.com/a"
        )
        assert urls == ["https://example.com/a"]

    def test_existing_repo_paths_extracted(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "spec.md").write_text("spec")
        (tmp_path / "Foo.lean").write_text("theorem foo : True := trivial")
        monkeypatch.chdir(tmp_path)
        _, spec, repo = extract_refs_from_instructions(
            "Check Foo.lean against docs/spec.md and docs/missing.md."
        )
        assert spec == []
        # order follows mention order; the missing path is dropped
        assert repo == ["Foo.lean", "docs/spec.md"]

    def test_pdf_and_tex_routed_to_spec(self, tmp_path, monkeypatch):
        (tmp_path / "references").mkdir()
        (tmp_path / "references" / "paper.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "blueprint.tex").write_text("\\section{Main}")
        monkeypatch.chdir(tmp_path)
        _, spec, repo = extract_refs_from_instructions(
            "The bound is from references/paper.pdf; see also blueprint.tex."
        )
        assert spec == ["references/paper.pdf", "blueprint.tex"]
        assert repo == []

    def test_directories_extracted_only_with_slash(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "kb").mkdir()
        (tmp_path / "docs" / "kb" / "note.md").write_text("note")
        monkeypatch.chdir(tmp_path)
        # bare word "docs" is prose, "docs/kb" is a path
        _, _, repo = extract_refs_from_instructions("Read the docs, especially docs/kb")
        assert repo == ["docs/kb"]

    def test_prose_word_matching_directory_not_extracted(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        monkeypatch.chdir(tmp_path)
        _, _, repo = extract_refs_from_instructions("Please update the docs after this.")
        assert repo == []

    def test_absolute_and_parent_paths_rejected(self, tmp_path, monkeypatch):
        (tmp_path / "inner").mkdir()
        (tmp_path / "secret.md").write_text("s")
        monkeypatch.chdir(tmp_path / "inner")
        _, spec, repo = extract_refs_from_instructions(
            f"Look at {tmp_path}/secret.md and ../secret.md and /etc/hostname"
        )
        assert spec == [] and repo == []

    def test_punctuation_around_paths(self, tmp_path, monkeypatch):
        (tmp_path / "CONTRIBUTING.md").write_text("rules")
        monkeypatch.chdir(tmp_path)
        _, _, repo = extract_refs_from_instructions(
            "Conventions: (see `CONTRIBUTING.md`, particularly naming)."
        )
        assert repo == ["CONTRIBUTING.md"]

    def test_sentence_period_after_path(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "spec.md").write_text("spec")
        monkeypatch.chdir(tmp_path)
        _, _, repo = extract_refs_from_instructions("Ground this in docs/spec.md.")
        assert repo == ["docs/spec.md"]

    def test_dot_slash_prefix_kept(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "spec.md").write_text("spec")
        monkeypatch.chdir(tmp_path)
        _, _, repo = extract_refs_from_instructions("see ./docs/spec.md")
        assert repo == ["./docs/spec.md"]

    def test_old_sectioned_format_still_yields_refs(self, tmp_path, monkeypatch):
        # The retired External:/Internal:/Comments: comment style degrades
        # gracefully: its URLs and paths are still picked up from the text.
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "spec.md").write_text("spec")
        monkeypatch.chdir(tmp_path)
        urls, _, repo = extract_refs_from_instructions(
            "External:\n- https://arxiv.org/pdf/2301.1.pdf\n"
            "Internal:\n- docs/spec.md\nComments:\nCheck section 4."
        )
        assert urls == ["https://arxiv.org/pdf/2301.1.pdf"]
        assert repo == ["docs/spec.md"]


class TestMergeCsv:
    def test_merge_into_empty(self):
        assert _merge_csv("", ["a", "b"]) == "a,b"

    def test_merge_dedupes(self):
        assert _merge_csv("a,b", ["b", "c"]) == "a,b,c"

    def test_merge_nothing(self):
        assert _merge_csv("a,b", []) == "a,b"
        assert _merge_csv("", []) == ""

    def test_existing_whitespace_normalized(self):
        assert _merge_csv(" a , b ", ["c"]) == "a,b,c"
