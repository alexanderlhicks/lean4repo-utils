"""Exercise the action's provider-key lifecycle without credentials or API calls."""

import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review  # noqa: E402


def action_steps():
    action = Path(__file__).resolve().parents[1] / "action.yml"
    return yaml.safe_load(action.read_text())["runs"]["steps"]


def stage_step():
    steps = [s for s in action_steps() if s.get("id") == "stage_review_key"]
    assert len(steps) == 1, "The action must stage the provider key before review"
    assert steps[0]["env"]["REVIEW_API_KEY"] == "${{ inputs.api_key }}"
    return steps[0]


def stage_key(tmp_path, key, **extra_env):
    output = tmp_path / "outputs"
    env = {
        "PATH": os.defpath,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "REVIEW_API_KEY": key,
        **extra_env,
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stage_step()["run"]],
        env=env, cwd=tmp_path, capture_output=True, text=True,
    )
    return result, output


def staged_path(output):
    return Path(dict(line.split("=", 1) for line in output.read_text().splitlines())["api_key_file"])


def test_review_receives_a_file_path_and_no_environment_key():
    steps = action_steps()
    stage = stage_step()
    run = next(s for s in steps if s.get("id") == "run_review")
    assert steps.index(stage) < steps.index(run)
    assert run["env"]["API_KEY_FILE"] == "${{ steps.stage_review_key.outputs.api_key_file }}"
    assert run["env"]["API_KEY"] == ""  # Clear an inherited legacy key too.
    assert all("inputs.api_key" not in step.get("run", "") for step in steps)


def test_stage_load_and_child_environment(tmp_path, monkeypatch):
    marker = tmp_path / "injected"
    key = f"dummy-key'\";$(touch {marker})\nsecond-line"
    result, output = stage_key(tmp_path, key)
    assert result.returncode == 0, result.stderr
    path = staged_path(output)
    assert path.parent == tmp_path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == key
    assert not marker.exists()
    assert key not in result.stdout + result.stderr + output.read_text()

    monkeypatch.setenv("API_KEY_FILE", str(path))
    monkeypatch.setenv("API_KEY", "")
    assert review._load_provider_key() == key
    assert not path.exists()
    subprocess.run(
        [sys.executable, "-c", "import os; assert 'API_KEY' not in os.environ; "
         "assert 'API_KEY_FILE' not in os.environ"], check=True,
    )


def test_cleanup_removes_key_if_review_never_reads_it(tmp_path):
    result, output = stage_key(tmp_path, "dummy-key")
    assert result.returncode == 0, result.stderr
    path = staged_path(output)
    cleanup = next(s for s in action_steps() if s.get("name") == "Clean up artifacts")
    assert cleanup["if"] == "always()"
    assert cleanup["env"]["API_KEY_FILE"] == "${{ steps.stage_review_key.outputs.api_key_file }}"
    env = {
        "PATH": os.defpath,
        "API_KEY_FILE": str(path),
        "BUILD_LOG_PATH": str(tmp_path / "build.log"),
        "LAKE_GRAPH_OUT": str(tmp_path / "graph.json"),
        "PAPER_LEAN_EVIDENCE_OUT": str(tmp_path / "evidence.json"),
    }
    subprocess.run(["bash", "-euo", "pipefail", "-c", cleanup["run"]],
                   env=env, cwd=tmp_path, check=True)
    assert not path.exists()


def test_stage_cleans_up_if_publishing_output_fails(tmp_path):
    result, _ = stage_key(tmp_path, "dummy-key", GITHUB_OUTPUT=str(tmp_path / "missing" / "outputs"))
    assert result.returncode != 0
    assert not list(tmp_path.glob("lean-review-key.*"))
    assert "dummy-key" not in result.stdout + result.stderr


def test_caller_staged_key_is_supported(tmp_path):
    path = tmp_path / "caller.key"
    path.write_text("caller-key")
    path.chmod(0o600)
    result, output = stage_key(tmp_path, "", API_KEY_FILE=str(path))
    assert result.returncode == 0, result.stderr
    assert staged_path(output) == path
    assert path.read_text() == "caller-key"
    assert not list(tmp_path.glob("lean-review-key.*"))


def test_missing_key_fails_before_review(tmp_path):
    result, _ = stage_key(tmp_path, "")
    assert result.returncode != 0
    assert not list(tmp_path.glob("lean-review-key.*"))


def test_caller_file_is_removed_if_publishing_output_fails(tmp_path):
    path = tmp_path / "caller.key"
    path.write_text("caller-key")
    result, _ = stage_key(tmp_path, "", API_KEY_FILE=str(path),
                          GITHUB_OUTPUT=str(tmp_path / "missing" / "outputs"))
    assert result.returncode != 0
    assert not path.exists()
    assert "caller-key" not in result.stdout + result.stderr


def test_staging_children_do_not_receive_key_even_with_inherited_variable(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    mktemp = shutil.which("mktemp", path=os.defpath)
    assert mktemp
    probe = (
        "import os, sys; assert 'REVIEW_API_KEY' not in os.environ; "
        "assert 'review_key' not in os.environ; "
        f"os.execv({mktemp!r}, [{mktemp!r}] + sys.argv[1:])"
    )
    wrapper = tools / "mktemp"
    wrapper.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(probe)} "$@"\n'
    )
    wrapper.chmod(0o755)
    result, output = stage_key(tmp_path, "dummy-key", PATH=str(tools) + os.pathsep + os.defpath,
                               review_key="inherited-placeholder")
    assert result.returncode == 0, result.stderr
    assert staged_path(output).read_text() == "dummy-key"
