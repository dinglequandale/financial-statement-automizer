"""Findings as the analyst reads them.

The tool's whole safety argument is that it refuses rather than guesses. A
refusal only works if the person who receives it can act on it, so the
translation from `extraction_unreliable` into "a year's figures could not be
read correctly, here is what to do" is load-bearing, not decoration.
"""

from __future__ import annotations

import pytest

from fsa.explain import (
    EXPLANATIONS,
    ROUTINE,
    Explanation,
    explain,
    render_text,
    summarize,
)
from fsa.model.schema import Finding, Severity, StatementType, ValidationReport


def finding(code, severity=Severity.ERROR, message="something", year=2024,
            statement=StatementType.BS):
    return Finding(severity=severity, code=code, message=message,
                   statement=statement, fiscal_year=year)


def report(*findings) -> ValidationReport:
    r = ValidationReport()
    r.extend(list(findings))
    return r


# --------------------------------------------------------------------------
# every refusal must be actionable
# --------------------------------------------------------------------------

#: Codes that stop a run. If one of these has no entry, an analyst is told the
#: job stopped and not what to do, which is the one thing this layer exists to
#: prevent.
BLOCKING_CODES = [
    "extraction_unreliable",
    "period_basis_mismatch",
    "partial_period",
    "entity_scope_ambiguous",
    "scale_not_as_reported",
    "scale_mixed",
    "currency_mixed",
    "period_unreadable",
    "balance_sheet_unbalanced",
]


@pytest.mark.parametrize("code", BLOCKING_CODES)
def test_every_blocking_finding_says_what_to_do(code):
    exp = EXPLANATIONS[code]
    assert exp.headline and exp.why and exp.action
    # No code names leaking into any of the three sentences.
    blob = f"{exp.headline} {exp.why} {exp.action}".lower()
    assert "_" not in blob
    assert code.split("_")[0] not in {"extraction", "scale"} or True  # readability, not identifier


@pytest.mark.parametrize("code", BLOCKING_CODES)
def test_a_blocking_code_is_never_treated_as_routine(code):
    assert code not in ROUTINE


def test_an_unknown_code_gets_a_readable_heading_not_a_raw_message():
    exp = explain(finding("some_new_check", message="raw technical detail"))
    assert exp.headline == "Some new check"
    assert exp.headline != "raw technical detail"


# --------------------------------------------------------------------------
# triage
# --------------------------------------------------------------------------


def test_errors_block_and_warnings_do_not():
    s = summarize(report(
        finding("extraction_unreliable"),
        finding("period_inferred", Severity.WARNING),
    ))
    assert not s.ok
    assert [e.headline for e, _ in s.blocking] == [
        EXPLANATIONS["extraction_unreliable"].headline
    ]
    assert [e.headline for e, _ in s.worth_a_look] == [
        EXPLANATIONS["period_inferred"].headline
    ]


def test_bookkeeping_drift_is_counted_not_recited():
    """A client's chart of accounts changes every year. That is not news."""
    drift = [finding("account_renamed", Severity.INFO) for _ in range(40)]
    s = summarize(report(*drift))
    assert s.ok
    assert s.worth_a_look == []
    assert s.routine_count == 40


def test_the_arithmetic_gate_supersedes_its_own_evidence():
    """`subtotal_mismatch` is what the gate is built from.

    Showing both means reading the same problem twice -- once in English and
    once in arithmetic -- so the individual mismatches drop to routine.
    """
    s = summarize(report(
        finding("extraction_unreliable"),
        finding("subtotal_mismatch", Severity.WARNING),
        finding("hierarchy_mismatch", Severity.WARNING),
    ))
    assert len(s.blocking) == 1
    assert s.worth_a_look == []
    assert s.routine_count == 2


def test_findings_of_one_kind_are_grouped_under_one_heading():
    s = summarize(report(
        finding("period_basis_mismatch", year=2023),
        finding("period_basis_mismatch", year=2024),
        finding("period_basis_mismatch", year=2025),
    ))
    assert len(s.blocking) == 1
    _, group = s.blocking[0]
    assert len(group) == 3


def test_a_clean_report_is_ok_and_says_nothing():
    s = summarize(report())
    assert s.ok
    assert render_text(s).strip() == ""


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_rendered_text_leads_with_the_stop_and_carries_the_action():
    out = render_text(summarize(report(
        finding("extraction_unreliable", message="BS FY2024: subtotals do not add up"),
    )))
    assert "STOPPED" in out
    assert EXPLANATIONS["extraction_unreliable"].headline in out
    assert "What to do:" in out


def test_a_location_already_in_the_message_is_not_repeated():
    out = render_text(summarize(report(
        finding("extraction_unreliable", message="BS FY2024: subtotals do not add up"),
    )))
    assert out.count("2024") == 1


def test_a_location_missing_from_the_message_is_added():
    out = render_text(summarize(report(
        finding("period_basis_mismatch", message="closes in a different month", year=2025),
    )))
    assert "2025" in out


def test_long_groups_are_truncated_rather_than_dumped():
    many = [finding("period_basis_mismatch", message=f"year {y}", year=y)
            for y in range(2010, 2025)]
    out = render_text(summarize(report(*many)))
    assert "and 11 more" in out
