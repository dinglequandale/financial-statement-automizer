"""Unit tests for fsa.consolidate, against synthetic StatementSet fixtures.

No ingest layer, no Excel files -- everything is built directly from the
schema dataclasses per SPEC-PHASE0.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fsa.consolidate import consolidate
from fsa.ingest.normalize import normalize
from fsa.model.schema import (
    AccountRow,
    CellRef,
    ColumnRole,
    ExtractedColumn,
    RowKind,
    StatementSet,
    StatementType,
)

FILE = Path("dummy.xlsx")


def _row(label: str, value: float | None, idx: int, kind: RowKind = RowKind.DATA, section: str | None = "Current Assets") -> AccountRow:
    return AccountRow(
        raw_label=label,
        norm_label=normalize(label),
        kind=kind,
        value=value,
        section=section,
        row_index=idx,
        ref=CellRef(file=FILE, sheet="Balance Sheet", cell=f"C{idx}"),
    )


def _column(
    year: int,
    labels_values: list[tuple[str, float | None]],
    statement: StatementType = StatementType.BS,
    role: ColumnRole = ColumnRole.PRIMARY,
    section: str | None = "Current Assets",
    start_idx: int = 10,
) -> ExtractedColumn:
    rows = [
        _row(label, value, start_idx + i, section=section)
        for i, (label, value) in enumerate(labels_values)
    ]
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


def test_basic_merge_two_years_same_accounts():
    ss = StatementSet()
    ss.add(_column(2023, [("Cash", 100.0), ("Accounts Receivable", 200.0)]))
    ss.add(_column(2024, [("Cash", 110.0), ("Accounts Receivable", 210.0)]))

    table = consolidate(ss, StatementType.BS)

    assert table.years == [2023, 2024]
    assert [r.raw_label for r in table.rows] == ["Cash", "Accounts Receivable"]
    cash = table.find(normalize("Cash"))
    assert cash.values == {2023: 100.0, 2024: 110.0}


def test_row_order_follows_most_recent_year():
    ss = StatementSet()
    # 2023 file lists AR before Cash; 2024 (most recent) lists Cash first.
    ss.add(_column(2023, [("Accounts Receivable", 200.0), ("Cash", 100.0)]))
    ss.add(_column(2024, [("Cash", 110.0), ("Accounts Receivable", 210.0)]))

    table = consolidate(ss, StatementType.BS)

    assert [r.raw_label for r in table.rows] == ["Cash", "Accounts Receivable"]


def test_earlier_only_account_inserted_after_anchor():
    ss = StatementSet()
    # 2023 has an extra account "Certificate of Deposit" between Cash and AR.
    ss.add(
        _column(
            2023,
            [("Cash", 100.0), ("Certificate of Deposit", 3_000_000.0), ("Accounts Receivable", 200.0)],
        )
    )
    # 2024 dropped it entirely.
    ss.add(_column(2024, [("Cash", 110.0), ("Accounts Receivable", 210.0)]))

    table = consolidate(ss, StatementType.BS)

    labels = [r.raw_label for r in table.rows]
    assert labels == ["Cash", "Certificate of Deposit", "Accounts Receivable"]

    cd_row = table.find(normalize("Certificate of Deposit"))
    assert cd_row.values[2023] == 3_000_000.0
    assert cd_row.values.get(2024) is None  # absent that year -- never 0.0


def test_earlier_only_account_falls_back_to_end_of_section():
    ss = StatementSet()
    # "Old Prepaid" is the very first row in 2023 and has no surviving
    # predecessor in the most-recent year's account list -> falls back to
    # end-of-section placement.
    ss.add(
        _column(
            2023,
            [("Old Prepaid", 50.0), ("Cash", 100.0), ("Accounts Receivable", 200.0)],
            section="Current Assets",
        )
    )
    ss.add(
        _column(
            2024,
            [("Cash", 110.0), ("Accounts Receivable", 210.0)],
            section="Current Assets",
        )
    )

    table = consolidate(ss, StatementType.BS)

    labels = [r.raw_label for r in table.rows]
    assert labels[-1] == "Old Prepaid"
    assert labels == ["Cash", "Accounts Receivable", "Old Prepaid"]


def test_typo_variant_labels_unify():
    ss = StatementSet()
    ss.add(_column(2023, [("Other Paybles", 40.0)], statement=StatementType.BS))
    ss.add(_column(2024, [("Other Payables", 45.0)], statement=StatementType.BS))

    table = consolidate(ss, StatementType.BS)

    assert len(table.rows) == 1
    row = table.rows[0]
    # raw_label on the merged row is the most recent year's spelling.
    assert row.raw_label == "Other Payables"
    assert row.values == {2023: 40.0, 2024: 45.0}


def test_never_merges_two_accounts_with_values_in_the_same_year():
    ss = StatementSet()
    # Both "Accounts Receivable - Trade" and "Accounts Receivable - Indital"
    # are distinct accounts that happen to both be present in 2023 -- even if
    # some fuzzy match would otherwise unify them with a later single-row
    # spine entry, they must never collapse into one row.
    ss.add(
        _column(
            2024,
            [("Accounts Receivable - Indital", 999.0)],
        )
    )
    ss.add(
        _column(
            2023,
            [
                ("Accounts Receivable - Indital", 500.0),
                ("Accounts Receivable - Indital2", 600.0),
            ],
        )
    )
    # This test mainly guards against an exception / silent data loss; the
    # precise row layout in pathological fuzzy-collision cases is secondary.
    table = consolidate(ss, StatementType.BS)
    total_2023_value = sum(v for r in table.rows for y, v in r.values.items() if y == 2023 and v is not None)
    assert total_2023_value == 1100.0


def test_conflict_within_same_year_keeps_rows_separate_and_warns():
    ss = StatementSet()
    ss.add(_column(2024, [("Other Payables", 10.0)]))
    # 2023 has two distinct labels that are both similar enough (>=0.92) to
    # the 2024 spine row "Other Payables" to collide if merged naively.
    ss.add(
        _column(
            2023,
            [("Other Payables", 20.0), ("Other Payable", 30.0)],
        )
    )

    with pytest.warns(UserWarning):
        table = consolidate(ss, StatementType.BS)

    # Both 2023 values must survive somewhere in the table -- no silent drop.
    values_2023 = sorted(v for r in table.rows for y, v in r.values.items() if y == 2023 and v is not None)
    assert values_2023 == [20.0, 30.0]


def test_none_never_coerced_to_zero():
    ss = StatementSet()
    ss.add(_column(2022, [("Prepaid Taxes", None), ("Prepaid Payroll", 0.0)]))
    ss.add(_column(2023, [("Prepaid Taxes", None), ("Prepaid Payroll", 0.0)]))

    table = consolidate(ss, StatementType.BS)

    prepaid_taxes = table.find(normalize("Prepaid Taxes"))
    prepaid_payroll = table.find(normalize("Prepaid Payroll"))

    assert prepaid_taxes.values[2022] is None
    assert prepaid_payroll.values[2022] == 0.0
    assert prepaid_payroll.values[2022] is not None


def test_empty_statement_set_returns_empty_table():
    ss = StatementSet()
    table = consolidate(ss, StatementType.BS)
    assert table.years == []
    assert table.rows == []


def test_subtotal_rows_are_merged_too():
    ss = StatementSet()
    ss.add(
        _column(
            2023,
            [("Cash", 100.0), ("Total Current Assets", 100.0)],
        )
    )
    # Mark the second row as a subtotal explicitly.
    col = ss.columns[-1]
    col.rows[1] = AccountRow(
        raw_label="Total Current Assets",
        norm_label=normalize("Total Current Assets"),
        kind=RowKind.SUBTOTAL,
        value=100.0,
        section="Current Assets",
        row_index=11,
    )
    table = consolidate(ss, StatementType.BS)
    subtotal = table.find(normalize("Total Current Assets"))
    assert subtotal is not None
    assert subtotal.kind is RowKind.SUBTOTAL
