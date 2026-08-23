"""Surveying a folder before anything expensive runs.

The tool already detects two sources claiming one year -- but only after
ingest, mapping and a round of suggestions. That is the right answer arriving
after the costly part, and it makes the analyst pay for a whole second run to
act on something that was visible from the outset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fsa.scan import FileScan, mark_overlaps


def fake(name, claims, size=100.0):
    return FileScan(path=Path(name), size_kb=size, claims=claims)


def link(scans):
    """Sort as `scan_folder` does, then mark overlaps."""
    return mark_overlaps(sorted(scans, key=lambda s: s.path.name))


# --------------------------------------------------------------------------
# what a file says it holds
# --------------------------------------------------------------------------


def test_a_span_of_years_reads_as_a_span():
    assert fake("a.xlsx", {"BS": [2019, 2020, 2021]}).summary == "BS 2019-2021"


def test_a_single_year_is_not_written_as_a_span():
    assert fake("a.xlsx", {"BS": [2024]}).summary == "BS 2024"


def test_both_statements_are_listed():
    assert fake("a.xlsx", {"BS": [2024], "IS": [2024]}).summary == "BS 2024, IS 2024"


def test_a_file_holding_neither_says_so():
    """Ram Rod sends a cash flow statement in its own file."""
    assert "no balance sheet or income statement" in fake("cf.xlsx", {}).summary


def test_an_unreadable_file_reports_that_rather_than_pretending():
    s = fake("scan.pdf", {})
    s.error = "could not be read (UnreadableSource)"
    assert s.summary == "could not be read (UnreadableSource)"


# --------------------------------------------------------------------------
# overlaps: the thing worth knowing before a run
# --------------------------------------------------------------------------


def test_two_files_claiming_one_year_are_both_flagged():
    """TS Distributors keeps Weaver's working consolidation beside the
    client's own statements, and both cover FY2025."""
    scans = link([
        fake("Dec 2020 -2025 (Weaver edited).xlsx", {"BS": [2025], "IS": [2025]}),
        fake("Dec 2025 Financials.xlsx", {"BS": [2025], "IS": [2025]}),
    ])
    assert all(s.overlaps for s in scans)
    assert "Dec 2025 Financials.xlsx" in scans[0].overlaps[0]
    assert "Weaver edited" in scans[1].overlaps[0]


def test_an_overlap_is_stated_once_per_file_not_once_per_year():
    """Client five's two balance sheet sources share six years each."""
    scans = link([
        fake("Balance Sheet 2019 to 2024.xlsx", {"BS": list(range(2019, 2025))}),
        fake("Profit and Loss Jan - Aug 24 and 25.xlsx", {"BS": list(range(2019, 2025))}),
    ])
    for s in scans:
        assert len(s.overlaps) == 1
        assert "BS 2019-2024 also in" in s.overlaps[0]


def test_files_covering_different_years_do_not_overlap():
    scans = link([
        fake("2023.xlsx", {"BS": [2023], "IS": [2023]}),
        fake("2024.xlsx", {"BS": [2024], "IS": [2024]}),
    ])
    assert all(not s.overlaps for s in scans)


def test_the_same_year_of_different_statements_is_not_an_overlap():
    """One file for the balance sheet, another for the income statement."""
    scans = link([
        fake("2022 BS.xlsx", {"BS": [2022]}),
        fake("2022 IS.xlsx", {"IS": [2022]}),
    ])
    assert all(not s.overlaps for s in scans)


def test_three_claimants_each_name_the_others():
    scans = link([
        fake("a.xlsx", {"BS": [2024]}),
        fake("b.xlsx", {"BS": [2024]}),
        fake("c.xlsx", {"BS": [2024]}),
    ])
    for s in scans:
        assert len(s.overlaps) == 2


# --------------------------------------------------------------------------
# the survey must stay cheap and safe
# --------------------------------------------------------------------------


def test_scanning_never_reaches_for_a_model(monkeypatch):
    """A survey that tells you what is in a folder must not spend money."""
    import fsa.ingest.period as period

    def explode(*a, **k):
        raise AssertionError("the folder survey must not call a model")

    monkeypatch.setattr(period.PeriodResolver, "_ask", explode)
    from fsa.scan import scan_file

    scan_file(Path("Konrad Project/sample_4_Ram Rod/Ram Rod/"
                   "Source Financial Statements from Client/2022 BS.xlsx"))


def test_an_unreadable_file_does_not_break_the_survey(tmp_path):
    bad = tmp_path / "not-really.xlsx"
    bad.write_bytes(b"this is not a workbook")
    from fsa.scan import scan_file

    out = scan_file(bad)
    assert out.error is not None
    assert out.claims == {}
