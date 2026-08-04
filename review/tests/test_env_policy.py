"""Env-policy tests for S18r.

Design constraints carried from the S18r Gate-1 review, which found ~12 HIGH findings
where a *proposed* test provably could not fail. Each is honoured here:

* **No equality against a reconstructed "0.3 formula".** Such a test becomes an identity
  the moment `main` fast-forwards onto this branch, so it would silently stop testing
  anything. Instead the invariants below are stated in terms of the *properties* the
  policy must have, which remain checkable forever.
* **No absence-only assertions.** `scrubbed_env() == {}` — the worst possible regression,
  under which every Lean seam dies — satisfies every "secret not present" check. So every
  absence assertion here is paired with a POSITIVE assertion that the vars a build actually
  needs are present.
* **No parsing of tokens a sibling change can delete.** The shell/Python parity test below
  is *behavioural*: it executes both real implementations over a value table. It cannot be
  defeated by reformatting either one, and it cannot go vacuously green (an empty table
  would fail the length assertion).
* **No dumping of `os.environ` into an assertion diff**, which would print real secret
  values into CI logs on failure. Comparisons are over key SETS, and failure messages name
  keys only.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lean_tools  # noqa: E402

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class TestEnvAllowedPrecedence:
    """`_env_allowed` is the single authority; its clause ORDER is load-bearing."""

    def test_hard_floor_beats_everything(self, monkeypatch):
        """A `_NEVER_FORWARD` name must be refused even if someone allowlists it.

        This is the whole point of the floor: it makes the dangerous direction
        unreachable for a future contributor widening the allowlist to fix a build.
        """
        for name in ("LD_PRELOAD", "BASH_ENV", "GIT_SSH_COMMAND", "GITHUB_ENV", "GITHUB_PATH"):
            assert lean_tools._env_allowed(name) is False, name
            # ...and still refused when explicitly allowlisted.
            monkeypatch.setattr(
                lean_tools, "_ENV_ALLOWLIST", lean_tools._ENV_ALLOWLIST | {name}
            )
            assert lean_tools._env_allowed(name) is False, f"{name} escaped the floor"

    def test_secret_exemption_cannot_admit_a_non_allowlisted_name(self, monkeypatch):
        """The CA exemption relaxes only the regex — never membership."""
        monkeypatch.setattr(
            lean_tools, "_SECRET_ENV_EXEMPT", lean_tools._SECRET_ENV_EXEMPT | {"ROGUE_CERT"}
        )
        assert lean_tools._env_allowed("ROGUE_CERT") is False

    def test_secret_exemption_cannot_defeat_the_floor(self, monkeypatch):
        monkeypatch.setattr(
            lean_tools, "_ENV_ALLOWLIST", lean_tools._ENV_ALLOWLIST | {"LD_PRELOAD"}
        )
        monkeypatch.setattr(
            lean_tools, "_SECRET_ENV_EXEMPT", lean_tools._SECRET_ENV_EXEMPT | {"LD_PRELOAD"}
        )
        assert lean_tools._env_allowed("LD_PRELOAD") is False

    def test_ca_paths_survive_the_cert_regex(self):
        """The regex matches the fragment "CERT", so a public CA *path* needs an explicit
        exemption; allowlisting it alone is a silent no-op. Regression for that trap."""
        for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "NIX_SSL_CERT_FILE"):
            assert lean_tools._SECRET_ENV_RE.search(name), f"{name} no longer needs exemption"
            assert lean_tools._env_allowed(name) is True, name

    def test_secret_named_lake_var_still_dropped(self):
        """The `LAKE_` prefix rule must not become a hole."""
        for name in ("LAKE_TOKEN", "LAKE_AUTH_TOKEN", "LAKE_SIGNING_KEY"):
            assert lean_tools._env_allowed(name) is False, name

    def test_ssh_auth_sock_is_deliberately_excluded(self):
        """Recorded decision, not an oversight: forwarding an agent socket into
        PR-controlled elaboration would hand it the operator's SSH identity. Every fleet
        dependency uses HTTPS, so nothing needs it. Adopters with private SSH deps are
        warned in the README instead."""
        assert lean_tools._env_allowed("SSH_AUTH_SOCK") is False


class TestPolicyInvariants:
    """Structural invariants — these are what keep the sets honest over time."""

    def test_floor_and_allowlist_are_disjoint(self):
        overlap = lean_tools._NEVER_FORWARD & lean_tools._ENV_ALLOWLIST
        assert not overlap, f"names both allowlisted and floored: {sorted(overlap)}"

    def test_every_exemption_is_necessary(self):
        """An exemption for a name the regex does not block is dead config that implies a
        constraint which does not exist. Keep the set minimal and truthful."""
        for name in lean_tools._SECRET_ENV_EXEMPT:
            assert lean_tools._SECRET_ENV_RE.search(name), (
                f"{name} is exempted but not regex-blocked — remove it"
            )

    def test_exemptions_are_allowlisted(self):
        assert lean_tools._SECRET_ENV_EXEMPT <= lean_tools._ENV_ALLOWLIST


class TestScrubbedEnvIsUsable:
    """Paired positive/negative: absence alone would be satisfied by returning `{}`."""

    def test_secrets_dropped_and_toolchain_preserved(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/x")
        monkeypatch.setenv("ELAN_HOME", "/home/x/.elan")
        monkeypatch.setenv("LEAN_NUM_THREADS", "4")
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/cert.pem")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
        monkeypatch.setenv("OR_AUTH", "novel-secret-name")
        monkeypatch.setenv("GITHUB_ENV", "/runner/file")

        env = lean_tools.scrubbed_env()

        # POSITIVE: a build must still be able to run.
        for required in ("PATH", "HOME", "ELAN_HOME", "LEAN_NUM_THREADS", "SSL_CERT_FILE"):
            assert required in env, f"{required} missing — the child could not build"
        # NEGATIVE: and no credential or runner command file may ride along.
        for forbidden in ("GITHUB_TOKEN", "OPENROUTER_API_KEY", "OR_AUTH", "GITHUB_ENV"):
            assert forbidden not in env, f"{forbidden} leaked into the child env"

    def test_never_returns_empty_for_a_realistic_environment(self, monkeypatch):
        """Guards the degenerate regression the absence-only assertions cannot see."""
        monkeypatch.setenv("PATH", "/usr/bin")
        assert lean_tools.scrubbed_env(), "scrubbed_env() returned nothing"


class TestEnvPolicyIndependentOfSandboxFlag:
    """S18r item 2, reduced to its honest residue.

    A policy KNOB was deliberately NOT built: it would have been attacker-settable (PR
    code runs `lake build` and can write `GITHUB_ENV`, setting env for later steps). What
    remains is pinning that env hygiene is not coupled to the sandbox flag at all, so a
    future change cannot reintroduce the rejected "gate the allowlist" design — under
    which setting `LEANREPO_SANDBOX=0` on a Landlock-less kernel would silently also
    disable secret hygiene.
    """

    def test_scrubbed_env_identical_with_flag_on_and_off(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/x")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

        monkeypatch.delenv("LEANREPO_SANDBOX", raising=False)
        off = set(lean_tools.scrubbed_env())
        monkeypatch.setenv("LEANREPO_SANDBOX", "1")
        on = set(lean_tools.scrubbed_env())

        assert off == on, (
            "env policy changed with the sandbox flag; it must be independent "
            f"(only-off={sorted(off - on)}, only-on={sorted(on - off)})"
        )
        assert "GITHUB_TOKEN" not in off and "PATH" in off


class TestShellPythonFlagParity:
    """The verified fail-OPEN this session fixes.

    `landrun-wrap.sh` normalised with `tr -d '[:space:]'` (deleting INTERIOR whitespace)
    while `sandbox_enabled()` uses `.strip()` (edges only), so `LEANREPO_SANDBOX='o ff'`
    collapsed to `off` and ran the build UNCONFINED on the shell side while Python failed
    closed. Behavioural comparison of the two real implementations.
    """

    VALUES = [
        "", " ", "  ", "0", "false", "no", "off", "disabled", "OFF", " 0 ",
        "\n0\n", "\toff\t", "\n \t",
        "1", "true", "yes", "on", "require", "enabled", "TRUE", " 1 ", "\non",
        # Unrecognised ⇒ must fail CLOSED (enabled) on BOTH sides.
        "o ff", "of f", "yolo", "2", "disabled x", "d\nisabled",
    ]

    def _shell_verdict(self, value):
        env = dict(os.environ)
        env["LEANREPO_SANDBOX"] = value
        proc = subprocess.run(
            ["bash", "-c",
             f'set -euo pipefail; . "{_SCRIPTS}/sandbox_flag.sh"; '
             'if sandbox_flag_enabled; then echo ENABLED; else echo DISABLED; fi'],
            capture_output=True, text=True, env=env, timeout=30,
        )
        lines = [ln for ln in proc.stdout.splitlines() if ln in ("ENABLED", "DISABLED")]
        assert lines, f"shell parser produced no verdict (rc={proc.returncode})"
        return lines[-1]

    @pytest.mark.parametrize("value", VALUES)
    def test_shell_agrees_with_python(self, value, monkeypatch):
        monkeypatch.setenv("LEANREPO_SANDBOX", value)
        expected = "ENABLED" if lean_tools.sandbox_enabled() else "DISABLED"
        assert self._shell_verdict(value) == expected, (
            f"flag parser drift for {value!r}: shell disagrees with sandbox_enabled()"
        )

    def test_the_reproduced_fail_open_is_closed(self, monkeypatch):
        """Named regression for the exact reported value."""
        monkeypatch.setenv("LEANREPO_SANDBOX", "o ff")
        assert lean_tools.sandbox_enabled() is True
        assert self._shell_verdict("o ff") == "ENABLED"

    def test_value_table_is_not_empty(self):
        """A parity test over an empty table would be vacuously green."""
        assert len(self.VALUES) >= 20


class TestLandrunWrapFailsClosed:
    """The shared parser is SOURCED, not executed for its exit status, precisely so a
    missing/corrupt copy aborts instead of being misread as "sandboxing disabled" (bash
    returns 127 for a missing file, 126 for non-executable, 2 for a syntax error — an
    exit-status design collapses all of them into "off" and runs PR code unconfined)."""

    def test_passes_through_when_disabled(self, tmp_path):
        env = dict(os.environ, LEANREPO_SANDBOX="0")
        proc = subprocess.run(
            ["bash", str(_SCRIPTS / "landrun-wrap.sh"), str(tmp_path), "--",
             "/bin/echo", "PASSTHROUGH"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert proc.returncode == 0 and "PASSTHROUGH" in proc.stdout

    def test_aborts_when_the_flag_library_is_missing(self, tmp_path):
        """Copy the wrapper WITHOUT its library: it must refuse, not pass through."""
        lone = tmp_path / "landrun-wrap.sh"
        lone.write_bytes((_SCRIPTS / "landrun-wrap.sh").read_bytes())
        wdir = tmp_path / "wd"
        env = dict(os.environ, LEANREPO_SANDBOX="0")
        proc = subprocess.run(
            ["bash", str(lone), str(wdir), "--", "/bin/echo", "MUST_NOT_RUN"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert proc.returncode != 0, "missing flag library was treated as 'disabled'"
        assert "MUST_NOT_RUN" not in proc.stdout

    @pytest.mark.parametrize("corruption", ["empty", "comments_only", "wrong_function_name"])
    def test_aborts_when_the_library_loads_but_defines_nothing(self, tmp_path, corruption):
        """The fail-open this session SHIPPED and then caught, now pinned.

        `test_aborts_when_the_flag_library_is_missing` covers only the missing-file case,
        which aborts at source time. It misses the variant that actually fails OPEN: a
        library that sources CLEANLY but leaves `sandbox_flag_enabled` undefined. bash then
        returns 127 for the missing command, and because a command inside an `if !`
        condition is EXEMPT from `set -e`, the `!` inverts 127 into "disabled" and the
        payload runs UNCONFINED. Reproduced on bash 5.2 before the explicit `declare -F`
        guard was added. All three corruptions are realistic: a truncated write, a partial
        copy, and a rename/typo.
        """
        real = (_SCRIPTS / "sandbox_flag.sh").read_text(encoding="utf-8")
        if corruption == "empty":
            body = ""
        elif corruption == "comments_only":
            body = "\n".join(real.splitlines()[:20]) + "\n"
        else:
            body = real.replace("sandbox_flag_enabled()", "sandbox_flag_typo()")
        assert "sandbox_flag_enabled()" not in body, "fixture must not define the function"

        (tmp_path / "sandbox_flag.sh").write_text(body)
        wrapper = tmp_path / "landrun-wrap.sh"
        wrapper.write_bytes((_SCRIPTS / "landrun-wrap.sh").read_bytes())

        env = dict(os.environ, LEANREPO_SANDBOX="1")
        proc = subprocess.run(
            ["bash", str(wrapper), str(tmp_path / "wd"), "--", "/bin/echo", "LEAKED"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert "LEAKED" not in proc.stdout, (
            f"FAIL-OPEN: ran the payload unconfined with a {corruption} flag library"
        )
        assert proc.returncode != 0

    def test_refuses_to_run_unconfined_when_enabled_without_landrun(self, tmp_path):
        """With sandboxing requested but no landrun on PATH, the command must not run.

        A minimal PATH is built with only the utilities the wrapper itself needs, so
        `landrun` is provably absent while the script can still execute. (Emptying PATH
        outright would prevent `bash` from starting and pass for the wrong reason.)
        """
        bindir = tmp_path / "bin"
        bindir.mkdir()
        import shutil as _shutil
        for util in ("bash", "dirname", "mkdir", "echo"):
            resolved = _shutil.which(util)
            assert resolved, f"test prerequisite {util} not found"
            (bindir / util).symlink_to(resolved)
        assert not (bindir / "landrun").exists()

        wdir = tmp_path / "wd"
        env = dict(os.environ, LEANREPO_SANDBOX="1", PATH=str(bindir))
        proc = subprocess.run(
            [str(bindir / "bash"), str(_SCRIPTS / "landrun-wrap.sh"), str(wdir), "--",
             "echo", "MUST_NOT_RUN"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert proc.returncode != 0, "ran PR code unconfined when landrun was absent"
        assert "MUST_NOT_RUN" not in proc.stdout


class TestBothShellSitesUseTheOneAuthority:
    """Gate-2 correction: there were THREE copies of the parser, not two — the third in
    `action.yml`'s sandbox self-test step. A fix that misses one leaves the drift."""

    def test_no_tr_delete_whitespace_parser_remains(self):
        """The `tr -d '[:space:]'` idiom is the bug; it must not reappear as CODE.

        Comment lines are excluded on purpose: both files legitimately *describe* the old
        idiom to explain why it was wrong, and a naive substring scan would flag that
        documentation forever (it did, on first run).
        """
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in list(root.glob("*.yml")) + list((root / "scripts").glob("*.sh")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                if line.lstrip().startswith("#"):
                    continue  # documentation of the historical bug, not the bug
                if "tr -d '[:space:]'" in line:
                    offenders.append(f"{path.name}:{lineno}")
        assert not offenders, f"whitespace-deleting flag parser still present at {offenders}"

    def test_both_shell_consumers_source_the_shared_parser(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("scripts/landrun-wrap.sh", "action.yml"):
            text = (root / rel).read_text(encoding="utf-8")
            assert "sandbox_flag.sh" in text, f"{rel} does not use the shared flag parser"
            assert "sandbox_flag_enabled" in text, f"{rel} does not call the shared parser"
