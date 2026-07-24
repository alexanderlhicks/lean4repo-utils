"""Tests for the Lean CLI tool backend (lean_tools.py)."""

import os
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lean_tools
from lean_tools import LeanToolbox, lean_available, scrubbed_env


class TestScrubEnv:
    def test_removes_secrets_keeps_others(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setenv("MY_SECRET", "s")
        monkeypatch.setenv("DB_PASSWORD", "p")
        monkeypatch.setenv("PATH", "/bin")
        monkeypatch.setenv("HOME", "/home/x")
        env = scrubbed_env()
        for leaked in ("GITHUB_TOKEN", "OPENROUTER_API_KEY", "MY_SECRET", "DB_PASSWORD"):
            assert leaked not in env
        assert env["PATH"] == "/bin"
        assert env["HOME"] == "/home/x"


class TestRunLean:
    def _mock(self, monkeypatch, stdout="", stderr="", raise_exc=None):
        captured = {}

        def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, env=None):
            captured["cmd"] = cmd
            captured["input"] = input
            captured["env"] = env
            captured["timeout"] = timeout
            if raise_exc:
                raise raise_exc
            return SimpleNamespace(stdout=stdout, stderr=stderr)
        monkeypatch.setattr(lean_tools.subprocess, "run", fake_run)
        return captured

    def test_check_builds_command_with_import(self, monkeypatch):
        cap = self._mock(monkeypatch, stdout="List.map : ...")
        out = LeanToolbox(module="Proj.Foo").run("lean_check", {"expr": "List.map"})
        assert "import Proj.Foo" in cap["input"]
        assert "#check List.map" in cap["input"]
        assert "List.map :" in out

    def test_print_axioms_command(self, monkeypatch):
        cap = self._mock(monkeypatch, stdout="'foo' depends on axioms: [propext]")
        LeanToolbox(module="M").run("lean_print_axioms", {"name": "M.foo"})
        assert "#print axioms M.foo" in cap["input"]

    def test_no_module_no_import(self, monkeypatch):
        cap = self._mock(monkeypatch, stdout="ok")
        LeanToolbox(module=None).run("lean_check", {"expr": "x"})
        assert "import" not in cap["input"]

    def test_env_is_scrubbed(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "secret")
        cap = self._mock(monkeypatch, stdout="ok")
        LeanToolbox(module="M").run("lean_print", {"name": "foo"})
        assert "GITHUB_TOKEN" not in cap["env"]

    def test_timeout_message(self, monkeypatch):
        self._mock(monkeypatch, raise_exc=subprocess.TimeoutExpired(cmd="lean", timeout=5))
        out = LeanToolbox(module="M", timeout=5).run("lean_typecheck", {"code": "def x := 1"})
        assert "timed out" in out

    def test_unavailable_message(self, monkeypatch):
        self._mock(monkeypatch, raise_exc=FileNotFoundError("no lake"))
        out = LeanToolbox().run("lean_check", {"expr": "x"})
        assert "unavailable" in out

    def test_empty_output_note(self, monkeypatch):
        self._mock(monkeypatch, stdout="", stderr="")
        out = LeanToolbox(module="M").run("lean_typecheck", {"code": "def x := 1"})
        assert "no output" in out

    def test_output_truncated(self, monkeypatch):
        self._mock(monkeypatch, stdout="x" * 10000)
        out = LeanToolbox().run("lean_check", {"expr": "y"})
        assert len(out) <= lean_tools.MAX_TOOL_OUTPUT_CHARS

    def test_unknown_tool(self):
        assert "unknown tool" in LeanToolbox().run("nope", {})


class TestSpecs:
    def test_four_tools_with_expected_names(self):
        names = {s["function"]["name"] for s in LeanToolbox(module="M").specs()}
        assert names == {"lean_check", "lean_print", "lean_print_axioms", "lean_typecheck"}

    def test_specs_are_wellformed(self):
        for s in LeanToolbox().specs():
            assert s["type"] == "function"
            assert "name" in s["function"] and "parameters" in s["function"]


class TestLeanAvailable:
    def test_true_when_lake_on_path(self, monkeypatch):
        monkeypatch.setattr(lean_tools.shutil, "which", lambda x: "/usr/bin/lake")
        assert lean_available() is True

    def test_false_when_absent(self, monkeypatch):
        monkeypatch.setattr(lean_tools.shutil, "which", lambda x: None)
        assert lean_available() is False
