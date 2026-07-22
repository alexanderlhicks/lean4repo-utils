"""Unit tests for lean_info_extractor.py core functions."""

import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lean_info_extractor import (
    get_lean_declarations,
    get_module_name,
    extract_axioms,
    extract_sorry_warnings,
    extract_diagnostics,
    extract_info_for_files,
    extract_light_info,
    format_for_review,
    main,
)
import lean_info_extractor


class TestGetLeanDeclarations:
    def test_basic_declarations(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("""
import Mathlib

def myDef (n : Nat) : Nat := n + 1

theorem myTheorem : True := trivial

lemma myLemma : 1 = 1 := rfl

structure MyStruct where
  x : Nat

noncomputable def myNonComp := Classical.choice ⟨0⟩
""")
        decls = get_lean_declarations(str(lean_file))
        assert "myDef" in decls
        assert "myTheorem" in decls
        assert "myLemma" in decls
        assert "MyStruct" in decls
        assert "myNonComp" in decls

    def test_empty_file(self, tmp_path):
        lean_file = tmp_path / "Empty.lean"
        lean_file.write_text("")
        assert get_lean_declarations(str(lean_file)) == []

    def test_nonexistent_file(self):
        assert get_lean_declarations("/nonexistent/file.lean") == []

    def test_ignores_declarations_inside_multiline_strings(self, tmp_path):
        lean_file = tmp_path / "Strings.lean"
        lean_file.write_text('def text := "first line\ntheorem fake : True := trivial"\ntheorem real : True := trivial\n')
        decls = get_lean_declarations(str(lean_file))
        assert "fake" not in decls
        assert "real" in decls

    def test_namespace_block_qualifies_names(self, tmp_path):
        lean_file = tmp_path / "Ns.lean"
        lean_file.write_text(
            "namespace Foo\n"
            "def bar := 1\n"
            "namespace Baz\n"
            "theorem qux : True := trivial\n"
            "end Baz\n"
            "def after := 2\n"
            "end Foo\n"
            "def top := 3\n"
        )
        assert get_lean_declarations(str(lean_file)) == [
            "Foo.bar", "Foo.Baz.qux", "Foo.after", "top",
        ]

    def test_dotted_namespace_closed_by_dotted_end(self, tmp_path):
        lean_file = tmp_path / "Dotted.lean"
        lean_file.write_text(
            "namespace A.B\n"
            "def x := 1\n"
            "end A.B\n"
            "def y := 2\n"
        )
        assert get_lean_declarations(str(lean_file)) == ["A.B.x", "y"]

    def test_sections_and_mutual_do_not_qualify_but_consume_end(self, tmp_path):
        lean_file = tmp_path / "Sec.lean"
        lean_file.write_text(
            "namespace Ns\n"
            "noncomputable section\n"
            "def a := 1\n"
            "end\n"
            "section Named\n"
            "def b := 2\n"
            "end Named\n"
            "mutual\n"
            "def c := 3\n"
            "end\n"
            "def d := 4\n"
            "end Ns\n"
        )
        assert get_lean_declarations(str(lean_file)) == [
            "Ns.a", "Ns.b", "Ns.c", "Ns.d",
        ]

    def test_inline_dotted_name_and_root_anchor(self, tmp_path):
        lean_file = tmp_path / "Inline.lean"
        lean_file.write_text(
            "namespace Ns\n"
            "def Sub.item := 1\n"
            "theorem _root_.Nat.mine : True := trivial\n"
            "end Ns\n"
        )
        assert get_lean_declarations(str(lean_file)) == ["Ns.Sub.item", "Nat.mine"]

    def test_inline_attributes_do_not_hide_declarations(self, tmp_path):
        lean_file = tmp_path / "Attrs.lean"
        lean_file.write_text(
            "namespace Ns\n"
            "@[simp] theorem coeff_add : True := trivial\n"
            "@[inline, aesop safe apply] protected def helper := 1\n"
            "end Ns\n"
        )

        assert get_lean_declarations(str(lean_file)) == [
            "Ns.coeff_add", "Ns.helper",
        ]

    def test_private_and_anonymous_instance_skipped(self, tmp_path):
        lean_file = tmp_path / "Priv.lean"
        lean_file.write_text(
            "namespace Ns\n"
            "private def hidden := 1\n"
            "instance : Inhabited Nat := ⟨0⟩\n"
            "instance named : Inhabited Int := ⟨0⟩\n"
            "protected def visible := 2\n"
            "end Ns\n"
        )
        assert get_lean_declarations(str(lean_file)) == ["Ns.named", "Ns.visible"]

    def test_namespace_keyword_in_comment_or_string_ignored(self, tmp_path):
        lean_file = tmp_path / "Fake.lean"
        lean_file.write_text(
            "-- namespace Fake\n"
            '/- namespace AlsoFake -/\n'
            'def s := "namespace StringFake"\n'
            "def x := 1\n"
        )
        assert get_lean_declarations(str(lean_file)) == ["s", "x"]


class TestGetModuleName:
    def test_with_src_prefix(self, tmp_path, monkeypatch):
        # Without lakefile, falls back to heuristic
        monkeypatch.chdir(tmp_path)
        assert get_module_name("src/Foo/Bar.lean") == "Foo.Bar"

    def test_with_lakefile_toml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "lakefile.toml").write_text('srcDir = "ArkLib"\n')
        assert get_module_name("ArkLib/Crypto/Hash.lean") == "Crypto.Hash"


class TestExtractSorryWarnings:
    def test_finds_sorry(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        locs = extract_sorry_warnings(str(lean_file))
        assert len(locs) == 1
        assert ":1" in locs[0]

    def test_ignores_comments(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("-- sorry\ndef foo := 1\n")
        locs = extract_sorry_warnings(str(lean_file))
        assert len(locs) == 0


class TestConfinedChangedFiles:
    def test_full_extractor_rejects_symlinked_changed_file(self, tmp_path, monkeypatch):
        outside = tmp_path.parent / "outside-lean-info.lean"
        outside.write_text("theorem leaked : True := sorry\n")
        (tmp_path / "Leak.lean").symlink_to(outside)
        monkeypatch.chdir(tmp_path)

        info = extract_info_for_files(["Leak.lean"])

        assert info["files"] == {}
        assert info["sorry_locations"] == []
        assert info["errors"] and "unsafe changed path" in info["errors"][0]

    def test_ignores_nested_block_comments(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("/- outer\n  /- inner sorry -/\n  still outer\n-/\ndef foo := 1\n")
        locs = extract_sorry_warnings(str(lean_file))
        assert len(locs) == 0

    def test_finds_sorry_after_block_comment(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("/- comment -/\ntheorem foo : True := sorry\n")
        locs = extract_sorry_warnings(str(lean_file))
        assert len(locs) == 1
        assert ":2" in locs[0]

    def test_empty_file(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("")
        assert extract_sorry_warnings(str(lean_file)) == []

    def test_ignores_sorry_inside_multiline_string(self, tmp_path):
        lean_file = tmp_path / "Strings.lean"
        lean_file.write_text('def text := "first line\nsecond line says sorry"\n')
        assert extract_sorry_warnings(str(lean_file)) == []


class TestExtractAxioms:
    """Parser tests for #print axioms output (run_lean_command is mocked).

    Declarations are namespace-qualified names (FQNs); the module name is used
    only for the import. Queries must use the FQN verbatim — never the
    module-path-prefixed form, which #print axioms cannot resolve.
    """

    def _patch_output(self, monkeypatch, output):
        calls = []

        def fake(mod, cmd, timeout=30):
            calls.append((mod, cmd, timeout))
            return output

        monkeypatch.setattr(lean_info_extractor, "run_lean_command", fake)
        return calls

    def test_query_uses_fqn_verbatim(self, monkeypatch):
        calls = self._patch_output(
            monkeypatch, "'Ns.bar' does not depend on any axioms"
        )
        # Module path (Crypto.Hash) differs from the namespace (Ns): the query
        # must NOT be prefixed with the module path.
        extract_axioms("Crypto.Hash", ["Ns.bar"])
        assert calls == [("Crypto.Hash", "#print axioms Ns.bar", 30)]

    def test_standard_axioms_only(self, monkeypatch):
        self._patch_output(
            monkeypatch,
            "'Foo.bar' depends on axioms: [propext, Classical.choice, Quot.sound]",
        )
        result, errors = extract_axioms("Foo", ["Foo.bar"])
        # The three standard axioms must be parsed as separate names so the
        # downstream non-standard filter sees nothing flagworthy.
        assert result == {"Foo.bar": ["propext", "Classical.choice", "Quot.sound"]}
        assert errors == []

    def test_non_standard_axiom_detected(self, monkeypatch):
        self._patch_output(
            monkeypatch,
            "'Foo.bar' depends on axioms: [propext, sorryAx, Lean.ofReduceBool]",
        )
        result, errors = extract_axioms("Foo", ["Foo.bar"])
        assert result == {"Foo.bar": ["propext", "sorryAx", "Lean.ofReduceBool"]}
        assert errors == []

    def test_no_axioms(self, monkeypatch):
        self._patch_output(monkeypatch, "'Foo.bar' does not depend on any axioms")
        assert extract_axioms("Foo", ["Foo.bar"]) == ({"Foo.bar": []}, [])

    def test_wrapped_list_across_lines(self, monkeypatch):
        self._patch_output(
            monkeypatch,
            "'Foo.bar' depends on axioms: [propext,\n  Classical.choice,\n  Quot.sound]",
        )
        result, _ = extract_axioms("Foo", ["Foo.bar"])
        assert result == {"Foo.bar": ["propext", "Classical.choice", "Quot.sound"]}

    def test_unrecognized_output_recorded_as_error(self, monkeypatch):
        # e.g. declaration not found — the error text must not become an axiom,
        # and the failure must be visible, not silently dropped.
        self._patch_output(monkeypatch, "unknown identifier 'Foo.bar'")
        result, errors = extract_axioms("Foo", ["Foo.bar"])
        assert result == {}
        assert len(errors) == 1
        assert "Foo.bar" in errors[0] and "unknown identifier" in errors[0]

    def test_none_output_recorded_as_error(self, monkeypatch):
        self._patch_output(monkeypatch, None)
        result, errors = extract_axioms("Foo", ["Foo.bar"])
        assert result == {}
        assert len(errors) == 1
        assert "failed" in errors[0]

    def test_deadline_exhaustion_truncates_with_error(self, monkeypatch):
        self._patch_output(monkeypatch, "'X' does not depend on any axioms")
        # Deadline already passed: nothing may run, truncation must be recorded.
        result, errors = extract_axioms(
            "Foo", ["Foo.a", "Foo.b"], deadline=lean_info_extractor._time.monotonic() - 1
        )
        assert result == {}
        assert len(errors) == 1
        assert "budget exhausted" in errors[0] and "2 remaining" in errors[0]

    def test_deadline_caps_per_call_timeout(self, monkeypatch):
        calls = self._patch_output(monkeypatch, "'Foo.a' does not depend on any axioms")
        extract_axioms(
            "Foo", ["Foo.a"], deadline=lean_info_extractor._time.monotonic() + 5
        )
        assert len(calls) == 1
        assert calls[0][2] <= 5  # per-subprocess timeout sized to remaining budget


class TestFormatForReview:
    def test_no_issues(self):
        info = {"sorry_locations": [], "axiom_summary": {}, "files": {}, "errors": []}
        result = format_for_review(info)
        assert "No issues detected" in result

    def test_with_sorry(self):
        info = {
            "sorry_locations": ["Foo.lean:42"],
            "axiom_summary": {},
            "files": {},
            "errors": []
        }
        result = format_for_review(info)
        assert "Foo.lean:42" in result
        assert "sorry" in result.lower() or "Incomplete" in result

    def test_with_diagnostics(self):
        info = {
            "sorry_locations": [],
            "axiom_summary": {},
            "files": {},
            "diagnostics": ["Foo.lean:10: warning: unused variable"],
            "errors": []
        }
        result = format_for_review(info)
        assert "Foo.lean:10" in result

    def test_with_axioms(self):
        info = {
            "sorry_locations": [],
            "axiom_summary": {"Foo.myAxiom": ["myCustomAxiom"]},
            "files": {},
            "errors": []
        }
        result = format_for_review(info)
        assert "myCustomAxiom" in result


class TestExtractDiagnostics:
    def test_returns_empty_when_lake_not_available(self):
        """extract_diagnostics should not crash when lake is not available."""
        result = extract_diagnostics("/nonexistent/file.lean", timeout=5)
        assert isinstance(result, list)

    def test_returns_list(self, tmp_path):
        """Should always return a list (possibly empty)."""
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("def foo := 1\n")
        result = extract_diagnostics(str(lean_file), timeout=5)
        assert isinstance(result, list)


class TestExtractInfoForFiles:
    @pytest.fixture(autouse=True)
    def _checkout_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def test_skips_non_lean(self, tmp_path):
        md_file = tmp_path / "README.md"
        md_file.write_text("hello")
        result = extract_info_for_files([str(md_file)], time_budget=10)
        assert result["files"] == {}

    def test_skips_nonexistent(self):
        result = extract_info_for_files(["/nonexistent/file.lean"], time_budget=10)
        assert result["files"] == {}

    def test_basic_lean_file(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("def foo := 1\ntheorem bar : True := sorry\n")
        result = extract_info_for_files([str(lean_file)], time_budget=10)
        assert str(lean_file) in result["files"]
        file_info = result["files"][str(lean_file)]
        assert "foo" in file_info["declarations"]
        assert "bar" in file_info["declarations"]
        # Should detect sorry
        assert any("sorry" in loc or str(lean_file) in loc for loc in result["sorry_locations"])

    def test_time_budget_exceeded(self, tmp_path):
        """Should report error when time budget is 0."""
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("def foo := 1\n")
        result = extract_info_for_files([str(lean_file)], time_budget=0)
        assert len(result["errors"]) > 0
        assert "Time budget" in result["errors"][0]

    def test_axiom_summary_keyed_by_fqn(self, tmp_path, monkeypatch):
        """End-to-end: namespace decl → FQN query → FQN-keyed axiom summary."""
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text(
            "namespace Ns\ntheorem thm : True := trivial\nend Ns\n"
        )
        queries = []

        def fake(mod, cmd, timeout=30):
            queries.append(cmd)
            return "'Ns.thm' depends on axioms: [myCustomAxiom]"

        monkeypatch.setattr(lean_info_extractor, "run_lean_command", fake)
        monkeypatch.setattr(
            lean_info_extractor, "get_module_name", lambda fp: "Some.Module"
        )
        monkeypatch.setattr(
            lean_info_extractor, "extract_diagnostics", lambda fp, timeout=60: []
        )
        result = extract_info_for_files([str(lean_file)], time_budget=300)
        assert queries == ["#print axioms Ns.thm"]
        assert result["axiom_summary"] == {"Ns.thm": ["myCustomAxiom"]}

    def test_axiom_errors_surfaced_and_bounded(self, tmp_path, monkeypatch):
        """Per-declaration extraction failures reach results['errors'], capped."""
        decls = "\n".join(f"def d{i} := 1" for i in range(10))
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text(decls + "\n")
        monkeypatch.setattr(
            lean_info_extractor,
            "run_lean_command",
            lambda mod, cmd, timeout=30: "unknown constant",
        )
        monkeypatch.setattr(
            lean_info_extractor, "get_module_name", lambda fp: "Some.Module"
        )
        monkeypatch.setattr(
            lean_info_extractor, "extract_diagnostics", lambda fp, timeout=60: []
        )
        result = extract_info_for_files([str(lean_file)], time_budget=300)
        # 10 failures → 5 shown + 1 "and N more" marker, not silence, not spam.
        assert len(result["errors"]) == 6
        assert "and 5 more" in result["errors"][-1]


class TestExtractLightInfo:
    @pytest.fixture(autouse=True)
    def _checkout_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def test_finds_sorry_in_summary_files(self, tmp_path):
        lean_file = tmp_path / "Summary.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        result = extract_light_info([str(lean_file)])
        assert len(result["sorry_locations"]) == 1
        assert result["files_scanned"] == 1

    def test_no_sorry(self, tmp_path):
        lean_file = tmp_path / "Clean.lean"
        lean_file.write_text("def foo := 1\n")
        result = extract_light_info([str(lean_file)])
        assert result["sorry_locations"] == []
        assert result["files_scanned"] == 1

    def test_skips_non_lean(self, tmp_path):
        md_file = tmp_path / "README.md"
        md_file.write_text("sorry\n")
        result = extract_light_info([str(md_file)])
        assert result["files_scanned"] == 0

    def test_handles_nonexistent(self):
        result = extract_light_info(["/nonexistent/file.lean"])
        assert result["files_scanned"] == 0


class TestGitHubOutputFormatting:
    def test_multiline_outputs_use_heredoc_syntax(self, tmp_path, monkeypatch, capsys):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        github_output = tmp_path / "github_output.txt"

        monkeypatch.setenv("CHANGED_FILES", str(lean_file))
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.delenv("SUMMARY_FILES", raising=False)
        monkeypatch.setattr(sys, "argv", ["lean_info_extractor.py"])

        main()

        output_text = github_output.read_text()
        assert "lean_info_json<<" in output_text
        assert "lean_info_formatted<<" in output_text
        assert '"sorry_locations": [' in output_text


class TestScrubbedEnv:
    def test_secret_variables_removed_benign_kept(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "sk-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
        monkeypatch.setenv("GH_TOKEN", "gho_secret")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
        monkeypatch.setenv("MY_DEPLOY_TOKEN", "tok")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/u")
        env = lean_info_extractor.scrubbed_env()
        for secret in ("API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "OPENROUTER_API_KEY", "MY_DEPLOY_TOKEN"):
            assert secret not in env, secret
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/u"

    def test_run_lean_command_child_env_scrubbed_by_default(self, monkeypatch):
        # D3 regression: attacker-influenced Lean elaboration in the secret-
        # bearing run-review step must never see API_KEY in its environment.
        monkeypatch.setenv("API_KEY", "sk-secret")
        captured = {}

        class FakeResult:
            stdout = "ok"
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return FakeResult()

        monkeypatch.setattr(lean_info_extractor.subprocess, "run", fake_run)
        out = lean_info_extractor.run_lean_command("Foo", "#print axioms Foo.x")
        assert out == "ok"
        assert captured["env"] is not None
        assert "API_KEY" not in captured["env"]
        assert "PATH" in captured["env"]

    def test_explicit_env_honored(self, monkeypatch):
        captured = {}

        class FakeResult:
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return FakeResult()

        monkeypatch.setattr(lean_info_extractor.subprocess, "run", fake_run)
        lean_info_extractor.run_lean_command("Foo", "#check 1", env={"PATH": "/x"})
        assert captured["env"] == {"PATH": "/x"}
