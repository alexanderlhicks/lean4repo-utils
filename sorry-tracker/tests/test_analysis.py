"""Tests for analysis.py: import-context gathering, budgets, and prompt assembly."""

import analysis
from detection import Sorry
from leanrepo_common.lean_utils import import_search_dirs


class TestFindAndReadImports:
    def test_reads_resolved_import(self, tmp_path):
        (tmp_path / "Dep").mkdir()
        (tmp_path / "Dep" / "Mod.lean").write_text("theorem helper : True := trivial\n")
        out = analysis.find_and_read_imports("import Dep.Mod\n", import_search_dirs(str(tmp_path)))
        assert "Content from: Dep.Mod" in out
        assert "helper" in out

    def test_skips_oversized_import(self, tmp_path):
        (tmp_path / "Big.lean").write_text("x" * (analysis.MAX_IMPORT_FILE_SIZE + 10))
        out = analysis.find_and_read_imports("import Big\n", import_search_dirs(str(tmp_path)))
        assert out == ""

    def test_missing_import_is_skipped(self, tmp_path):
        out = analysis.find_and_read_imports("import No.Such.Module\n", import_search_dirs(str(tmp_path)))
        assert out == ""

    def test_total_import_budget_caps_context(self, tmp_path, monkeypatch):
        # Three imports, each under the per-file cap but together over the total.
        monkeypatch.setattr(analysis, "MAX_TOTAL_IMPORT_BYTES", 1500)
        body = "x" * 1000
        for name in ("A", "B", "C"):
            (tmp_path / f"{name}.lean").write_text(body)
        out = analysis.find_and_read_imports(
            "import A\nimport B\nimport C\n", import_search_dirs(str(tmp_path))
        )
        # First fits, second would exceed 1500 -> stop. Only A included.
        assert "Content from: A" in out
        assert "Content from: B" not in out
        assert "Content from: C" not in out


class TestImportContext:
    def test_caches_per_file(self, tmp_path):
        (tmp_path / "Dep.lean").write_text("theorem helper : True := trivial\n")
        ctx = analysis.ImportContext(str(tmp_path))
        calls = []
        # Wrap find_and_read_imports to count resolutions.
        orig = analysis.find_and_read_imports
        analysis.find_and_read_imports = lambda *a, **k: calls.append(1) or orig(*a, **k)
        try:
            a = ctx.for_file("F.lean", "import Dep\n")
            b = ctx.for_file("F.lean", "import Dep\n")
        finally:
            analysis.find_and_read_imports = orig
        assert a == b and "helper" in a
        assert len(calls) == 1  # second lookup served from cache


class TestBuildPrompt:
    def _sorry(self):
        return Sorry(
            file_path="A.lean", line_num=5, decl_name="foo",
            snippet="theorem foo := by sorry", full_content="theorem foo := by sorry",
            stable_id="foo@A.lean",
        )

    def test_includes_location_and_snippet(self):
        prompt = analysis._build_prompt(self._sorry(), "", has_references=False)
        assert "**Location:** A.lean:5" in prompt
        assert "theorem foo := by sorry" in prompt
        assert "External Reference Content" not in prompt

    def test_reference_section_toggles(self):
        prompt = analysis._build_prompt(self._sorry(), "", has_references=True)
        assert "External Reference Content" in prompt
