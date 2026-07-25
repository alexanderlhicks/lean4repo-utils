"""Tests for the Lean CLI tool backend (lean_tools.py)."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

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

    def test_allowlist_drops_novel_secret_names(self, monkeypatch):
        # The whole point of an allowlist over the old denylist: a secret under a
        # name that does NOT contain TOKEN/KEY/SECRET/... is still dropped, because
        # it is simply not on the allowlist. The old regex would have leaked these.
        for novel in ("OR_AUTH", "GH_APP_PEM", "BEARER", "SESSION_COOKIE", "SLACK_HOOK"):
            monkeypatch.setenv(novel, "leak")
        env = scrubbed_env()
        for novel in ("OR_AUTH", "GH_APP_PEM", "BEARER", "SESSION_COOKIE", "SLACK_HOOK"):
            assert novel not in env

    def test_lake_prefix_allowed_but_secret_named_lake_var_dropped(self, monkeypatch):
        monkeypatch.setenv("LAKE_HOME", "/x/.lake")     # infra: allowed via prefix
        monkeypatch.setenv("LAKE_TOKEN", "leak")        # secret name: denylist backstop drops it
        env = scrubbed_env()
        assert env.get("LAKE_HOME") == "/x/.lake"
        assert "LAKE_TOKEN" not in env

    def test_unlisted_infra_looking_var_dropped(self, monkeypatch):
        monkeypatch.setenv("SOME_RANDOM_CONFIG", "v")   # not on the allowlist -> dropped
        assert "SOME_RANDOM_CONFIG" not in scrubbed_env()

    def test_lake_prefixed_secret_name_variants_dropped(self, monkeypatch):
        # Backstop covers credential fragments beyond TOKEN/KEY, so a LAKE_-prefixed
        # secret can't slip through the prefix allow rule.
        for v in ("LAKE_AUTH", "LAKE_BEARER", "LAKE_PRIVATE", "LAKE_MY_PEM", "LAKE_SIGNING"):
            monkeypatch.setenv(v, "leak")
        env = scrubbed_env()
        for v in ("LAKE_AUTH", "LAKE_BEARER", "LAKE_PRIVATE", "LAKE_MY_PEM", "LAKE_SIGNING"):
            assert v not in env


class TestSandbox:
    def test_passthrough_when_disabled(self, monkeypatch):
        monkeypatch.delenv("LEANREPO_SANDBOX", raising=False)
        argv = ["lake", "env", "lean", "--stdin"]
        assert lean_tools.sandbox_wrap(argv) == argv          # unchanged (local/dev/tests)

    def test_structure_when_enabled_and_available(self, monkeypatch):
        monkeypatch.setenv("LEANREPO_SANDBOX", "1")
        monkeypatch.setattr(lean_tools, "landrun_available", lambda: True)
        wrapped = lean_tools.sandbox_wrap(["lake", "env", "lean", "--stdin"], write_dir="/w/.lake")
        assert wrapped[0] == "landrun"
        assert "--rwx" in wrapped and "/w/.lake" in wrapped   # build dir writable
        assert "--net" not in wrapped                         # egress denied by default
        i = wrapped.index("--")                               # command follows the separator
        assert wrapped[i + 1:] == ["lake", "env", "lean", "--stdin"]

    def test_enabled_accepts_common_truthy_values(self, monkeypatch):
        for v in ("1", "true", "yes", "on", "require", "enabled", "TRUE"):
            monkeypatch.setenv("LEANREPO_SANDBOX", v)
            assert lean_tools.sandbox_enabled() is True, v

    def test_disabled_on_empty_and_explicit_off(self, monkeypatch):
        monkeypatch.delenv("LEANREPO_SANDBOX", raising=False)
        assert lean_tools.sandbox_enabled() is False
        for v in ("0", "false", "no", "off", "disabled"):
            monkeypatch.setenv("LEANREPO_SANDBOX", v)
            assert lean_tools.sandbox_enabled() is False, v

    def test_fail_closed_on_unrecognized_value(self, monkeypatch):
        # A typo (e.g. LEANREPO_SANDBOX=enable/yolo) must NOT silently disable the
        # sandbox — a security control fails closed (enabled), not open.
        monkeypatch.setenv("LEANREPO_SANDBOX", "yolo")
        assert lean_tools.sandbox_enabled() is True

    def test_fail_closed_when_enabled_but_absent(self, monkeypatch):
        monkeypatch.setenv("LEANREPO_SANDBOX", "1")
        monkeypatch.setattr(lean_tools, "landrun_available", lambda: False)
        with pytest.raises(lean_tools.SandboxUnavailable):
            lean_tools.sandbox_wrap(["lake", "env", "lean"])

    def test_seam_is_wrapped_when_enabled(self, monkeypatch):
        # Proves the _run_lean seam actually routes through sandbox_wrap.
        monkeypatch.setenv("LEANREPO_SANDBOX", "1")
        monkeypatch.setattr(lean_tools, "landrun_available", lambda: True)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(stdout="ok", stderr="")
        monkeypatch.setattr(lean_tools.subprocess, "run", fake_run)
        LeanToolbox(module="M").run("lean_print", {"name": "foo"})
        assert captured["cmd"][0] == "landrun" and "--stdin" in captured["cmd"]


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
