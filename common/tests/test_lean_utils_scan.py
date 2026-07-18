"""Tests for lean_utils: comment-aware scanning and import resolution."""

import os

from leanrepo_common.lean_utils import (
    is_in_comment,
    strip_comments,
    strip_comments_preserve_strings,
    scrub_line,
    detect_src_dir,
    import_search_dirs,
    resolve_import,
)


class TestScrubLine:
    def test_drops_string_content(self):
        code, depth, in_str = scrub_line('def m := "a sorry b"', 0, False)
        assert "sorry" not in code
        assert depth == 0 and in_str is False

    def test_drops_trailing_comment(self):
        code, _, _ = scrub_line("def f := 1 -- sorry later", 0, False)
        assert "sorry" not in code

    def test_block_comment_and_string_both_removed(self):
        code, depth, _ = scrub_line('def f := /- sorry -/ "sorry"', 0, False)
        assert "sorry" not in code and depth == 0

    def test_multiline_string_stays_open(self):
        # An unterminated string keeps in_string True so the next line, which
        # mentions sorry inside the same literal, is also suppressed.
        _, _, in_str = scrub_line('def m := "line one', 0, False)
        assert in_str is True
        code, _, in_str = scrub_line('still string with sorry"', 0, True)
        assert "sorry" not in code and in_str is False

    def test_escaped_quote_does_not_close_string(self):
        _, _, in_str = scrub_line(r'def m := "he said \"sorry\"', 0, False)
        assert in_str is True

    def test_real_code_after_string_is_kept(self):
        code, _, _ = scrub_line('def x := "msg"; sorry', 0, False)
        assert "sorry" in code


class TestStripComments:
    def test_plain_code_unchanged(self):
        code, depth = strip_comments("def foo := 1", 0)
        assert code == "def foo := 1"
        assert depth == 0

    def test_trailing_line_comment_removed(self):
        code, depth = strip_comments("def foo := 1 -- mentions sorry", 0)
        assert "sorry" not in code
        assert depth == 0

    def test_full_line_comment_empty(self):
        code, _ = strip_comments("  -- sorry here", 0)
        assert code.strip() == ""

    def test_block_comment_inline(self):
        code, depth = strip_comments("def f := /- sorry -/ 1", 0)
        assert "sorry" not in code
        assert depth == 0

    def test_unterminated_block_tracks_depth(self):
        code, depth = strip_comments("def f := 1 /- open sorry", 0)
        assert "sorry" not in code
        assert depth == 1

    def test_string_preserved(self):
        code, depth = strip_comments('def f := "a -- sorry b"', 0)
        assert code.strip() == 'def f := "a -- sorry b"'
        assert depth == 0

    def test_multiline_string_state_is_preserved(self):
        code, depth, in_str = strip_comments_preserve_strings('def m := "line one', 0, False)
        assert code == 'def m := "line one'
        assert depth == 0 and in_str is True
        code, depth, in_str = strip_comments_preserve_strings('still -- text" -- comment', depth, in_str)
        assert code == 'still -- text" '
        assert depth == 0 and in_str is False


class TestIsInComment:
    def test_line_comment(self):
        flag, depth = is_in_comment("  -- comment", 0)
        assert flag is True and depth == 0

    def test_code(self):
        flag, depth = is_in_comment("def f := 1", 0)
        assert flag is False and depth == 0

    def test_nested_open(self):
        flag, depth = is_in_comment("/- a /- b", 0)
        assert flag is True and depth == 2


class TestDetectSrcDir:
    def test_toml(self, tmp_path):
        (tmp_path / "lakefile.toml").write_text('srcDir = "ArkLib"\n')
        assert detect_src_dir(str(tmp_path)) == "ArkLib"

    def test_lean(self, tmp_path):
        (tmp_path / "lakefile.lean").write_text('  srcDir := "src/lib"\n')
        assert detect_src_dir(str(tmp_path)) == "src/lib"

    def test_none(self, tmp_path):
        assert detect_src_dir(str(tmp_path)) is None


class TestImportResolution:
    def _make_repo(self, tmp_path):
        # Project module under srcDir "MyLib".
        (tmp_path / "lakefile.toml").write_text('srcDir = "MyLib"\n')
        (tmp_path / "MyLib" / "Core").mkdir(parents=True)
        (tmp_path / "MyLib" / "Core" / "Basic.lean").write_text("-- basic\n")
        # A dependency whose import root (ProofWidgets) differs from its
        # lowercase package directory name (proofwidgets) — the case the old
        # capitalize() heuristic got wrong.
        pkg = tmp_path / ".lake" / "packages" / "proofwidgets" / "ProofWidgets"
        pkg.mkdir(parents=True)
        (pkg / "Widget.lean").write_text("-- widget\n")
        # Mathlib-style: module path lives directly under the package dir.
        ml = tmp_path / ".lake" / "packages" / "mathlib" / "Mathlib" / "Data"
        ml.mkdir(parents=True)
        (ml / "Nat.lean").write_text("-- nat\n")
        return str(tmp_path)

    def test_resolves_project_module_via_srcdir(self, tmp_path):
        root = self._make_repo(tmp_path)
        dirs = import_search_dirs(root)
        path = resolve_import("MyLib.Core.Basic", dirs)
        assert path is not None and path.endswith(os.path.join("MyLib", "Core", "Basic.lean"))

    def test_resolves_dependency_with_mismatched_dir_name(self, tmp_path):
        root = self._make_repo(tmp_path)
        dirs = import_search_dirs(root)
        path = resolve_import("ProofWidgets.Widget", dirs)
        assert path is not None and path.endswith(os.path.join("ProofWidgets", "Widget.lean"))

    def test_resolves_mathlib_module(self, tmp_path):
        root = self._make_repo(tmp_path)
        dirs = import_search_dirs(root)
        path = resolve_import("Mathlib.Data.Nat", dirs)
        assert path is not None and path.endswith(os.path.join("Mathlib", "Data", "Nat.lean"))

    def test_unresolvable_returns_none(self, tmp_path):
        root = self._make_repo(tmp_path)
        dirs = import_search_dirs(root)
        assert resolve_import("Does.Not.Exist", dirs) is None

    def test_empty_import_returns_none(self, tmp_path):
        assert resolve_import("", import_search_dirs(str(tmp_path))) is None
