"""End-to-end Session-15 round-trips against a REAL Lean repository.

Skipped unless the environment names a built Lean checkout and two probe
declarations. Read-only for the target repo (only `lake env lean --stdin`
queries are run; nothing is written into it). Example:

    E2E_LEAN_REPO=~/ArkLib \
    E2E_LEAN_FILE=ArkLib/ProofSystem/Fri/Spec/SingleRound.lean \
    E2E_SORRY_DECL=Fri.Spec.FoldPhase.inputRelation \
    E2E_CLEAN_DECL=Fri.Spec.round_bound \
    uv run --no-sync pytest tests/test_e2e_lean.py -v
"""

import os

import pytest

E2E_REPO = os.path.expanduser(os.environ.get("E2E_LEAN_REPO", ""))
E2E_FILE = os.environ.get("E2E_LEAN_FILE", "")
E2E_SORRY_DECL = os.environ.get("E2E_SORRY_DECL", "")
E2E_CLEAN_DECL = os.environ.get("E2E_CLEAN_DECL", "")

pytestmark = pytest.mark.skipif(
    not (E2E_REPO and E2E_FILE and E2E_SORRY_DECL and E2E_CLEAN_DECL),
    reason="E2E_LEAN_REPO/E2E_LEAN_FILE/E2E_SORRY_DECL/E2E_CLEAN_DECL not set",
)

FABRICATED_DECL = "E2ELedger.noSuchDecl9f3a"


def test_source_ledger_on_real_repo(monkeypatch):
    """Gap A end-to-end: the ledger lists the repo's real admitted decl."""
    import review

    monkeypatch.chdir(E2E_REPO)
    ledger = review.build_source_ledger([E2E_FILE])
    short_name = E2E_SORRY_DECL.rsplit(".", 1)[-1]
    assert short_name in ledger
    assert "unadjudicated" in ledger


def test_coverage_matrix_round_trips_on_real_repo(monkeypatch):
    """Gap B end-to-end, three kernel round-trips on the real toolchain:
    sorryAx on an admitted decl (claimed proven -> status-mismatch), an
    unknown constant (-> unresolved-symbol), and a clean proven decl (-> no
    finding)."""
    import time

    from coverage_matrix import check_coverage_matrix, parse_coverage_matrix

    monkeypatch.chdir(E2E_REPO)
    matrix = (
        "| result | symbol | status | file |\n"
        "| --- | --- | --- | --- |\n"
        f"| item 1 | `{E2E_SORRY_DECL}` | proven | {E2E_FILE} |\n"
        f"| item 2 | `{E2E_CLEAN_DECL}` | proven | {E2E_FILE} |\n"
        f"| item 3 | `{FABRICATED_DECL}` | proven | {E2E_FILE} |\n"
    )
    rows, warnings = parse_coverage_matrix(matrix, E2E_REPO)
    assert warnings == []
    assert len(rows) == 3

    findings, infra_warnings, notes = check_coverage_matrix(
        rows, deadline=time.monotonic() + 280.0
    )
    assert infra_warnings == [], infra_warnings
    by_symbol = {f.row.symbol: f for f in findings}
    assert by_symbol[E2E_SORRY_DECL].kind == "status-mismatch"
    assert by_symbol[FABRICATED_DECL].kind == "unresolved-symbol"
    assert E2E_CLEAN_DECL not in by_symbol
