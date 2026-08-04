"""Shared pytest fixtures for the review suite.

The autouse fixture below exists because a large number of tests in this suite depend on
`LEANREPO_SANDBOX` being *absent*, and until now that was true only by accident — nothing
neutralised an inherited value. `sandbox_enabled()` fails CLOSED on any unrecognised
non-empty value, so a developer (or a CI job, or a fanned-out agent running pytest in a
shell that exported it) with `LEANREPO_SANDBOX=1` set would silently invert every such
test: `sandbox_wrap` would start raising `SandboxUnavailable` and the failures would look
like real regressions in unrelated code.

Tests that care about the flag set it explicitly via `monkeypatch.setenv`, which still
works — this only removes an *ambient* value inherited from the parent environment.
"""

import pytest


@pytest.fixture(autouse=True)
def _neutralise_ambient_sandbox_flag(monkeypatch):
    """Remove an inherited LEANREPO_SANDBOX so tests see the documented default (off)."""
    monkeypatch.delenv("LEANREPO_SANDBOX", raising=False)
