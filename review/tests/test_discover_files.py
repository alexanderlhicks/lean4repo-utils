"""Unit tests for discover_files.py core functions."""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discover_files
from discover_files import (
    get_lean_module_name,
    get_dependent_lean_files,
    get_dependency_lean_files,
    get_transitive_dependencies,
    convert_module_to_file_path,
    build_lean_file_index,
    partition_context_tiers,
)


class TestGetLeanModuleName:
    def test_src_prefix(self):
        assert get_lean_module_name("src/My/Module.lean") == "My.Module"

    def test_mathlib_prefix(self):
        assert get_lean_module_name("Mathlib/Algebra/Ring.lean") == "Algebra.Ring"

    def test_lib_prefix(self):
        assert get_lean_module_name("lib/Foo/Bar.lean") == "Foo.Bar"

    def test_no_prefix(self):
        assert get_lean_module_name("Foo/Bar/Baz.lean") == "Foo.Bar.Baz"

    def test_single_file(self):
        assert get_lean_module_name("Main.lean") == "Main"


class TestGetDependentLeanFiles:
    def test_basic_dependency(self):
        graph = [
            {"name": "A", "imports": ["B"]},
            {"name": "B", "imports": []},
            {"name": "C", "imports": ["A"]},
        ]
        # If B changed, A depends on B
        result = get_dependent_lean_files({"B"}, graph)
        assert "A" in result
        assert "C" not in result  # C depends on A, not B directly

    def test_no_dependents(self):
        graph = [
            {"name": "A", "imports": []},
            {"name": "B", "imports": []},
        ]
        result = get_dependent_lean_files({"A"}, graph)
        assert result == []


class TestGetDependencyLeanFiles:
    def test_basic(self):
        graph = [
            {"name": "A", "imports": ["B", "C"]},
            {"name": "B", "imports": []},
            {"name": "C", "imports": []},
        ]
        result = get_dependency_lean_files({"A"}, graph)
        assert "B" in result
        assert "C" in result

    def test_excludes_changed(self):
        graph = [
            {"name": "A", "imports": ["B"]},
            {"name": "B", "imports": []},
        ]
        # Both A and B changed — B should not be in dependencies
        result = get_dependency_lean_files({"A", "B"}, graph)
        assert "B" not in result


class TestConvertModuleToFilePath:
    def test_basic(self):
        index = ["src/Foo/Bar.lean", "src/Baz.lean"]
        assert convert_module_to_file_path("Foo.Bar", index) == "src/Foo/Bar.lean"

    def test_fallback(self):
        index = []
        # When not found, returns heuristic path
        result = convert_module_to_file_path("Foo.Bar", index)
        assert result.endswith("Foo" + os.sep + "Bar.lean")


class TestTransitiveDependencies:
    GRAPH = [
        {"name": "A", "imports": ["B", "C"]},
        {"name": "B", "imports": ["D"]},
        {"name": "C", "imports": ["D", "E"]},
        {"name": "D", "imports": ["F"]},
        {"name": "E", "imports": []},
        {"name": "F", "imports": []},
    ]

    def test_depth_1_matches_direct(self):
        """At depth 1, should match get_dependency_lean_files behavior."""
        result = get_transitive_dependencies({"A"}, self.GRAPH, max_depth=1)
        assert set(result.keys()) == {"B", "C"}
        assert all(d == 1 for d in result.values())

    def test_depth_2_finds_imports_of_imports(self):
        """At depth 2, should also find D and E (imports of B and C)."""
        result = get_transitive_dependencies({"A"}, self.GRAPH, max_depth=2)
        assert "B" in result and result["B"] == 1
        assert "C" in result and result["C"] == 1
        assert "D" in result and result["D"] == 2
        assert "E" in result and result["E"] == 2
        assert "F" not in result  # depth 3

    def test_depth_3_finds_deeper(self):
        result = get_transitive_dependencies({"A"}, self.GRAPH, max_depth=3)
        assert "F" in result and result["F"] == 3

    def test_excludes_changed_modules(self):
        """Changed modules should not appear in the result."""
        result = get_transitive_dependencies({"A", "B"}, self.GRAPH, max_depth=2)
        assert "A" not in result
        assert "B" not in result
        assert "C" in result  # direct import of A
        assert "D" in result  # import of C (depth 2 from A, or depth 1 from B — but B is changed)

    def test_cycle_handling(self):
        """Cycles should not cause infinite loops."""
        cyclic_graph = [
            {"name": "A", "imports": ["B"]},
            {"name": "B", "imports": ["C"]},
            {"name": "C", "imports": ["A"]},  # cycle back to A
        ]
        result = get_transitive_dependencies({"A"}, cyclic_graph, max_depth=5)
        assert "B" in result and result["B"] == 1
        assert "C" in result and result["C"] == 2
        # A is in changed_modules, so it's excluded

    def test_empty_graph(self):
        result = get_transitive_dependencies({"A"}, [], max_depth=2)
        assert result == {}

    def test_depth_tags_are_correct(self):
        """Each module should be tagged with its minimum depth."""
        result = get_transitive_dependencies({"A"}, self.GRAPH, max_depth=3)
        # D is reachable at depth 2 (A->B->D or A->C->D)
        assert result["D"] == 2
        # F is reachable at depth 3 (A->B->D->F or A->C->D->F)
        assert result["F"] == 3


class TestBuildLeanFileIndex:
    def test_finds_lean_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "Foo.lean").write_text("def foo := 1")
        (tmp_path / "src" / "Bar.lean").write_text("def bar := 1")
        (tmp_path / "README.md").write_text("hello")
        index = build_lean_file_index()
        assert any("Foo.lean" in f for f in index)
        assert any("Bar.lean" in f for f in index)
        assert not any("README.md" in f for f in index)

    def test_skips_git_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        git_dir = tmp_path / ".git" / "objects"
        git_dir.mkdir(parents=True)
        (git_dir / "Fake.lean").write_text("-- not a real lean file")
        (tmp_path / "Real.lean").write_text("def real := 1")
        index = build_lean_file_index()
        assert not any(".git" in f for f in index)
        assert any("Real.lean" in f for f in index)

    def test_skips_lake_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        lake_dir = tmp_path / ".lake" / "packages"
        lake_dir.mkdir(parents=True)
        (lake_dir / "Dep.lean").write_text("-- dependency")
        (tmp_path / "Main.lean").write_text("def main := 1")
        index = build_lean_file_index()
        assert not any(".lake" in f for f in index)
        assert any("Main.lean" in f for f in index)

    def test_empty_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        index = build_lean_file_index()
        assert index == []


class TestPartitionContextTiers:
    def test_changed_preferred_then_deps(self):
        final = ["A.lean", "B.lean", "C.lean", "D.lean"]
        changed = {"B.lean", "D.lean"}
        depths = {"A.lean": 1, "C.lean": 2}
        full, summary = partition_context_tiers(final, changed, depths, context_limit=10)
        # Changed files first (ordered by their appearance in final), then
        # others sorted by depth then name.
        assert full[:2] == ["B.lean", "D.lean"]
        assert full[2:] == ["A.lean", "C.lean"]
        assert summary == []

    def test_depth_ordering_within_non_changed(self):
        final = ["A.lean", "B.lean", "C.lean", "D.lean"]
        changed = set()
        depths = {"A.lean": 2, "B.lean": 1, "C.lean": 2, "D.lean": 1}
        full, _ = partition_context_tiers(final, changed, depths, context_limit=10)
        # depth-1 before depth-2, tie-broken alphabetically.
        assert full == ["B.lean", "D.lean", "A.lean", "C.lean"]

    def test_hard_cap_demotes_overflow_deps(self):
        final = [f"F{i}.lean" for i in range(10)]
        changed = {"F0.lean", "F1.lean"}
        depths = {f"F{i}.lean": 1 for i in range(2, 10)}
        full, summary = partition_context_tiers(final, changed, depths, context_limit=5)
        assert len(full) == 5
        assert full[:2] == ["F0.lean", "F1.lean"]
        assert len(summary) == 5  # the 5 overflow deps

    def test_hard_cap_demotes_overflow_changed_files(self):
        """If there are more changed files than CONTEXT_LIMIT, the excess
        changed files should also fall through to summary — previously they
        would escape the cap and blow the prompt budget."""
        final = [f"C{i}.lean" for i in range(8)]
        changed = set(final)  # all 8 are changed
        full, summary = partition_context_tiers(final, changed, {}, context_limit=5)
        assert len(full) == 5
        assert len(summary) == 3
        # All 8 still accounted for, order preserved.
        assert full + summary == final

    def test_negative_context_limit_is_clamped(self):
        full, summary = partition_context_tiers(["A.lean", "B.lean"], set(), {}, -1)
        assert full == []
        assert summary == ["A.lean", "B.lean"]


class TestBuildImportGraph:
    """The graph is built by scanning source `import` lines — NOT
    `lake exe graph`, which has no JSON mode in the importGraph the target
    repos pin, needs a built package, and only covers the default target."""

    def _write(self, tmp_path, rel, content):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def _graph(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        idx = discover_files.build_lean_file_index()
        graph = discover_files.build_import_graph(idx)
        return {g["name"]: g["imports"] for g in graph}

    def test_scans_imports_restricted_to_local_modules(self, tmp_path, monkeypatch):
        self._write(tmp_path, "MyLib/A.lean",
                    "import Mathlib.Data.Set.Basic\nimport MyLib.B\n\ndef a := 1\n")
        self._write(tmp_path, "MyLib/B.lean", "def b := 1\n")
        by = self._graph(tmp_path, monkeypatch)
        assert by["MyLib.A"] == ["MyLib.B"]  # external import filtered out
        assert by["MyLib.B"] == []

    def test_module_system_modifiers_and_import_all(self, tmp_path, monkeypatch):
        self._write(tmp_path, "L/A.lean",
                    "module\n\npublic import L.B\nmeta import L.C\nimport all L.D\n")
        for m in ("B", "C", "D"):
            self._write(tmp_path, f"L/{m}.lean", "def x := 1\n")
        by = self._graph(tmp_path, monkeypatch)
        assert by["L.A"] == ["L.B", "L.C", "L.D"]

    def test_indented_or_line_commented_imports_not_matched(self, tmp_path, monkeypatch):
        self._write(tmp_path, "L/A.lean",
                    "  import L.B\n-- import L.C\ndef a := 1\n")
        for m in ("B", "C"):
            self._write(tmp_path, f"L/{m}.lean", "def x := 1\n")
        by = self._graph(tmp_path, monkeypatch)
        assert by["L.A"] == []

    def test_covers_multiple_top_level_libs(self, tmp_path, monkeypatch):
        # The lake-exe-graph gap this replaces: an importer in a NON-default
        # lib must still produce an edge.
        self._write(tmp_path, "Core/Basic.lean", "def core := 1\n")
        self._write(tmp_path, "Extras/Uses.lean", "import Core.Basic\ndef e := core\n")
        monkeypatch.chdir(tmp_path)
        idx = discover_files.build_lean_file_index()
        graph = discover_files.build_import_graph(idx)
        dependents = discover_files.get_dependent_lean_files({"Core.Basic"}, graph)
        assert dependents == ["Extras.Uses"]


class TestMainOutputs:
    def test_writes_changed_files_and_graph_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Changed.lean").write_text("def changed := 1\n")
        github_output = tmp_path / "out.txt"
        monkeypatch.setenv("PR_NUMBER", "1")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.setattr(discover_files, "get_changed_lean_files", lambda pr: ["Changed.lean"])

        discover_files.main()

        out = github_output.read_text()
        assert "changed_files=Changed.lean\n" in out
        assert "discovered_files=Changed.lean\n" in out
        # The graph travels by FILE, not inline: a real repo's serialized graph
        # can exceed GITHUB_OUTPUT's 1 MB cap and Linux's 128 KiB env cap
        # (measured 183 KB on VCV-io).
        assert "lake_graph_path=lake_graph.json\n" in out
        assert "lake_graph=" not in out.replace("lake_graph_path=", "")
        graph = json.loads((tmp_path / "lake_graph.json").read_text())
        assert {"name": "Changed", "imports": []} in graph

    def test_graph_failure_falls_back_to_changed_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Changed.lean").write_text("def changed := 1\n")
        github_output = tmp_path / "out.txt"
        monkeypatch.setenv("PR_NUMBER", "1")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.setattr(discover_files, "get_changed_lean_files", lambda pr: ["Changed.lean"])

        def boom(index):
            raise OSError("disk on fire")
        monkeypatch.setattr(discover_files, "build_import_graph", boom)

        discover_files.main()

        assert "discovered_files=Changed.lean\n" in github_output.read_text()
        # Fallback still writes the (empty) graph file: a lake_graph.json
        # planted by PR-controlled build code must never survive to review.
        assert (tmp_path / "lake_graph.json").read_text() == "[]"
