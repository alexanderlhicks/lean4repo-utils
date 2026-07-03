"""Tests for detection.py: comment/string-aware sorry detection, declaration
attribution, and stable-id dedup."""

import os

import detection


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestFindAllSorries:
    def _scan(self, tmp_path, content, name="Foo.lean"):
        _write(str(tmp_path / name), content)
        return detection.find_all_sorries(str(tmp_path))

    def test_finds_sorry_with_decl_name(self, tmp_path):
        res = self._scan(tmp_path, "theorem foo : True := by\n  sorry\n")
        assert len(res) == 1
        assert res[0].decl_name == "foo"
        assert res[0].line_num == 2

    def test_detects_admit(self, tmp_path):
        res = self._scan(tmp_path, "theorem foo : True := by\n  admit\n")
        assert len(res) == 1
        assert res[0].decl_name == "foo"

    def test_ignores_line_comment(self, tmp_path):
        res = self._scan(tmp_path, "def foo := 1 -- TODO: sorry later\n")
        assert res == []

    def test_ignores_block_comment(self, tmp_path):
        res = self._scan(tmp_path, "/- this sorry is in a comment -/\ndef foo := 1\n")
        assert res == []

    def test_ignores_nested_block_comment(self, tmp_path):
        res = self._scan(
            tmp_path,
            "/- outer\n  /- inner sorry -/\n still outer\n-/\ndef foo := 1\n",
        )
        assert res == []

    def test_does_not_match_sorryax(self, tmp_path):
        # `sorryAx` is the underlying axiom, not a proof obligation.
        res = self._scan(tmp_path, "theorem foo : True := sorryAx _\n")
        assert res == []

    def test_ignores_sorry_in_string_literal(self, tmp_path):
        # Soundness: a `sorry` inside a string is not a proof obligation.
        res = self._scan(tmp_path, 'def msg : String := "TODO: sorry not done"\n')
        assert res == []

    def test_ignores_sorry_in_multiline_string(self, tmp_path):
        res = self._scan(tmp_path, 'def msg : String := "line one\nstill sorry here"\n')
        assert res == []

    def test_ignores_primed_identifier(self, tmp_path):
        # `sorry'` is a distinct identifier, not the `sorry` keyword.
        res = self._scan(tmp_path, "def sorry' : Nat := 1\n")
        assert res == []

    def test_names_partial_def(self, tmp_path):
        res = self._scan(tmp_path, "partial def foo : Nat := by\n  sorry\n")
        assert len(res) == 1 and res[0].decl_name == "foo"

    def test_names_attributed_decl(self, tmp_path):
        res = self._scan(tmp_path, "@[simp] lemma bar : True := by\n  sorry\n")
        assert len(res) == 1 and res[0].decl_name == "bar"

    def test_names_indented_decl(self, tmp_path):
        # Declarations inside a `mutual` block are indented.
        res = self._scan(tmp_path, "mutual\n  theorem qux : True := by\n    sorry\nend\n")
        assert len(res) == 1 and res[0].decl_name == "qux"

    def test_dedups_multiple_sorries_in_one_decl(self, tmp_path):
        content = "theorem foo : True ∧ True := by\n  refine ⟨?_, ?_⟩\n  · sorry\n  · sorry\n"
        res = self._scan(tmp_path, content)
        assert len(res) == 1  # one obligation for the declaration, not two

    def test_unnamed_sorries_kept_separate(self, tmp_path):
        # Two sorries with no enclosing declaration get distinct line-based ids.
        content = "#eval sorry\n#eval sorry\n"
        res = self._scan(tmp_path, content)
        assert len(res) == 2

    def test_skips_lake_directory(self, tmp_path):
        _write(str(tmp_path / ".lake" / "packages" / "x" / "Dep.lean"),
               "theorem dep : True := sorry\n")
        _write(str(tmp_path / "Real.lean"), "theorem real : True := sorry\n")
        res = detection.find_all_sorries(str(tmp_path))
        names = {r.decl_name for r in res}
        assert names == {"real"}


class TestStableId:
    def test_named_decl_uses_name(self):
        assert detection._stable_id("foo", "A.lean", 10) == "foo@A.lean"

    def test_unnamed_uses_line(self):
        assert detection._stable_id("", "A.lean", 10) == "L10@A.lean"
