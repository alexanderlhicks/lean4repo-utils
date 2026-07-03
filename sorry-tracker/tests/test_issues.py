"""Tests for issues.py: the main() orchestration (with side effects mocked)."""

import os
from types import SimpleNamespace

import issues


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestMainOrchestration:
    def _repo(self, tmp_path):
        _write(str(tmp_path / "Foo.lean"),
               "theorem foo : True := by\n  sorry\n\ndef bar := 1 -- no sorry here\n")
        return str(tmp_path)

    def test_dry_run_is_offline_and_lists_sorries(self, tmp_path, monkeypatch, capsys):
        repo = self._repo(tmp_path)
        monkeypatch.setattr(issues, "check_dependencies", lambda: None)
        # run_command must never be called in dry-run (no gh repo view).
        monkeypatch.setattr(issues, "run_command",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called in dry-run")))
        monkeypatch.setattr(issues.sys, "argv", ["sorry-tracker", "--repo-path", repo, "--dry-run"])
        cwd = os.getcwd()
        try:
            issues.main()
        finally:
            os.chdir(cwd)
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "foo" in out

    def test_full_run_creates_one_issue_per_sorry(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        created = []
        monkeypatch.setattr(issues, "check_dependencies", lambda: None)
        monkeypatch.setattr(issues, "run_command", lambda *a, **k: "owner/repo")
        monkeypatch.setattr(issues, "current_commit_sha", lambda: "deadbeef")
        monkeypatch.setattr(issues, "create_provider", lambda key: SimpleNamespace(name="fake"))
        monkeypatch.setattr(issues, "fetch_reference_parts", lambda urls: [])
        monkeypatch.setattr(issues, "generate_ai_analysis",
                            lambda *a, **k: "### Statement Explanation\n...")
        monkeypatch.setattr(issues, "create_github_issue",
                            lambda title, body, repo_name, label, stable_id: created.append((title, body, stable_id, repo_name)))
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.setattr(issues.sys, "argv", ["sorry-tracker", "--repo-path", repo])
        cwd = os.getcwd()
        try:
            issues.main()
        finally:
            os.chdir(cwd)
        assert len(created) == 1
        title, body, stable_id, repo_name = created[0]
        assert "foo" in title
        assert stable_id.startswith("foo@") and "Foo.lean" in stable_id
        assert repo_name == "owner/repo"
        # The permalink is pinned to the resolved commit SHA, not the moving HEAD.
        assert "/blob/deadbeef/" in body
