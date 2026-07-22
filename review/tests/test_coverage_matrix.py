"""Tests for the deterministic coverage-matrix resolution (R7 Gap B).

The checker tests drive the REAL extract_axioms parser with a fake
run_lean_command returning real-format Lean output strings (the shared-seam
approach), so the fakes and the production parser cannot diverge.
"""

import os

import lean_info_extractor
from coverage_matrix import (
    MAX_MATRIX_ROWS,
    CoverageFinding,
    MatrixRow,
    check_coverage_matrix,
    is_valid_lean_symbol,
    parse_coverage_matrix,
)

# Real Lean 4 output shapes (see lean_info_extractor.extract_axioms).
OUT_SORRY = "'{d}' depends on axioms: [propext, sorryAx, Classical.choice]"
OUT_CLEAN = "'{d}' depends on axioms: [propext, Classical.choice]"
OUT_NO_AXIOMS = "'{d}' does not depend on any axioms"
# Current Lean spelling (observed live on ArkLib, lean-toolchain 4.x 2026):
OUT_UNKNOWN = "<stdin>:2:14: error(lean.unknownIdentifier): Unknown constant `{d}`"
# Older-toolchain spelling — classification must accept both.
OUT_UNKNOWN_OLD = "<stdin>:2:14: error: unknown constant '{d}'"
OUT_IMPORT_FAIL = "error: no such file or directory (error code: 2)\n  logs at ./.lake"

HEADER = "| result | symbol | status | file |\n| --- | --- | --- | --- |\n"


def _matrix(*rows):
    return HEADER + "\n".join(rows) + "\n"


def _mk_repo(tmp_path, *files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("theorem x : True := trivial\n")
    return str(tmp_path)


class TestSymbolAllowlist:
    def test_valid_symbols(self):
        for sym in ("foo", "Foo.bar", "Ns.Sub.thm'", "_root", "α.β", "Nat.add_comm", "ℝtest"):
            assert is_valid_lean_symbol(sym), sym

    def test_rejected_symbols(self):
        bad = [
            "", "foo bar", "foo\nbar", "foo#eval", "foo\\bar", "foo[0]",
            "«evil»", "foo..bar", ".foo", "foo.", "foo;bar", "foo`bar",
            "foo-bar", "a" * 201, "1foo", "foo/bar",
        ]
        for sym in bad:
            assert not is_valid_lean_symbol(sym), repr(sym)


class TestParseCoverageMatrix:
    def test_valid_row_parsed(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo/Bar.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm 1 | `Ns.foo` | proven | Foo/Bar.lean |"), root)
        assert warnings == []
        assert len(rows) == 1
        assert rows[0].symbol == "Ns.foo"
        assert rows[0].claimed == "proven"
        assert rows[0].module == "Foo.Bar"

    def test_backticked_path_and_dot_slash_accepted(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `foo` | sorry | `./Foo.lean` |"), root)
        assert warnings == []
        assert rows[0].claimed == "admitted"
        assert rows[0].file_path == "Foo.lean"

    def test_invalid_symbol_rejected_with_warning(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        for cell in ("`foo bar`", "`foo#print`", "`«x»`", "bare_no_backticks"):
            rows, warnings = parse_coverage_matrix(
                _matrix(f"| Thm | {cell} | proven | Foo.lean |"), root)
            assert rows == [], cell
            assert len(warnings) == 1 and "skipped" in warnings[0], cell

    def test_traversal_and_absolute_paths_rejected(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        outside = tmp_path.parent / "outside.lean"
        outside.write_text("theorem y : True := trivial\n")
        for path in ("../outside.lean", "/etc/passwd", "Foo\\Bar.lean"):
            rows, warnings = parse_coverage_matrix(
                _matrix(f"| Thm | `foo` | proven | {path} |"), root)
            assert rows == [], path
            assert len(warnings) == 1, path

    def test_symlink_path_rejected(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        outside = tmp_path.parent / "target.lean"
        outside.write_text("theorem z : True := trivial\n")
        os.symlink(outside, tmp_path / "Link.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `foo` | proven | Link.lean |"), root)
        assert rows == []
        assert len(warnings) == 1

    def test_missing_file_rejected(self, tmp_path):
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `foo` | proven | Nope.lean |"), str(tmp_path))
        assert rows == [] and len(warnings) == 1

    def test_per_cell_semantics_symbol_and_path_never_set_status(self, tmp_path):
        # `foo_proven_iff` in the symbol cell and Proofs/Sorry.lean in the
        # path cell must not influence the status read.
        root = _mk_repo(tmp_path, "Proofs/Sorry.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `Ns.foo_proven_iff` | sorry | Proofs/Sorry.lean |"), root)
        assert warnings == []
        assert rows[0].claimed == "admitted"

    def test_negated_status_is_ambiguous(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `foo` | not proven | Foo.lean |"), root)
        assert rows == []
        assert "ambiguous" in warnings[0]

    def test_both_status_classes_in_one_cell_ambiguous(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `foo` | complete modulo sorry | Foo.lean |"), root)
        assert rows == []
        assert "ambiguous" in warnings[0]

    def test_emoji_status_unsupported_and_warned(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `foo` | ✅ | Foo.lean |"), root)
        assert rows == []
        assert "no recognized status keyword" in warnings[0]

    def test_status_word_boundary(self, tmp_path):
        # 'unproven' must not match 'proven'.
        root = _mk_repo(tmp_path, "Foo.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `foo` | unproven | Foo.lean |"), root)
        assert rows == []
        assert "no recognized status keyword" in warnings[0]

    def test_row_cap_truncates_with_warning(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        lines = [f"| T{i} | `sym{i}` | proven | Foo.lean |" for i in range(MAX_MATRIX_ROWS + 5)]
        rows, warnings = parse_coverage_matrix(_matrix(*lines), root)
        assert len(rows) == MAX_MATRIX_ROWS
        assert any("front-load" in w for w in warnings)

    def test_non_table_text_ignored(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        text = "# Title\n\nProse mentioning proven and sorry.\n\n" + _matrix(
            "| Thm | `foo` | proven | Foo.lean |")
        rows, warnings = parse_coverage_matrix(text, root)
        assert len(rows) == 1 and warnings == []


class _FakeLean:
    """Fake at the run_lean_command seam with real-format outputs."""

    def __init__(self, monkeypatch, outputs):
        # outputs: dict decl -> output template (or None for runner failure)
        self.outputs = outputs
        self.calls = []

        def fake(mod, cmd, timeout=30, env=None):
            self.calls.append((mod, cmd))
            decl = cmd.removeprefix("#print axioms ")
            template = self.outputs.get(decl)
            return None if template is None else template.format(d=decl)

        monkeypatch.setattr(lean_info_extractor, "run_lean_command", fake)


def _row(symbol, claimed, file_path="Foo.lean", module="Foo", line_no=3):
    return MatrixRow(symbol=symbol, file_path=file_path, module=module,
                     claimed=claimed, line_no=line_no)


class TestCheckCoverageMatrix:
    def test_sorry_claimed_proven_is_status_mismatch(self, monkeypatch):
        _FakeLean(monkeypatch, {"Ns.foo": OUT_SORRY})
        findings, warnings, notes = check_coverage_matrix([_row("Ns.foo", "proven")])
        assert [f.kind for f in findings] == ["status-mismatch"]
        assert "sorryAx" in findings[0].evidence
        assert warnings == [] and notes == []

    def test_sorry_claimed_admitted_is_clean(self, monkeypatch):
        _FakeLean(monkeypatch, {"Ns.foo": OUT_SORRY})
        findings, warnings, notes = check_coverage_matrix([_row("Ns.foo", "admitted")])
        assert findings == [] and warnings == [] and notes == []

    def test_clean_claimed_proven_is_clean(self, monkeypatch):
        _FakeLean(monkeypatch, {"Ns.foo": OUT_CLEAN, "Ns.bar": OUT_NO_AXIOMS})
        findings, warnings, notes = check_coverage_matrix(
            [_row("Ns.foo", "proven"), _row("Ns.bar", "proven")])
        assert findings == [] and warnings == [] and notes == []

    def test_stale_admitted_claim_is_note_not_finding(self, monkeypatch):
        _FakeLean(monkeypatch, {"Ns.foo": OUT_CLEAN})
        findings, warnings, notes = check_coverage_matrix([_row("Ns.foo", "admitted")])
        assert findings == [] and warnings == []
        assert len(notes) == 1 and "ready to flip" in notes[0]

    def test_unknown_constant_is_unresolved_finding(self, monkeypatch):
        for template in (OUT_UNKNOWN, OUT_UNKNOWN_OLD):
            _FakeLean(monkeypatch, {"Ghost.thm": template})
            findings, warnings, notes = check_coverage_matrix([_row("Ghost.thm", "proven")])
            assert [f.kind for f in findings] == ["unresolved-symbol"], template
            assert findings[0].blocking is False

    def test_import_failure_is_warning_never_finding(self, monkeypatch):
        _FakeLean(monkeypatch, {"Ns.foo": OUT_IMPORT_FAIL})
        findings, warnings, notes = check_coverage_matrix([_row("Ns.foo", "proven")])
        assert findings == []
        assert len(warnings) == 1 and "did not complete" in warnings[0]

    def test_runner_failure_is_warning_never_finding(self, monkeypatch):
        _FakeLean(monkeypatch, {"Ns.foo": None})
        findings, warnings, notes = check_coverage_matrix([_row("Ns.foo", "proven")])
        assert findings == []
        assert len(warnings) == 1

    def test_rows_grouped_by_module_single_extractor_call(self, monkeypatch):
        fake = _FakeLean(monkeypatch, {"Ns.a": OUT_CLEAN, "Ns.b": OUT_CLEAN})
        check_coverage_matrix([
            _row("Ns.a", "proven", module="Foo"),
            _row("Ns.b", "proven", module="Foo"),
        ])
        assert all(mod == "Foo" for mod, _ in fake.calls)
        assert len(fake.calls) == 2

    def test_deadline_before_spawn_truncates_with_warning(self):
        def boom(*a, **k):
            raise AssertionError("extractor must not run past the deadline")

        findings, warnings, notes = check_coverage_matrix(
            [_row("Ns.a", "proven"), _row("Ns.b", "proven", module="Bar")],
            deadline=100.0,
            clock=lambda: 200.0,
            extractor=boom,
        )
        assert findings == []
        assert any("budget exhausted" in w for w in warnings)
        assert any("2 row(s)" in w for w in warnings)


class TestCoverageFindingDefaults:
    def test_blocking_starts_false(self):
        f = CoverageFinding(kind="status-mismatch", row=_row("x", "proven"), evidence="e")
        assert f.blocking is False


# --- Phase-3 review regressions (B2/N1/N2/N3) ---

class TestParserReviewRegressions:
    def test_non_normalized_paths_rejected(self, tmp_path):
        # B2: `Fri/./Spec.lean` / `Fri//Spec.lean` would derive a broken
        # module AND never match git-diff changed-file keys (D1 evasion).
        root = _mk_repo(tmp_path, "Fri/Spec.lean")
        for path in ("Fri/./Spec.lean", "Fri//Spec.lean", "Fri/../Fri/Spec.lean"):
            rows, warnings = parse_coverage_matrix(
                _matrix(f"| Thm | `Ns.foo` | proven | {path} |"), root)
            assert rows == [], path
            assert any("normalized" in w for w in warnings), path

    def test_exact_status_cell_beats_description_prose(self, tmp_path):
        # N1: prose like 'as stated in Thm 4' in the description column must
        # not veto a row whose dedicated status cell says 'proven'.
        root = _mk_repo(tmp_path, "Fri/Spec.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Soundness as stated in Thm 4 | `Ns.foo` | proven | Fri/Spec.lean |"),
            root)
        assert warnings == []
        assert len(rows) == 1 and rows[0].claimed == "proven"

    def test_exact_cells_still_conflict_check(self, tmp_path):
        root = _mk_repo(tmp_path, "Foo.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `foo` | proven | sorry | Foo.lean |"), root)
        assert rows == []
        assert any("ambiguous" in w for w in warnings)

    def test_fenced_example_rows_ignored(self, tmp_path):
        # N2: documentation examples in ``` fences are not claims.
        root = _mk_repo(tmp_path, "Foo.lean")
        text = (
            "Example format:\n\n```\n| Thm | `Fake.sym` | proven | Foo.lean |\n```\n\n"
            + _matrix("| Thm | `Real.sym` | proven | Foo.lean |")
        )
        rows, warnings = parse_coverage_matrix(text, root)
        assert warnings == []
        assert [r.symbol for r in rows] == ["Real.sym"]

    def test_two_tables_both_headers_dropped_no_row_eaten(self, tmp_path):
        # N2: per-table header detection — the second table's first data row
        # must not be eaten as a "header".
        root = _mk_repo(tmp_path, "Foo.lean")
        text = (
            _matrix("| T1 | `Sym.one` | proven | Foo.lean |")
            + "\nSome prose.\n\n"
            + _matrix("| T2 | `Sym.two` | sorry | Foo.lean |")
        )
        rows, warnings = parse_coverage_matrix(text, root)
        assert warnings == []
        assert [r.symbol for r in rows] == ["Sym.one", "Sym.two"]

    def test_module_name_held_to_identifier_allowlist(self, tmp_path):
        # N3: a committed filename with a space would interpolate `import A b`.
        root = _mk_repo(tmp_path, "A b.lean")
        rows, warnings = parse_coverage_matrix(
            _matrix("| Thm | `Ns.foo` | proven | A b.lean |"), root)
        assert rows == []
        assert any("valid Lean module name" in w for w in warnings)
