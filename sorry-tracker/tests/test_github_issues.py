"""Tests for github_issues.py: duplicate detection and commit-SHA resolution."""

from types import SimpleNamespace

import github_issues


class TestFindExistingIssue:
    def test_found(self, monkeypatch):
        monkeypatch.setattr(
            github_issues.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout='[{"number": 7}]', stderr=""),
        )
        assert github_issues._find_existing_issue("<!-- id -->", "o/r") == 7

    def test_none_when_empty(self, monkeypatch):
        monkeypatch.setattr(
            github_issues.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
        )
        assert github_issues._find_existing_issue("<!-- id -->", "o/r") is None

    def test_none_and_no_exit_on_gh_failure(self, monkeypatch):
        # A transient gh failure must NOT abort the run (regression: the old
        # path routed through run_command, which sys.exit()s).
        monkeypatch.setattr(
            github_issues.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="rate limited"),
        )
        assert github_issues._find_existing_issue("<!-- id -->", "o/r") is None

    def test_none_when_gh_missing(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError()
        monkeypatch.setattr(github_issues.subprocess, "run", boom)
        assert github_issues._find_existing_issue("<!-- id -->", "o/r") is None


class TestCurrentCommitSha:
    def test_returns_sha(self, monkeypatch):
        monkeypatch.setattr(
            github_issues.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="abc123\n", stderr=""),
        )
        assert github_issues.current_commit_sha() == "abc123"

    def test_falls_back_to_head_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            github_issues.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=128, stdout="", stderr="not a repo"),
        )
        assert github_issues.current_commit_sha() == "HEAD"

    def test_falls_back_when_git_missing(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError()
        monkeypatch.setattr(github_issues.subprocess, "run", boom)
        assert github_issues.current_commit_sha() == "HEAD"
