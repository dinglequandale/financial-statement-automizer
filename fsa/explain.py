"""Findings, in the words of the person who has to act on them.

A finding carries a code, a severity and a precise technical message. That is
right for the log and wrong for the analyst: `extraction_unreliable` and
`period_basis_mismatch` describe what the code concluded, not what the person
in front of the screen should now do about it.

Each entry below answers three questions in the order an analyst asks them:

    what happened   in a sentence, with no jargon and no code names
    why it matters  what it would do to the valuation if ignored
    what to do      the next physical action, not an abstraction

Anything without an entry falls through to its own technical message, which is
worse but never wrong. New codes are therefore safe to add without touching
this file; they just read like a developer wrote them until someone does.
"""

from __future__ import annotations

from dataclasses import dataclass

from fsa.model.schema import Finding, Severity, ValidationReport


@dataclass(frozen=True)
class Explanation:
    headline: str
    why: str
    action: str


#: Keyed on finding code. Present tense, second person, no code names.
EXPLANATIONS: dict[str, Explanation] = {
    "extraction_unreliable": Explanation(
        "A year's figures could not be read correctly",
        "The client's own subtotals do not add up from the lines we read, which "
        "means something on the page was missed or misaligned. Any model built "
        "on these figures would be wrong.",
        "Open the client's file for the year named above and check it against "
        "the numbers in the report. Usually a column is laid out differently "
        "from the other years. Send the file on if you cannot see it.",
    ),
    "period_basis_mismatch": Explanation(
        "One year covers a different period from the others",
        "A March or June statement sitting among December year ends is a part "
        "year, not another year. Trending it or reading working capital off it "
        "misstates the valuation.",
        "Ask the client for the matching year end, or leave that period out and "
        "run again.",
    ),
    "partial_period": Explanation(
        "A period covers less than a full year",
        "Mixing a part year with full years makes revenue and expenses look "
        "smaller than they are.",
        "Use the full-year statement for this year, or exclude the period.",
    ),
    "entity_scope_ambiguous": Explanation(
        "Several companies' figures are in one file",
        "The file holds more than one set of books for the same year and it is "
        "not clear which one is being valued. Picking the wrong one values a "
        "subsidiary instead of the group.",
        "Decide which company you are valuing and enter its name in the "
        "Reporting entity box, exactly as the tab is labelled.",
    ),
    "scale_not_as_reported": Explanation(
        "Figures are stated in thousands or millions",
        "This tool writes figures exactly as the client reported them. Left "
        "alone, the model would be out by a factor of a thousand or more.",
        "Ask the client for statements in whole dollars, or convert them before "
        "running this.",
    ),
    "scale_mixed": Explanation(
        "Years are not stated in the same units",
        "One year is in thousands and another in whole dollars. Every subtotal "
        "still adds up, so nothing else will catch this.",
        "Put every year on the same footing before running again.",
    ),
    "currency_mixed": Explanation(
        "Figures are reported in more than one currency",
        "Adding one currency to another needs an exchange rate, which is a "
        "judgement call for the engagement rather than something to assume.",
        "Supply statements already translated into a single currency.",
    ),
    "period_unreadable": Explanation(
        "A statement does not say what period it covers",
        "Without a date we cannot tell which year the figures belong to, and "
        "guessing would file them under the wrong one.",
        "Check the top of that statement for a date. If it is missing, ask the "
        "client which period it covers.",
    ),
    "balance_sheet_unbalanced": Explanation(
        "A balance sheet does not balance",
        "Assets do not equal liabilities plus equity, so either a line is "
        "missing or one was read incorrectly.",
        "Compare the totals in the report against the client's file for that "
        "year.",
    ),
    "no_statements_found": Explanation(
        "A file held no balance sheet or income statement",
        "It was skipped. Often this is correct -- a cash flow statement, a "
        "cover letter or a schedule -- but it is worth a glance.",
        "If that file should have contained a statement, check it opens "
        "normally and is not a scan.",
    ),
    "source_unreadable": Explanation(
        "A file could not be opened",
        "Scanned PDFs have no text to read, only a picture of one.",
        "Ask the client for the original spreadsheet or a text-based PDF.",
    ),
    "double_count": Explanation(
        "The same money would be counted twice",
        "Both a total and the accounts inside it were mapped to the model, "
        "which would double the amount.",
        "This was resolved automatically. The line that was dropped appears in "
        "the review sheet marked as excluded -- overtype it if the other choice "
        "was the right one.",
    ),
    "llm_unavailable": Explanation(
        "The suggestion service was unavailable",
        "Everything else ran. You will have more rows to fill in by hand than "
        "usual.",
        "Check the internet connection and the account balance, then run again "
        "if you would rather have the suggestions.",
    ),
    "period_inferred": Explanation(
        "A period was worked out rather than read",
        "The statement did not print its period plainly, so it was taken from a "
        "sheet tab, a file name or the wording around it. That is usually right "
        "and occasionally not.",
        "Check the years listed are the ones you expect before relying on them.",
    ),
    "uncovered_accounts": Explanation(
        "Some accounts are not accounted for anywhere",
        "Every account should end up either mapped to a line of the model or "
        "deliberately excluded. These are neither.",
        "They appear at the top of the review sheet in red. Give each one a "
        "line, or mark it excluded.",
    ),
    "period_unverified": Explanation(
        "A year's reporting date could not be confirmed",
        "The figures were read, but nothing proves they cover a full year "
        "ending when the other years end.",
        "Confirm those years are full years before relying on the model.",
    ),
    "period_length_unverified": Explanation(
        "A year's length could not be confirmed",
        "The date is known but not how many months it covers, so a part year "
        "here would not be spotted.",
        "Confirm those years cover twelve months.",
    ),
}

#: Codes that describe ordinary bookkeeping drift rather than a problem. They
#: are still written to the report; they just do not belong in a summary that
#: is meant to be read.
ROUTINE = frozenset(
    {
        # Superseded by `extraction_unreliable`, which groups the material ones
        # per year and says what to do. Listing both means the analyst reads the
        # same problem twice, once in English and once in arithmetic.
        "subtotal_mismatch",
        "hierarchy_mismatch",
        "account_added",
        "account_removed",
        "account_renamed",
        "duplicate_label_qualified",
        "label_truncated_at_source",
        "duplicate_page_skipped",
        "pages_joined",
        "entity_scope_selected",
        "period_end_from_balance_sheet",
        "extractor_declined",
        "comparative_disagreement",
    }
)


def _humanize(code: str) -> str:
    """`period_basis_mismatch` -> `Period basis mismatch`.

    A readable stand-in for a code nobody has written an entry for yet. Better
    than showing one finding's message as the heading for a group of them.
    """
    return (code or "note").replace("_", " ").strip().capitalize()


def explain(finding: Finding) -> Explanation:
    known = EXPLANATIONS.get(finding.code)
    if known is not None:
        return known
    return Explanation(_humanize(finding.code), "", "")


@dataclass
class Summary:
    """What to show someone who has just pressed the button."""

    blocking: list[tuple[Explanation, list[Finding]]]
    worth_a_look: list[tuple[Explanation, list[Finding]]]
    routine_count: int

    @property
    def ok(self) -> bool:
        return not self.blocking


def _group(findings: list[Finding]) -> list[tuple[Explanation, list[Finding]]]:
    order: list[str] = []
    by_code: dict[str, list[Finding]] = {}
    for f in findings:
        if f.code not in by_code:
            order.append(f.code)
            by_code[f.code] = []
        by_code[f.code].append(f)
    return [(explain(by_code[c][0]), by_code[c]) for c in order]


def summarize(report: ValidationReport) -> Summary:
    """Split a report into what stops the job, what to glance at, and noise."""
    blocking = [f for f in report.findings if f.severity is Severity.ERROR]
    look = [
        f
        for f in report.findings
        if f.severity is Severity.WARNING and f.code not in ROUTINE
    ]
    routine = [
        f
        for f in report.findings
        if f.severity is not Severity.ERROR and f.code in ROUTINE
    ]
    return Summary(_group(blocking), _group(look), len(routine))


def render_text(summary: Summary, width: int = 78) -> str:
    """The same summary as plain text, for the terminal and for log files."""
    out: list[str] = []

    def block(title: str, groups) -> None:
        if not groups:
            return
        out.append("")
        out.append(title)
        out.append("-" * min(width, len(title)))
        for exp, findings in groups:
            out.append("")
            out.append(f"  {exp.headline}")
            for f in findings[:4]:
                # Most messages already name their statement and year; only add
                # a prefix when the message does not carry the location itself.
                year = str(f.fiscal_year) if f.fiscal_year else ""
                needs = year and year not in f.message
                prefix = f"{f.statement.value if f.statement else ''} {year}: ".lstrip() if needs else ""
                out.append(f"      - {prefix}{f.message}")
            if len(findings) > 4:
                out.append(f"      - ... and {len(findings) - 4} more")
            if exp.why:
                out.append(f"    Why it matters: {exp.why}")
            if exp.action:
                out.append(f"    What to do:     {exp.action}")

    block("STOPPED -- these must be resolved before the model can be trusted",
          summary.blocking)
    block("WORTH A LOOK", summary.worth_a_look)
    if summary.routine_count:
        out.append("")
        out.append(
            f"({summary.routine_count} routine notes about accounts added, removed or "
            f"renamed since last year -- all recorded in the report file.)"
        )
    return "\n".join(out)
