"""Tests for fsa.write.com_writer.

Pure-helper tests (column mapping, year-to-column logic, report assembly) run
everywhere and require no Excel installation. Anything that opens a real
workbook through COM is marked `@pytest.mark.sample` and skips cleanly when
Excel/COM is unavailable, per the project convention (see pytest.ini).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fsa.model.mapping import Decider
from fsa.model.schema import ConsolidatedRow, ConsolidatedTable, RowKind, StatementType
from fsa.write.com_writer import (
    ComWriterError,
    WriteReport,
    historical_column,
    historical_rows,
    std_year_column,
    year_column_plan,
)

BS = StatementType.BS
IS = StatementType.IS


# --------------------------------------------------------------------------
# std_year_column
# --------------------------------------------------------------------------


def test_std_year_column_full_seven_years_spans_e_to_k():
    assert [std_year_column(i, 7) for i in range(7)] == list("EFGHIJK")


def test_std_year_column_right_aligns_to_k():
    # 6 years of history (TS Distributors' actual shape) -> F..K, matching
    # the delivered model exactly (BS!F9 = 'Historical BS'!B, ..., K9 = !G).
    assert [std_year_column(i, 6) for i in range(6)] == list("FGHIJK")


def test_std_year_column_single_year_lands_on_k():
    assert std_year_column(0, 1) == "K"


def test_std_year_column_rejects_out_of_range_index():
    with pytest.raises(ComWriterError):
        std_year_column(6, 6)
    with pytest.raises(ComWriterError):
        std_year_column(-1, 6)


def test_std_year_column_rejects_too_many_years():
    with pytest.raises(ComWriterError):
        std_year_column(0, 8)


def test_std_year_column_rejects_non_positive_n_years():
    with pytest.raises(ComWriterError):
        std_year_column(0, 0)


# --------------------------------------------------------------------------
# historical_column
# --------------------------------------------------------------------------


def test_historical_column_starts_at_b():
    assert historical_column(0) == "B"
    assert historical_column(5) == "G"


def test_historical_column_rejects_negative_index():
    with pytest.raises(ComWriterError):
        historical_column(-1)


# --------------------------------------------------------------------------
# historical_rows
# --------------------------------------------------------------------------


def _row(label: str, kind: RowKind = RowKind.DATA) -> ConsolidatedRow:
    return ConsolidatedRow(raw_label=label, norm_label=label.lower(), kind=kind, section=None)


def test_historical_rows_maps_in_table_order_starting_row_2():
    table = ConsolidatedTable(
        statement=BS,
        years=[2023, 2024],
        rows=[_row("Cash"), _row("Accounts Receivable"), _row("Inventory")],
    )
    rows = historical_rows(table)
    assert rows == {"cash": 2, "accounts receivable": 3, "inventory": 4}


def test_historical_rows_custom_start_row():
    table = ConsolidatedTable(statement=BS, years=[2023], rows=[_row("Cash")])
    assert historical_rows(table, start_row=5) == {"cash": 5}


def test_historical_rows_empty_table():
    table = ConsolidatedTable(statement=BS, years=[], rows=[])
    assert historical_rows(table) == {}


# --------------------------------------------------------------------------
# year_column_plan
# --------------------------------------------------------------------------


def test_year_column_plan_normal_case_maps_every_year():
    mapping, warnings = year_column_plan([2021, 2022, 2023, 2024, 2025])
    assert warnings == []
    assert mapping == {
        2021: ("B", "G"),
        2022: ("C", "H"),
        2023: ("D", "I"),
        2024: ("E", "J"),
        2025: ("F", "K"),
    }


def test_year_column_plan_sorts_unordered_input():
    mapping, _ = year_column_plan([2023, 2021, 2022])
    assert list(mapping.keys()) == [2021, 2022, 2023]


def test_year_column_plan_full_seven_periods_starts_at_e():
    mapping, warnings = year_column_plan(list(range(2019, 2026)))  # 7 years
    assert warnings == []
    assert mapping[2019] == ("B", "E")
    assert mapping[2025] == ("H", "K")


def test_year_column_plan_overflow_drops_oldest_years_and_warns():
    years = list(range(2015, 2026))  # 11 years, only 7 fit
    mapping, warnings = year_column_plan(years)
    assert len(warnings) == 1
    assert "4 year(s)" in warnings[0]
    assert set(mapping.keys()) == set(range(2019, 2026))
    # historical columns still cover every year, including dropped ones,
    # relative to the full ascending sequence
    assert mapping[2019][0] == historical_column(4)


def test_year_column_plan_empty_years():
    mapping, warnings = year_column_plan([])
    assert mapping == {}
    assert warnings == []


# --------------------------------------------------------------------------
# WriteReport
# --------------------------------------------------------------------------


def test_write_report_defaults():
    report = WriteReport(out_path=Path("out/model.xlsx"))
    assert report.out_path == Path("out/model.xlsx")
    assert report.hist_rows == {}
    assert report.n_inserts == {}
    assert report.n_renames == {}
    assert report.n_repurposes == {}
    assert report.balance_check == {}
    assert report.warnings == []
    assert report.balanced is True


def test_write_report_fields_are_independent_between_instances():
    # dataclass mutable-default pitfall check
    a = WriteReport(out_path=Path("a.xlsx"))
    b = WriteReport(out_path=Path("b.xlsx"))
    a.warnings.append("uh oh")
    a.hist_rows[BS] = {"cash": 2}
    assert b.warnings == []
    assert b.hist_rows == {}


# --------------------------------------------------------------------------
# @pytest.mark.sample -- real Excel COM, real sample data
# --------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE_DIR = _PROJECT_ROOT / "Konrad Project" / "sample_1_TS"
_TEMPLATE = _SAMPLE_DIR / "BVAL Model (Weaver Template).xlsx"
_CLIENT_DIR = _SAMPLE_DIR / "TS Distributors Financial Statements (from client)"
_EXCLUDE = "Dec 2020 -2025 Financias (Weaver edited).xlsx"


def _excel_available() -> bool:
    try:
        import pythoncom
        import win32com.client as win32
    except ImportError:
        return False
    try:
        pythoncom.CoInitialize()
        app = win32.DispatchEx("Excel.Application")
        app.Quit()
        del app
        return True
    except Exception:
        return False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


@pytest.mark.sample
def test_write_model_end_to_end_ts_distributors(tmp_path):
    if not _SAMPLE_DIR.exists():
        pytest.skip("sample data not present")
    if not _excel_available():
        pytest.skip("Excel/COM not available in this environment")

    from fsa.consolidate import consolidate
    from fsa.ingest.extract import extract_workbook
    from fsa.mapping.matcher import propose
    from fsa.mapping.template import load_template
    from fsa.model.schema import StatementSet
    from fsa.write.com_writer import write_model
    from fsa.write.plan import build_plan

    ss = StatementSet()
    for f in sorted(_CLIENT_DIR.glob("*.xlsx")):
        if f.name == _EXCLUDE or f.name.startswith("~$"):
            continue
        for col in extract_workbook(f, statements=(BS, IS)):
            ss.add(col)

    tables = {BS: consolidate(ss, BS), IS: consolidate(ss, IS)}
    spec = load_template(_TEMPLATE)

    plans = {}
    for statement, table in tables.items():
        ms = propose(table, spec)
        for rule in ms.rules:
            if rule.decided_by is not Decider.UNRESOLVED:
                rule.decided_by = Decider.HUMAN
        hist_rows = historical_rows(table)
        try:
            plans[statement] = build_plan(ms, spec, hist_rows)
        except Exception:
            continue  # a statement with nothing confirmed is not fatal here

    assert plans, "expected at least one statement to produce a write plan"

    out = tmp_path / "TS Distributors - written.xlsx"
    report = write_model(
        _TEMPLATE,
        out,
        tables,
        plans,
        entity_name="TS Distributors, Inc.",
        valuation_date=date(2025, 12, 31),
    )

    assert out.exists()

    # Deterministic-only mapping (propose() with no LLM, no analyst filling
    # gaps) leaves most of BS/IS genuinely unresolved on this client -- PLAN.md
    # documents ~58-62% deterministic coverage, and matcher.py *by design*
    # never auto-resolves a client subtotal (e.g. "Total Owners Equity"),
    # which is the only thing that would populate BS's entire Equity block
    # here. So the balance check reliably does NOT tie under this input, and
    # asserting `report.balanced` would test the mapping engine's coverage,
    # not the writer. What IS this module's responsibility -- and what a
    # fuller, ground-truth-completed run (see the manual verification script)
    # confirms converges the balance check to zero -- is that every mapping
    # actually written resolves cleanly: no #REF! anywhere, and the balance
    # check mechanism itself produces a real number per year, not an error.
    assert not any("formula errors" in w for w in report.warnings)
    assert set(report.balance_check) == set(tables[BS].years)
    assert all(isinstance(v, float) for v in report.balance_check.values())


def test_write_model_refuses_to_overwrite_without_flag(tmp_path):
    # Pure error-handling path: write_model raises before ever touching Excel
    # (the check happens before the template is copied or COM is opened), so
    # this needs neither a real template nor a real Excel installation.
    from fsa.write.com_writer import write_model

    template = tmp_path / "template.xlsx"
    template.write_text("stand-in template")
    out = tmp_path / "existing.xlsx"
    out.write_text("not a real workbook")

    with pytest.raises(ComWriterError):
        write_model(template, out, {}, {})


def test_write_model_raises_when_template_missing(tmp_path):
    from fsa.write.com_writer import write_model

    with pytest.raises(ComWriterError):
        write_model(tmp_path / "nope.xlsx", tmp_path / "out.xlsx", {}, {})
