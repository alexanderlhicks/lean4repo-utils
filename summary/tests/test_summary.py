import base64
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


github_module = types.ModuleType("github")
github_module.Github = object
github_module.Auth = types.SimpleNamespace(Token=object)
sys.modules.setdefault("github", github_module)

pull_request_module = types.ModuleType("github.PullRequest")
pull_request_module.PullRequest = object
sys.modules.setdefault("github.PullRequest", pull_request_module)

repo_module = types.ModuleType("github.Repository")
repo_module.Repository = object
sys.modules.setdefault("github.Repository", repo_module)

import summary


class FakeComment:
    def __init__(self, body):
        self.body = body

    def edit(self, body):
        self.body = body


class FakePR:
    def __init__(self, comments):
        self._comments = comments

    def get_issue_comments(self):
        return self._comments


class FakeOrchestrationPR:
    """A PR fake that records created/edited comments and serves them back on
    subsequent get_issue_comments() calls (so a second run sees the cache)."""

    def __init__(self, title="feat: add thing", body="PR body"):
        self.title = title
        self.body = body
        self._comments = []
        self.created = []

    def get_issue_comments(self):
        return list(self._comments)

    def create_issue_comment(self, body):
        comment = FakeComment(body)
        self._comments.append(comment)
        self.created.append(body)
        return comment


class SummaryTests(unittest.TestCase):
    def test_truncate_file_diff_uses_hunk_boundary(self):
        file_diff = (
            "diff --git a/A.lean b/A.lean\n"
            "@@ -1,3 +1,3 @@\n"
            "-old\n"
            "+new\n"
            "@@ -20,3 +20,3 @@\n"
            "-old2\n"
            "+new2\n"
        )
        truncated, was_truncated = summary._truncate_file_diff(file_diff, max_chars=55)
        self.assertTrue(was_truncated)
        self.assertIn("@@ -1,3 +1,3 @@", truncated)
        self.assertNotIn("@@ -20,3 +20,3 @@", truncated)

    def test_triage_enforces_proof_signal_file_in_normal_mode(self):
        diff_by_file = {
            "Proof.lean": "diff --git a/Proof.lean b/Proof.lean\n+  sorry\n",
            "Docs.md": "diff --git a/Docs.md b/Docs.md\n+docs\n",
        }
        triage_response = summary._TriageSimple(summarize=["Docs.md"])
        with mock.patch.object(summary, "_call_llm", return_value=triage_response):
            selected, low = summary.triage_files(["Proof.lean", "Docs.md"], diff_by_file, "dummy-model")
        self.assertEqual(low, [])
        self.assertEqual(selected, ["Proof.lean", "Docs.md"])

    def test_triage_tiered_promotes_proof_signal_to_high(self):
        # Tiered mode triggers when |files| > LARGE_PR_FILE_THRESHOLD.
        file_paths = [f"Noise{i}.md" for i in range(50)] + ["Proof.lean"]
        diff_by_file = {fp: f"diff --git a/{fp} b/{fp}\n+x\n" for fp in file_paths}
        # Proof.lean contains a proof signal so should be force-promoted to high,
        # even though the triage agent puts it in `low`.
        diff_by_file["Proof.lean"] = "diff --git a/Proof.lean b/Proof.lean\n+  sorry\n"
        triage_response = summary._TriageTiered(
            high=["Noise0.md"], low=["Proof.lean", "Noise1.md"],
        )
        with mock.patch.object(summary, "_call_llm", return_value=triage_response):
            high, low = summary.triage_files(file_paths, diff_by_file, "dummy-model")
        self.assertIn("Proof.lean", high)
        self.assertNotIn("Proof.lean", low)

    def test_triage_filters_deterministic_noise_before_llm(self):
        diff_by_file = {
            "A.lean": "diff --git a/A.lean b/A.lean\n@@ -1 +1 @@\n+theorem t := by trivial\n",
            "pnpm-lock.yaml": "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n@@ -1 +1 @@\n+lock\n",
            "logo.png": "diff --git a/logo.png b/logo.png\n@@ -1 +1 @@\n+bin\n",
        }
        resp = summary._TriageSimple(summarize=["A.lean", "pnpm-lock.yaml"])
        with mock.patch.object(summary, "_call_llm", return_value=resp):
            selected, low = summary.triage_files(list(diff_by_file), diff_by_file, "m")
        # Noise is dropped deterministically even if the LLM tries to keep it.
        self.assertEqual(selected, ["A.lean"])
        self.assertNotIn("pnpm-lock.yaml", selected)
        self.assertNotIn("logo.png", selected)

    def test_triage_skips_llm_when_only_noise(self):
        diff_by_file = {
            "uv.lock": "diff --git a/uv.lock b/uv.lock\n@@ -1 +1 @@\n+x\n",
            "a.png": "diff --git a/a.png b/a.png\n@@ -1 +1 @@\n+x\n",
        }
        with mock.patch.object(summary, "_call_llm", side_effect=AssertionError("must not call LLM")):
            selected, low = summary.triage_files(list(diff_by_file), diff_by_file, "m")
        self.assertEqual((selected, low), ([], []))

    def test_triage_falls_back_to_all_files_on_provider_failure(self):
        diff_by_file = {
            "Proof.lean": "diff --git a/Proof.lean b/Proof.lean\n+  sorry\n",
            "Docs.md": "diff --git a/Docs.md b/Docs.md\n+docs\n",
        }
        with mock.patch.object(summary, "_call_llm", side_effect=RuntimeError("API down")):
            selected, low = summary.triage_files(["Proof.lean", "Docs.md"], diff_by_file, "dummy-model")
        self.assertEqual(low, [])
        self.assertEqual(set(selected), {"Proof.lean", "Docs.md"})

    def test_call_prose_unwraps_summary_field(self):
        """_call_prose wraps generate_structured with _ProseSummary and returns the summary string."""
        fake_provider = mock.Mock()
        fake_provider.generate_structured.return_value = (
            summary._ProseSummary(summary="hello world"),
            summary.TokenUsage(input_tokens=1, output_tokens=1),
        )
        original_provider = summary._provider
        try:
            summary._provider = fake_provider
            result = summary._call_prose("any prompt", "dummy-model")
        finally:
            summary._provider = original_provider
        self.assertEqual(result, "hello world")
        # Confirms the schema wiring — we should have asked for _ProseSummary.
        _, kwargs = fake_provider.generate_structured.call_args
        self.assertIs(kwargs["schema"], summary._ProseSummary)

    def test_analyzer_uses_source_lookup_for_body_only_sorry_change(self):
        old_source = "\n".join([
            "theorem bodyOnly : True := by",
            "  have h : True := by",
            "    trivial",
            "  exact h",
        ])
        new_source = "\n".join([
            "theorem bodyOnly : True := by",
            "  have h : True := by",
            "    sorry",
            "  exact h",
        ])
        diff = "\n".join([
            "diff --git a/Test.lean b/Test.lean",
            "@@ -3,1 +3,1 @@",
            "-    trivial",
            "+    sorry",
        ])

        def fake_load(path, revision=None):
            if revision:
                return old_source
            return new_source

        with mock.patch.object(summary, "_load_lean_source", side_effect=fake_load):
            analyzer = summary.DiffAnalyzer(["theorem"], base_revision="base").analyze(diff)

        self.assertEqual(len(analyzer.added_sorries), 1)
        self.assertEqual(analyzer.removed_sorries, [])
        self.assertEqual(analyzer.affected_sorries, [])
        self.assertIn("bodyOnly", analyzer.added_sorries[0]["header"])

    def test_sorry_in_block_comment_ignored_via_source_lookup(self):
        # The block comment opens on line 2, above the shown hunk. Diff-local
        # depth would reset to 0 at the hunk and treat line 3 as live code,
        # falsely counting the `sorry`. Source-backed detection knows line 3 is
        # inside the comment and ignores it.
        old_source = "\n".join([
            "theorem foo : True := by",
            "  /- note:",
            "  replace this todo with a real proof",
            "  -/",
            "  trivial",
        ])
        new_source = "\n".join([
            "theorem foo : True := by",
            "  /- note:",
            "  replace this sorry with a real proof",
            "  -/",
            "  trivial",
        ])
        diff = "\n".join([
            "diff --git a/T.lean b/T.lean",
            "@@ -3,1 +3,1 @@",
            "-  replace this todo with a real proof",
            "+  replace this sorry with a real proof",
        ])

        def fake_load(path, revision=None):
            return old_source if revision else new_source

        # The old code did source-based *declaration* lookup but diff-local
        # *comment* detection, so it would have attributed this `sorry` to
        # `foo`. Source-backed comment detection correctly ignores it.
        with mock.patch.object(summary, "_load_lean_source", side_effect=fake_load):
            analyzer = summary.DiffAnalyzer(["theorem"], base_revision="base").analyze(diff)
        self.assertEqual(analyzer.added_sorries, [])

    # ------------------------------------------------------------------
    # Provider wiring regression guards for the generate_structured refactor
    # ------------------------------------------------------------------

    def test_summarize_file_diff_substitutes_placeholders_and_returns_prose(self):
        captured = {}

        def fake(prompt, model_name, schema):
            captured["prompt"] = prompt
            captured["schema"] = schema
            return summary._ProseSummary(summary="file summary")

        with mock.patch.object(summary, "_call_llm", side_effect=fake):
            result = summary.summarize_file_diff(
                "X.lean",
                "+++diff+++",
                "model",
                "FILE={{FILE_PATH}} D={{FILE_DIFF}}",
            )

        self.assertEqual(result, "file summary")
        self.assertIn("FILE=X.lean", captured["prompt"])
        self.assertIn("D=+++diff+++", captured["prompt"])
        self.assertIs(captured["schema"], summary._ProseSummary)

    def test_synthesize_summary_empty_result_raises(self):
        empty = summary._ProseSummary(summary="")
        template = "T={{PR_TITLE}} B={{PR_BODY}} S={{PER_FILE_SUMMARIES}} H={{PR_TYPE_HINT}}"
        with mock.patch.object(summary, "_read_prompt_template", return_value=template), \
             mock.patch.object(summary, "_call_llm", return_value=empty):
            with self.assertRaises(RuntimeError):
                summary.synthesize_summary(["f1"], "model", "title", "body", "hint ")

    def test_synthesize_summary_substitutes_all_placeholders(self):
        captured = {}

        def fake(prompt, model_name, schema):
            captured["prompt"] = prompt
            return summary._ProseSummary(summary="synthesised")

        template = "T={{PR_TITLE}} B={{PR_BODY}} S={{PER_FILE_SUMMARIES}} H={{PR_TYPE_HINT}}"
        with mock.patch.object(summary, "_read_prompt_template", return_value=template), \
             mock.patch.object(summary, "_call_llm", side_effect=fake):
            result = summary.synthesize_summary(["f1", "f2"], "model", "title", "body", "hint ")

        self.assertEqual(result, "synthesised")
        self.assertIn("T=title", captured["prompt"])
        self.assertIn("B=body", captured["prompt"])
        self.assertIn("- f1", captured["prompt"])
        self.assertIn("- f2", captured["prompt"])
        self.assertIn("H=hint ", captured["prompt"])

    def test_apply_additional_instructions_returns_none_without_instructions(self):
        # The empty-instructions short-circuit must not hit the provider.
        with mock.patch.object(summary, "_call_llm", side_effect=AssertionError("provider must not be called")):
            result = summary.apply_additional_instructions("diff content", "", "model", "template")
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # DiffAnalyzer: quality signals and declaration tracking
    # ------------------------------------------------------------------

    def test_diff_analyzer_flags_quality_signals(self):
        diff = "\n".join([
            "diff --git a/Q.lean b/Q.lean",
            "@@ -1,1 +1,4 @@",
            " def existing : Nat := 1",
            "+theorem foo : True := by native_decide",
            "+theorem bar : False := by admit",
            "+#eval foo",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["theorem", "def"]).analyze(diff)
        signals = {w["signal"] for w in analyzer.warnings}
        self.assertEqual(signals, {"native_decide", "admit", "#eval"})

    def test_diff_analyzer_ignores_commented_quality_signals(self):
        diff = "\n".join([
            "diff --git a/Q.lean b/Q.lean",
            "@@ -1,1 +1,2 @@",
            " def foo := 1",
            "+-- TODO: replace with native_decide when proof is stable",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["def"]).analyze(diff)
        self.assertEqual(analyzer.warnings, [])

    def test_sorry_detection_uses_word_boundary(self):
        # `sorryAx` contains the substring "sorry" but is a distinct identifier;
        # it must not be reported as an added proof obligation.
        diff = "\n".join([
            "diff --git a/A.lean b/A.lean",
            "@@ -1,2 +1,3 @@",
            " theorem foo : True := by",
            "+  exact sorryAx _   -- uses sorryAx, not the sorry tactic",
            " done",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["theorem"]).analyze(diff)
        self.assertEqual(analyzer.added_sorries, [])

    def test_real_sorry_still_detected(self):
        diff = "\n".join([
            "diff --git a/A.lean b/A.lean",
            "@@ -1,2 +1,3 @@",
            " theorem foo : True := by",
            "+  sorry",
            " done",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["theorem"]).analyze(diff)
        self.assertEqual(len(analyzer.added_sorries), 1)

    def test_added_line_starting_with_plus_plus_is_counted(self):
        # A real added content line whose text starts with '++' must not be
        # mistaken for the '+++ b/...' file header and dropped from the stats.
        diff = "\n".join([
            "diff --git a/doc.md b/doc.md",
            "--- a/doc.md",
            "+++ b/doc.md",
            "@@ -1,2 +1,4 @@",
            " title",
            "+++",
            "+real added line",
            " end",
        ])
        analyzer = summary.DiffAnalyzer(["theorem"]).analyze(diff)
        self.assertEqual(analyzer.stats["lines_added"], 2)

    def test_count_diff_lines_skips_header_counts_content(self):
        file_diff = "\n".join([
            "diff --git a/doc.md b/doc.md",
            "--- a/doc.md",
            "+++ b/doc.md",
            "@@ -1,2 +1,3 @@",
            " ctx",
            "+++",
            "-old",
        ])
        self.assertEqual(summary._count_diff_lines(file_diff), (1, 1))

    def test_format_summary_sheds_content_to_fit_comment_limit(self):
        class FakeCache:
            def to_json(self):
                return "C" * 50_000

        display = [f"**f{i}**: {'y' * 400}" for i in range(400)]
        out = summary.format_summary(
            "ai summary",
            {"files_changed": 1, "lines_added": 1, "lines_removed": 0},
            [], [], [], [], [], [], [],
            display, "R" * 5_000, FakeCache(),
        )
        self.assertLessEqual(len(out), summary.MAX_COMMENT_CHARS)
        # Core sections survive; the regenerable cache is shed first.
        self.assertIn("Statistics", out)
        self.assertNotIn(summary.CACHE_IDENTIFIER, out)
        self.assertIn("omitted to fit", out)

    def test_format_summary_keeps_everything_when_small(self):
        out = summary.format_summary(
            "tiny summary",
            {"files_changed": 1, "lines_added": 1, "lines_removed": 0},
            [], [], [], [], [], [], [],
            ["**f**: small"], None, None,
        )
        self.assertLessEqual(len(out), summary.MAX_COMMENT_CHARS)
        self.assertIn("Per-File Summaries", out)
        self.assertNotIn("omitted to fit", out)

    def test_find_related_issue_requires_standalone_name(self):
        sorry_info = {"id": "h@Foo.lean", "file": "Foo.lean"}
        # 'h' appears only inside 'hash' (and the file path) — not a real mention.
        no_match = [types.SimpleNamespace(body="touches the hash table in Foo.lean", number=1)]
        self.assertIsNone(summary._find_related_issue(sorry_info, no_match))
        # Standalone 'h' plus the file path — a genuine mention.
        match = [types.SimpleNamespace(body="lemma h in Foo.lean is unproven", number=2)]
        self.assertEqual(summary._find_related_issue(sorry_info, match).number, 2)

    def test_reasoning_kwargs_maps_valid_levels(self):
        self.assertEqual(summary._reasoning_kwargs("high"), {"reasoning_default": {"effort": "high"}})
        self.assertEqual(summary._reasoning_kwargs(" Medium "), {"reasoning_default": {"effort": "medium"}})
        self.assertEqual(summary._reasoning_kwargs("low"), {"reasoning_default": {"effort": "low"}})

    def test_reasoning_kwargs_empty_or_invalid_is_model_default(self):
        self.assertEqual(summary._reasoning_kwargs(""), {})
        self.assertEqual(summary._reasoning_kwargs(None), {})
        self.assertEqual(summary._reasoning_kwargs("turbo"), {})

    def test_positive_int_parses_or_falls_back(self):
        self.assertEqual(summary._positive_int("120000", 60000), 120000)
        # Empty / missing / non-numeric / non-positive all fall back to default.
        self.assertEqual(summary._positive_int("", 60000), 60000)
        self.assertEqual(summary._positive_int(None, 60000), 60000)
        self.assertEqual(summary._positive_int("abc", 60000), 60000)
        self.assertEqual(summary._positive_int("0", 60000), 60000)
        self.assertEqual(summary._positive_int("-5", 60000), 60000)

    def test_find_related_issue_tracker_id_wins(self):
        sorry_info = {"id": "lemA@Foo.lean", "file": "Foo.lean"}
        issues = [types.SimpleNamespace(body="<!-- sorry-tracker-id: lemA@Foo.lean -->", number=7)]
        self.assertEqual(summary._find_related_issue(sorry_info, issues).number, 7)

    def test_diff_analyzer_tracks_added_and_removed_decls(self):
        diff = "\n".join([
            "diff --git a/X.lean b/X.lean",
            "@@ -1,2 +1,2 @@",
            " existing line",
            "-theorem oldThm : False := by skip",
            "+theorem newThm : True := trivial",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["theorem"]).analyze(diff)
        self.assertTrue(any("newThm" in s["header"] for s in analyzer.added_decls))
        self.assertTrue(any("oldThm" in s["header"] for s in analyzer.removed_decls))
        self.assertEqual(analyzer.affected_decls, [])

    def test_diff_analyzer_tracks_affected_decl_when_same_name_in_both(self):
        diff = "\n".join([
            "diff --git a/X.lean b/X.lean",
            "@@ -1,1 +1,1 @@",
            "-theorem sharedThm : Old := old",
            "+theorem sharedThm : New := new",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["theorem"]).analyze(diff)
        self.assertEqual(analyzer.added_decls, [])
        self.assertEqual(analyzer.removed_decls, [])
        self.assertEqual(len(analyzer.affected_decls), 1)
        self.assertEqual(analyzer.affected_decls[0]["file"], "X.lean")

    # ------------------------------------------------------------------
    # SummaryCache
    # ------------------------------------------------------------------

    def _make_cache_comment(self, fingerprint, cache_payload):
        """Build a comment body in the shape SummaryCache expects (base64 cache)."""
        encoded = base64.b64encode(json.dumps(cache_payload).encode("utf-8")).decode("ascii")
        body = "### 🤖 PR Summary\n\n"
        body += summary.COMMENT_IDENTIFIER.replace("{{timestamp}}", "2026-04-18-T") + "\n\n"
        body += f"{summary.CACHE_IDENTIFIER}{encoded}-->\n\n"
        return FakePR([FakeComment(body)])

    def test_summary_cache_returns_entry_when_fingerprint_matches(self):
        fp = "fingerprint-abc"
        payload = {"File.lean": {"hash": "h1", "summary": "cached summary"}, "_config": fp}
        pr = self._make_cache_comment(fp, payload)
        cache = summary.SummaryCache(pr, fp)
        self.assertEqual(cache.get("File.lean", "h1"), "cached summary")
        # Hash mismatch → miss.
        self.assertIsNone(cache.get("File.lean", "h2"))

    def test_summary_cache_invalidates_on_stale_fingerprint(self):
        payload = {"File.lean": {"hash": "h1", "summary": "x"}, "_config": "old-fp"}
        pr = self._make_cache_comment("old-fp", payload)
        cache = summary.SummaryCache(pr, "new-fp")
        self.assertIsNone(cache.get("File.lean", "h1"))

    # ------------------------------------------------------------------
    # Pure helpers: validate_pr_title, split_diff_into_files, _detect_proof_signals
    # ------------------------------------------------------------------

    def test_validate_pr_title_valid_plain(self):
        is_valid, t, msg = summary.validate_pr_title("feat: add X")
        self.assertTrue(is_valid)
        self.assertEqual(t, "feat")
        self.assertIsNone(msg)

    def test_validate_pr_title_valid_with_scope(self):
        is_valid, t, msg = summary.validate_pr_title("fix(auth): handle null token")
        self.assertTrue(is_valid)
        self.assertEqual(t, "fix")
        self.assertIsNone(msg)

    def test_validate_pr_title_invalid_format(self):
        is_valid, t, msg = summary.validate_pr_title("Add feature X")
        self.assertFalse(is_valid)
        self.assertIsNone(t)
        self.assertIn("conventional commit", msg)

    def test_validate_pr_title_empty_is_accepted(self):
        # No title supplied → nothing to validate; short-circuit to True.
        is_valid, t, msg = summary.validate_pr_title("")
        self.assertTrue(is_valid)
        self.assertIsNone(t)
        self.assertIsNone(msg)

    def test_split_diff_into_files_multi_file(self):
        diff = "\n".join([
            "diff --git a/A.lean b/A.lean",
            "--- a/A.lean",
            "+++ b/A.lean",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            "diff --git a/B.lean b/B.lean",
            "--- a/B.lean",
            "+++ b/B.lean",
            "@@ -1 +1 @@",
            "-old2",
            "+new2",
        ])
        result = summary.split_diff_into_files(diff)
        self.assertIn("A.lean", result)
        self.assertIn("B.lean", result)
        self.assertIn("-old\n+new", result["A.lean"])
        self.assertIn("-old2\n+new2", result["B.lean"])

    def test_split_diff_into_files_empty(self):
        self.assertEqual(summary.split_diff_into_files(""), {})

    def test_decls_section_deterministic_grouped_and_capped(self):
        added = [{"file": f"F{i % 3}.lean", "header": f"theorem t{i}"} for i in range(10)]
        out = summary._format_decls_section(added, [], [])
        # Order-independent (underlying data is set-derived).
        self.assertEqual(out, summary._format_decls_section(list(reversed(added)), [], []))
        # Grouped by file, each file appears once as a sub-header.
        self.assertEqual(out.count("`F0.lean`"), 1)
        self.assertIn("`F1.lean`", out)

        many = [{"file": "A.lean", "header": f"def d{i}"} for i in range(200)]
        capped = summary._format_decls_section(many, [], [])
        self.assertIn("more not listed", capped)
        self.assertLessEqual(capped.count("*   `def"), summary.MAX_LISTED_DECLS)

    def test_cache_prune_drops_files_absent_from_diff(self):
        pr = self._make_cache_comment(
            "fp",
            {"_config": "fp",
             "keep.lean": {"hash": "h", "summary": "s"},
             "stale.lean": {"hash": "h", "summary": "s"}},
        )
        cache = summary.SummaryCache(pr, "fp")
        cache.prune(["keep.lean", "other.lean"])
        decoded = json.loads(base64.b64decode(cache.to_json()).decode("utf-8"))
        self.assertIn("keep.lean", decoded)
        self.assertNotIn("stale.lean", decoded)
        self.assertEqual(decoded.get("_config"), "fp")

    def test_config_fingerprint_changes_with_model_and_prompt(self):
        fp1 = summary._compute_config_fingerprint("model-a", "prompt-a")
        fp2 = summary._compute_config_fingerprint("model-b", "prompt-a")
        fp3 = summary._compute_config_fingerprint("model-a", "prompt-b")
        self.assertNotEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)
        self.assertEqual(fp1, summary._compute_config_fingerprint("model-a", "prompt-a"))

    def test_format_sorry_delta(self):
        self.assertEqual(summary._format_sorry_delta([], []), "")
        self.assertIn("delta: -1", summary._format_sorry_delta(["a"], ["b", "c"]))
        self.assertIn("delta: +2", summary._format_sorry_delta(["a", "b"], []))
        self.assertIn("delta: 0", summary._format_sorry_delta(["a"], ["b"]))

    def test_all_prompt_templates_load_non_empty(self):
        for name in ("triage.md", "triage_tiered.md", "summarize_file.md",
                     "additional_instructions.md", "synthesize_summary.md"):
            self.assertTrue(summary._read_prompt_template(name).strip(), f"{name} is empty")

    def test_detect_proof_signals_each_keyword(self):
        for keyword in ("sorry", "admit", "native_decide"):
            diff = f"diff --git a/X.lean b/X.lean\n+  {keyword}\n"
            self.assertEqual(summary._detect_proof_signals(diff), {keyword})

    def test_detect_proof_signals_all_keywords_combined(self):
        diff = "\n".join([
            "diff --git a/X.lean b/X.lean",
            "+  sorry",
            "-  admit",
            "+  native_decide",
        ])
        self.assertEqual(
            summary._detect_proof_signals(diff),
            {"sorry", "admit", "native_decide"},
        )

    def test_detect_proof_signals_ignores_context_lines(self):
        # Context lines (leading space, no +/-) must not register a match.
        diff = "diff --git a/X.lean b/X.lean\n  sorry on a context line\n"
        self.assertEqual(summary._detect_proof_signals(diff), set())

    def test_detect_proof_signals_ignores_diff_headers(self):
        # `+++` and `---` are file-header markers, not added/removed content.
        diff = "+++ b/sorry.lean\n--- a/sorry.lean\n def real := 1\n"
        self.assertEqual(summary._detect_proof_signals(diff), set())

    # ------------------------------------------------------------------
    # Cache corruption regression + truncation fallback
    # ------------------------------------------------------------------

    def test_cache_round_trips_summary_containing_comment_terminator(self):
        """A summary containing '-->' must survive a write/read cycle.

        Regression: the cache used to embed raw JSON and split on '-->', so a
        '-->' inside any summary truncated the JSON and wiped the whole cache."""
        cache = summary.SummaryCache(FakePR([]), "fp-1")
        dangerous = "Defines the map `A --> B` and proves it <--> mono."
        cache.update("Map.lean", "h1", dangerous)

        # Embed exactly as format_summary does, then read back.
        body = (
            summary.COMMENT_IDENTIFIER.replace("{{timestamp}}", "ts") + "\n\n"
            + f"{summary.CACHE_IDENTIFIER}{cache.to_json()}-->\n\nrest of comment"
        )
        reloaded = summary.SummaryCache(FakePR([FakeComment(body)]), "fp-1")
        self.assertEqual(reloaded.get("Map.lean", "h1"), dangerous)

    def test_truncate_single_hunk_falls_back_to_newline_not_midline(self):
        # One hunk, body far over budget: must cut at a newline (no half line),
        # and must NOT collapse to just the header by cutting at the lone hunk.
        file_diff = (
            "diff --git a/Big.lean b/Big.lean\n"
            "@@ -1,1 +1,400 @@\n"
            + "".join(f"+line {i} aaaaaaaaaaaaaaaaaaaaaaaaaaaa\n" for i in range(400))
        )
        truncated, was_truncated = summary._truncate_file_diff(file_diff, max_chars=500)
        self.assertTrue(was_truncated)
        self.assertTrue(truncated.endswith("\n"))           # never a mid-line cut
        self.assertIn("@@ -1,1 +1,400 @@", truncated)       # body retained, not just header
        self.assertIn("+line 0", truncated)

    # ------------------------------------------------------------------
    # Full mocked main() orchestration
    # ------------------------------------------------------------------

    def _run_main(self, diff, pr, repo, summarize_side_effect):
        env = {
            "API_KEY": "k",
            "GITHUB_TOKEN": "t",
            "GITHUB_REPOSITORY": "owner/repo",
            "PR_NUMBER": "1",
            "INPUT_MODEL": "anthropic/claude-haiku-4.5",
        }
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "pr.diff"), "w") as f:
                f.write(diff)
            cwd = os.getcwd()
            os.chdir(td)
            try:
                with mock.patch.dict(os.environ, env, clear=False), \
                     mock.patch.object(summary, "create_provider",
                                       return_value=types.SimpleNamespace(name="fake")), \
                     mock.patch.object(summary, "get_github_objects", return_value=(repo, pr)), \
                     mock.patch.object(summary, "find_sorry_issues", return_value=[]), \
                     mock.patch.object(summary, "triage_files",
                                       return_value=(["Big.lean"], ["Small.lean"])), \
                     mock.patch.object(summary, "summarize_file_diff",
                                       side_effect=summarize_side_effect) as m_sum, \
                     mock.patch.object(summary, "synthesize_summary", return_value="FINAL AI SUMMARY"):
                    summary.main()
                    return m_sum.call_count
            finally:
                os.chdir(cwd)

    def test_main_orchestration_end_to_end_and_cache_hit(self):
        big = (
            "diff --git a/Big.lean b/Big.lean\n@@ -1,1 +1,1 @@\n"
            + "".join(f"+line {i} padding padding padding padding padding\n" for i in range(1500))
        )
        small = "diff --git a/Small.lean b/Small.lean\n@@ -1,1 +1,1 @@\n+y\n"
        diff = big + small
        pr = FakeOrchestrationPR()
        repo = object()

        summarize_calls = {"n": 0}

        def fake_summarize(fp, fd, model, tmpl):
            summarize_calls["n"] += 1
            return "summarized: defines A --> B"   # contains the dangerous terminator

        # First run: summarizes the one high-priority file and posts a comment.
        n_calls_1 = self._run_main(diff, pr, repo, fake_summarize)
        self.assertEqual(n_calls_1, 1)               # only Big.lean (Small is low-priority)
        self.assertEqual(len(pr.created), 1)
        posted = pr.created[0]
        self.assertIn("FINAL AI SUMMARY", posted)
        self.assertIn("Big.lean", posted)            # truncation coverage note + summary
        self.assertIn("Small.lean", posted)          # low-priority brief mention
        self.assertIn("minor changes", posted)

        # The cache survived the '-->' in the summary and is recoverable.
        payload = posted.split(summary.CACHE_IDENTIFIER, 1)[1].split("-->", 1)[0].strip()
        decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
        self.assertIn("Big.lean", decoded)
        self.assertIn("-->", decoded["Big.lean"]["summary"])

        # Second run with identical diff: the cached comment is now present, so
        # the summarizer must NOT be called again for Big.lean.
        n_calls_2 = self._run_main(diff, pr, repo, fake_summarize)
        self.assertEqual(n_calls_2, 0)               # pure cache hit

    def test_main_notes_files_filtered_as_noise(self):
        # A file present in the diff but in neither triage tier must still be
        # mentioned, so the file count reconciles and nothing is invisible.
        diff = (
            "diff --git a/Big.lean b/Big.lean\n@@ -1,1 +1,1 @@\n+a\n"
            "diff --git a/Small.lean b/Small.lean\n@@ -1,1 +1,1 @@\n+b\n"
            "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n@@ -1,1 +1,1 @@\n+lock\n"
        )
        pr = FakeOrchestrationPR()
        self._run_main(diff, pr, object(), lambda *a: "sum")
        posted = pr.created[0]
        self.assertIn("filtered as noise", posted)
        self.assertIn("pnpm-lock.yaml", posted)


if __name__ == "__main__":
    unittest.main()
