"""Unit tests for fsa.validate.report and fsa.cli.

No real Excel files are touched. End-to-end CLI tests fake out
fsa.ingest.extract / fsa.ingest.discover via sys.modules injection so the
lazy `from fsa.ingest.extract import ...` inside fsa.cli resolves to
synthetic, in-memory data instead of ever touching the filesystem for a
workbook.
"""

from __future__ import annotations

import csv
import json
import sys
import types
from pathlib import Path

import openpyxl
import pytest

from fsa import cli
from fsa.ingest.normalize import normalize
from fsa.model.schema import (
    AccountRow,
    CellRef,
    ColumnRole,
    ConsolidatedRow,
    ConsolidatedTable,
    ExtractedColumn,
    Finding,
    RowKind,
    Severity,
    StatementType,
    ValidationReport,
)
from fsa.validate.report import render_json, render_text

FILE = Path("dummy.xlsx")


def _row(label, value, idx, kind=RowKind.DATA, section="Current Assets"):
    return AccountRow(
        raw_label=label,
        norm_label=normalize(label),
        kind=kind,
        value=value,
        section=section,
        row_index=idx,
        ref=CellRef(file=FILE, sheet="Balance Sheet", cell=f"C{idx}"),
    )


def _column(year, rows, statement=StatementType.BS, role=ColumnRole.PRIMARY):
    return ExtractedColumn(
        statement=statement,
        fiscal_year=year,
        role=role,
        source_file=FILE,
        source_sheet="Balance Sheet",
        value_column="C",
        label_column="A",
        rows=rows,
    )


def _crow(label, values):
    return ConsolidatedRow(raw_label=label, norm_label=normalize(label), kind=RowKind.DATA, section=None, values=values)


# --------------------------------------------------------------------------
# render_text
# --------------------------------------------------------------------------


def test_render_text_ok_verdict():
    report = ValidationReport()
    table = {StatementType.BS: ConsolidatedTable(statement=StatementType.BS, years=[2023], rows=[])}
    text = render_text(report, table)
    assert text.startswith("OK")


def test_render_text_error_warning_counts():
    report = ValidationReport()
    report.add(Finding(severity=Severity.ERROR, code="balance_sheet_unbalanced", message="boom"))
    report.add(Finding(severity=Severity.WARNING, code="value_delta", message="warn 1"))
    report.add(Finding(severity=Severity.WARNING, code="value_delta", message="warn 2"))
    table = {StatementType.BS: ConsolidatedTable(statement=StatementType.BS, years=[2023], rows=[])}
    text = render_text(report, table)
    assert text.startswith("1 error(s), 2 warning(s)")


def test_render_text_errors_before_warnings():
    report = ValidationReport()
    report.add(Finding(severity=Severity.WARNING, code="value_delta", message="a warning"))
    report.add(Finding(severity=Severity.ERROR, code="balance_sheet_unbalanced", message="an error"))
    table = {}
    text = render_text(report, table)
    assert text.index("ERROR") < text.index("WARNING")
    assert text.index("an error") < text.index("a warning")


def test_render_text_is_ascii_even_with_unicode_input():
    report = ValidationReport()
    report.add(
        Finding(
            severity=Severity.WARNING,
            code="value_delta",
            message="value differs → by ’a lot’ — check it",
            account="Café Account",
        )
    )
    table = {}
    text = render_text(report, table)
    assert all(ord(c) < 128 for c in text)
    # Must not raise when encoded as the console's cp1252.
    text.encode("cp1252")


def test_render_text_names_account_and_cell():
    ref = CellRef(file=FILE, sheet="Balance Sheet", cell="C42")
    report = ValidationReport()
    report.add(
        Finding(
            severity=Severity.ERROR,
            code="balance_sheet_unbalanced",
            message="does not tie",
            account="Total Assets",
            ref=ref,
            fiscal_year=2023,
            statement=StatementType.BS,
        )
    )
    text = render_text(report, {})
    assert "Total Assets" in text
    assert "C42" in text
    assert "FY2023" in text


def test_render_text_summary_reflects_table():
    report = ValidationReport()
    table = {
        StatementType.BS: ConsolidatedTable(
            statement=StatementType.BS,
            years=[2021, 2022, 2023],
            rows=[_crow("Cash", {2021: 1.0, 2022: 2.0, 2023: 3.0})],
        )
    }
    text = render_text(report, table)
    assert "BS: years 2021-2023, 1 accounts" in text


# --------------------------------------------------------------------------
# render_json
# --------------------------------------------------------------------------


def test_render_json_structure_and_roundtrip():
    report = ValidationReport()
    ref = CellRef(file=FILE, sheet="Balance Sheet", cell="C10")
    report.add(
        Finding(
            severity=Severity.ERROR,
            code="row_alignment_suspected",
            message="shift detected",
            statement=StatementType.BS,
            fiscal_year=2023,
            account="Certificate of Deposit",
            ref=ref,
            detail={"length": 11, "shift": -1},
        )
    )
    table = {
        StatementType.BS: ConsolidatedTable(
            statement=StatementType.BS,
            years=[2023],
            rows=[_crow("Cash", {2023: 100.0}), _crow("Certificate of Deposit", {2023: None})],
        )
    }
    text = render_json(report, table)
    payload = json.loads(text)  # must be valid JSON

    assert payload["verdict"] == "error"
    assert payload["error_count"] == 1
    assert payload["warning_count"] == 0
    assert len(payload["findings"]) == 1
    f = payload["findings"][0]
    assert f["code"] == "row_alignment_suspected"
    assert f["fiscal_year"] == 2023
    assert f["ref"]["cell"] == "C10"
    assert f["detail"]["length"] == 11

    bs = payload["tables"]["BS"]
    assert bs["years"] == [2023]
    labels = {r["label"] for r in bs["rows"]}
    assert labels == {"Cash", "Certificate of Deposit"}
    cash_row = next(r for r in bs["rows"] if r["label"] == "Cash")
    assert cash_row["values"]["2023"] == 100.0


def test_render_json_ok_verdict():
    report = ValidationReport()
    text = render_json(report, {})
    payload = json.loads(text)
    assert payload["verdict"] == "ok"


# --------------------------------------------------------------------------
# CLI argument parsing
# --------------------------------------------------------------------------


def test_build_parser_ingest_flags():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "ingest",
            "somedir",
            "--exclude",
            "a.xlsx",
            "--exclude",
            "b.xlsx",
            "--audit-against",
            "ref.xlsx",
            "--json",
            "out.json",
            "--csv-dir",
            "outdir",
            "-v",
        ]
    )
    assert args.paths == ["somedir"]
    assert args.exclude == ["a.xlsx", "b.xlsx"]
    assert args.audit_against == "ref.xlsx"
    assert args.json == "out.json"
    assert args.csv_dir == "outdir"
    assert args.verbose is True


def test_build_parser_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["ingest", "somedir"])
    assert args.exclude == []
    assert args.audit_against is None
    assert args.json is None
    assert args.csv_dir is None
    assert args.verbose is False


# --------------------------------------------------------------------------
# _resolve_files
# --------------------------------------------------------------------------


def test_resolve_files_directory_excludes_and_lockfiles(tmp_path):
    (tmp_path / "Dec 2021 Financials.xlsx").write_text("x")
    (tmp_path / "Dec 2022 Financials.xlsx").write_text("x")
    (tmp_path / "Weaver edited.xlsx").write_text("x")
    (tmp_path / "~$Dec 2021 Financials.xlsx").write_text("x")  # Excel lockfile

    files = cli._resolve_files([str(tmp_path)], ["Weaver edited.xlsx"])
    names = sorted(f.name for f in files)
    assert names == ["Dec 2021 Financials.xlsx", "Dec 2022 Financials.xlsx"]


def test_resolve_files_explicit_file_list(tmp_path):
    f1 = tmp_path / "one.xlsx"
    f1.write_text("x")
    f2 = tmp_path / "two.xlsx"
    f2.write_text("x")

    files = cli._resolve_files([str(f1), str(f2)], ["two.xlsx"])
    assert [f.name for f in files] == ["one.xlsx"]


# --------------------------------------------------------------------------
# _write_csv
# --------------------------------------------------------------------------


def test_write_csv_blank_for_none(tmp_path):
    table = ConsolidatedTable(
        statement=StatementType.BS,
        years=[2022, 2023],
        rows=[
            _crow("Cash", {2022: 100.0, 2023: 110.0}),
            _crow("Certificate of Deposit", {2022: 3_000_000.0, 2023: None}),
        ],
    )
    cli._write_csv(tmp_path, table)
    out_file = tmp_path / "BS.csv"
    assert out_file.exists()

    with out_file.open(newline="", encoding="ascii") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["label", "section", "2022", "2023"]
    assert rows[1][0] == "Cash"
    assert rows[2][0] == "Certificate of Deposit"
    assert rows[2][3] == ""  # None -> blank, never "0"


# --------------------------------------------------------------------------
# End-to-end CLI, with the ingest layer faked out via sys.modules
# --------------------------------------------------------------------------


def _install_fake_extract(monkeypatch, columns_by_file):
    """Install a fake fsa.ingest.extract module returning canned
    ExtractedColumns for extract_workbook, keyed by Path."""
    fake_extract = types.ModuleType("fsa.ingest.extract")

    def extract_workbook(path, statements=(StatementType.BS, StatementType.IS)):
        return columns_by_file.get(Path(path).name, [])

    def extract_reference_grid(path, sheet, statement):
        raise AssertionError("extract_reference_grid should not be called without --audit-against")

    fake_extract.extract_workbook = extract_workbook
    fake_extract.extract_reference_grid = extract_reference_grid
    monkeypatch.setitem(sys.modules, "fsa.ingest.extract", fake_extract)
    return fake_extract


def test_cli_ingest_clean_data_exit_zero(tmp_path, monkeypatch, capsys):
    client_file = tmp_path / "Dec 2023 Financials.xlsx"
    client_file.write_text("x")

    columns = [
        _column(
            2023,
            [
                _row("Cash", 100.0, 10),
                _row("Total Assets", 100.0, 11, kind=RowKind.SUBTOTAL),
                _row("Accounts Payable", 40.0, 12),
                _row("Total Liabilities", 40.0, 13, kind=RowKind.SUBTOTAL),
                _row("Common Stock", 60.0, 14),
                _row("Total Owners Equity", 60.0, 15, kind=RowKind.SUBTOTAL),
            ],
        )
    ]
    _install_fake_extract(monkeypatch, {client_file.name: columns})

    exit_code = cli.main(["ingest", str(tmp_path)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert out.startswith("OK")
    assert all(ord(c) < 128 for c in out)


def test_cli_ingest_error_exit_one(tmp_path, monkeypatch, capsys):
    client_file = tmp_path / "Dec 2023 Financials.xlsx"
    client_file.write_text("x")

    columns = [
        _column(
            2023,
            [
                _row("Cash", 100.0, 10),
                _row("Total Assets", 100.0, 11, kind=RowKind.SUBTOTAL),
                _row("Total Liabilities", 40.0, 12, kind=RowKind.SUBTOTAL),
                _row("Total Owners Equity", 50.0, 13, kind=RowKind.SUBTOTAL),  # off by 10
            ],
        )
    ]
    _install_fake_extract(monkeypatch, {client_file.name: columns})

    exit_code = cli.main(["ingest", str(tmp_path)])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "error(s)" in out
    assert "balance_sheet_unbalanced" in out


def test_cli_verbose_shows_info_findings(tmp_path, monkeypatch, capsys):
    f2023 = tmp_path / "Dec 2023 Financials.xlsx"
    f2023.write_text("x")
    f2024 = tmp_path / "Dec 2024 Financials.xlsx"
    f2024.write_text("x")

    cols = {
        f2023.name: [_column(2023, [_row("Cash", 100.0, 10), _row("Certificate of Deposit", 3_000_000.0, 11)])],
        f2024.name: [_column(2024, [_row("Cash", 110.0, 10)])],
    }
    _install_fake_extract(monkeypatch, cols)

    exit_code = cli.main(["ingest", str(tmp_path)])
    out_quiet = capsys.readouterr().out
    assert exit_code == 0
    assert "account_removed" not in out_quiet

    exit_code = cli.main(["ingest", str(tmp_path), "-v"])
    out_verbose = capsys.readouterr().out
    assert exit_code == 0
    assert "account_removed" in out_verbose


def test_cli_writes_json_and_csv(tmp_path, monkeypatch, capsys):
    client_file = tmp_path / "Dec 2023 Financials.xlsx"
    client_file.write_text("x")
    columns = [_column(2023, [_row("Cash", 100.0, 10)])]
    _install_fake_extract(monkeypatch, {client_file.name: columns})

    json_out = tmp_path / "report.json"
    csv_dir = tmp_path / "csvs"

    exit_code = cli.main(
        ["ingest", str(tmp_path), "--json", str(json_out), "--csv-dir", str(csv_dir)]
    )
    capsys.readouterr()

    assert exit_code == 0
    assert json_out.exists()
    payload = json.loads(json_out.read_text(encoding="ascii"))
    assert payload["verdict"] == "ok"

    assert (csv_dir / "BS.csv").exists()
    assert (csv_dir / "IS.csv").exists()


def test_cli_audit_against_invokes_audit_checks(tmp_path, monkeypatch, capsys):
    client_file = tmp_path / "Dec 2023 Financials.xlsx"
    client_file.write_text("x")
    audit_file = tmp_path / "Weaver.xlsx"
    audit_file.write_text("x")

    ours_rows = [_row("Cash", 100.0, 10), _row("Certificate of Deposit", 0.0, 11)]
    columns = [_column(2023, ours_rows)]
    _install_fake_extract(monkeypatch, {client_file.name: columns})

    # Reference table: "Certificate of Deposit" position holds Cash's value
    # (100.0) instead of its own (0.0) -- a single shifted row, too short to
    # qualify as a run (MIN_RUN_LENGTH=3), so it should surface as value_delta.
    reference_table = ConsolidatedTable(
        statement=StatementType.BS,
        years=[2023],
        rows=[
            _crow("Cash", {2023: 100.0}),
            _crow("Certificate of Deposit", {2023: 100.0}),
        ],
    )

    fake_discover = types.ModuleType("fsa.ingest.discover")

    def find_statement_sheet(wb, statement):
        return "Balance Sheets" if statement is StatementType.BS else None

    fake_discover.find_statement_sheet = find_statement_sheet
    monkeypatch.setitem(sys.modules, "fsa.ingest.discover", fake_discover)

    fake_extract = sys.modules["fsa.ingest.extract"]

    def extract_reference_grid(path, sheet, statement):
        assert statement is StatementType.BS
        return reference_table

    fake_extract.extract_reference_grid = extract_reference_grid

    monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: object())

    exit_code = cli.main(["ingest", str(tmp_path), "--audit-against", str(audit_file), "-v"])
    out = capsys.readouterr().out

    assert exit_code == 0  # value_delta is a WARNING, not an ERROR
    assert "value_delta" in out
    assert "Certificate of Deposit" in out
