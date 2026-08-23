"""Decide which column actually represents a year, when several claim it.

A client workbook does not always hold one statement per year. Sample 3 ships,
for every year, a `consolidated` tab beside `US`, `Canada` and `Bermuda` tabs --
four balance sheets, one fiscal year. `StatementSet.get()` returns the first
match, so before this module existed the client's FY2024 balance sheet was a
single subsidiary's, chosen by worksheet ordering, with nothing said about it.

Two rules, both of which report rather than assume:

* **Scope.** Prefer the tab that names itself a roll-up. The word list below is
  a *default that orders the options*, not a decision: when it does not resolve
  the clash the module says so loudly and leaves the choice to the analyst,
  because which entity is being valued is the first question of an engagement
  and not one a filename should answer. The list is allowed to be incomplete
  precisely because being wrong costs a question, not a wrong model.

* **Basis.** Columns in one statement must share a year end. A group that
  reports to December every year and then hands over a March balance sheet has
  not given us FY2025 -- it has given us a stub, and averaging it into a trend
  or reading working capital off it misstates the valuation (PLAN 2.6).
"""

from __future__ import annotations

import re

from fsa.model.schema import (
    ColumnRole,
    ExtractedColumn,
    Finding,
    Severity,
    StatementSet,
    StatementType,
)

#: Names a tab gives itself when it is the whole group rather than one part of
#: it. Deliberately short: an unmatched roll-up costs a question to the
#: analyst, whereas a wrongly matched one costs a wrong valuation.
_ROLLUP = re.compile(
    r"\b(consolidat\w*|combined|combining|group|total|all\s+entities|"
    r"parent|holdco|hold\s*co)\b",
    re.I,
)

#: A tab named after the *statement* rather than after a place or a legal
#: entity is the statement -- sample 3's 2022 workbook calls its roll-up
#: `Profit and Loss` while its components are `US` and `Bermuda`. This is a
#: structural observation, not more roll-up vocabulary: `US` and `Bermuda` name
#: who is reporting, `Profit and Loss` names what is being reported.
_STATEMENT_NAMED = re.compile(
    r"\b(profit\s*(and|&)?\s*loss|p\s*&\s*l|income\s+statement|balance\s+sheet|"
    r"statement\s+of\s+(operations|income|earnings|financial\s+position))\b",
    re.I,
)


def looks_like_rollup(name: str | None) -> bool:
    if not name:
        return False
    return bool(_ROLLUP.search(name) or _STATEMENT_NAMED.search(name))


def merge_pages(ss: StatementSet) -> list[Finding]:
    """Join columns that are pages of one statement, not rival scopes.

    A PDF balance sheet split over two pages arrives as two columns for the
    same year, distinguished only by `p1`/`p2`. Treating those as competing
    entities would make the reader choose one and silently drop half the
    statement -- which is what `StatementSet.get()` did before anything looked.
    Pages are identified by carrying no entity and sharing a source file.
    """
    findings: list[Finding] = []
    groups: dict[tuple, list[ExtractedColumn]] = {}
    for c in ss.columns:
        if c.role is ColumnRole.PRIMARY and c.entity is None:
            groups.setdefault((c.statement, c.fiscal_year, c.source_file), []).append(c)

    drop: set[int] = set()
    for (st, year, src), cols in groups.items():
        if len(cols) < 2:
            continue
        keep, rest = cols[0], cols[1:]
        for c in rest:
            keep.rows.extend(c.rows)
            drop.add(id(c))
        findings.append(
            Finding(
                severity=Severity.INFO,
                code="pages_joined",
                message=(
                    f"{st.value} FY{year}: {src.name} spans {len(cols)} pages; "
                    f"joined into one statement of {len(keep.rows)} rows."
                ),
                statement=st,
                fiscal_year=year,
            )
        )

    if drop:
        ss.columns = [c for c in ss.columns if id(c) not in drop]
    return findings


def _label(c: ExtractedColumn) -> str:
    """How to name a claimant so the analyst can tell them apart.

    A named tab identifies itself. A default one does not -- `Sheet1, Sheet1`
    tells nobody anything -- and in that case the claimants are really two
    *files*, which is what client five hit: a workbook of balance sheets and a
    file named `Profit and Loss ...` that turned out to contain balance sheets
    too, both claiming the same six years.
    """
    if c.entity:
        return c.entity
    return c.source_file.name if c.source_file else (c.source_sheet or "(unnamed)")


def select_scope(ss: StatementSet, preferred: str | None = None) -> list[Finding]:
    """Keep one PRIMARY column per (statement, year). Mutates `ss`.

    `preferred` is the analyst's answer to "which entity are we valuing?" and
    outranks every heuristic here -- naming a scope explicitly is the whole
    point, so it is honoured even when it is not a tab this module would have
    guessed. Returns findings describing every choice made and every clash
    that could not be resolved without a human.
    """
    findings: list[Finding] = []
    contested = ss.contested()
    if not contested:
        return findings

    want = (preferred or "").strip().casefold()
    drop: set[int] = set()
    for (st, year), cols in sorted(contested.items(), key=lambda kv: (kv[0][0].value, kv[0][1])):
        # Roll-up candidacy is judged on the *entity* a tab names, never on a
        # file name. Client five ships a file called `Profit and Loss Jan - Aug
        # 24 and 25.ods` that actually contains balance sheets; letting the
        # filename match the statement-named rule made it the winning "roll-up"
        # for six balance sheet years, silently, over the real workbook.
        rollups = [c for c in cols if looks_like_rollup(c.entity)]
        names = ", ".join(sorted(_label(c) for c in cols))

        asked = [c for c in cols if want and want in _label(c).casefold()] if want else []
        if len(asked) == 1:
            winner = asked[0]
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="entity_scope_selected",
                    message=(
                        f"{st.value} FY{year}: {len(cols)} sources claim this year "
                        f"({names}); used {_label(winner)!r} as requested."
                    ),
                    statement=st,
                    fiscal_year=year,
                    detail={"chosen": _label(winner), "requested": preferred},
                )
            )
            for c in cols:
                if c is not winner:
                    drop.add(id(c))
            continue

        if len(rollups) == 1:
            winner = rollups[0]
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="entity_scope_selected",
                    message=(
                        f"{st.value} FY{year}: {len(cols)} sources claim this year "
                        f"({names}); used {_label(winner)!r} as the reporting "
                        f"scope. Override with --scope if that is not the entity "
                        f"being valued."
                    ),
                    statement=st,
                    fiscal_year=year,
                    detail={"chosen": _label(winner), "candidates": sorted(_label(c) for c in cols)},
                )
            )
        else:
            winner = cols[0]
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="entity_scope_ambiguous",
                    message=(
                        f"{st.value} FY{year}: {len(cols)} sources claim this year "
                        f"({names}) and "
                        + (
                            f"{len(rollups)} of them look like roll-ups"
                            if rollups
                            else "none of them names itself a roll-up"
                        )
                        + f". Provisionally using {_label(winner)!r} -- confirm which "
                        f"entity is being valued before relying on this year."
                    ),
                    statement=st,
                    fiscal_year=year,
                    detail={"chosen": _label(winner), "candidates": sorted(_label(c) for c in cols)},
                )
            )

        for c in cols:
            if c is not winner:
                drop.add(id(c))

    if drop:
        ss.columns = [c for c in ss.columns if id(c) not in drop]
    return findings


def check_period_basis(ss: StatementSet) -> list[Finding]:
    """Every year of a statement should close on the same day of the year.

    A single odd year end is how an interim or trailing-twelve-month column
    enters a history of full years without anything noticing: its length can
    look perfectly annual while covering a completely different span.
    """
    findings: list[Finding] = []
    by_stmt: dict = {}
    for c in ss.columns:
        if c.role is ColumnRole.PRIMARY and c.period_end is not None:
            by_stmt.setdefault(c.statement, []).append(c)

    for st, cols in by_stmt.items():
        if len(cols) < 2:
            continue
        # Compared by *month*, not by exact day. Plenty of real clients close
        # on a weekday rather than a date -- TS Distributors' balance sheets
        # land on Dec 30 one year and Dec 1 another, which is a fiscal calendar
        # and not a change of basis. What matters is a year that closes in a
        # different month entirely: June against December is half a year of
        # trading, and March against December is a quarter of one.
        ends: dict[int, list[ExtractedColumn]] = {}
        for c in cols:
            ends.setdefault(c.period_end.month, []).append(c)
        if len(ends) == 1:
            continue
        majority = max(ends, key=lambda k: len(ends[k]))
        for month, odd in ends.items():
            if month == majority:
                continue
            for c in odd:
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code="period_basis_mismatch",
                        message=(
                            f"{st.value} FY{c.fiscal_year} ends {c.period_end} but every "
                            f"other year of this {st.value} closes in month "
                            f"{majority:02d}. That is a different reporting basis, not "
                            f"another year -- supply the matching year end or exclude "
                            f"this period."
                        ),
                        statement=st,
                        fiscal_year=c.fiscal_year,
                        detail={"period_end": str(c.period_end), "source": _label(c)},
                    )
                )

    for c in ss.columns:
        if c.role is ColumnRole.PRIMARY and c.is_partial:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="partial_period",
                    message=(
                        f"{c.statement.value} FY{c.fiscal_year} covers "
                        f"{c.period_months} month(s), not a full year. Mixing it with "
                        f"annual figures would misstate the valuation."
                    ),
                    statement=c.statement,
                    fiscal_year=c.fiscal_year,
                    detail={"months": c.period_months, "source": _label(c)},
                )
            )
    return findings


def infer_missing_period_ends(ss: StatementSet) -> list[Finding]:
    """Give an income statement the year end its own balance sheet reports.

    The multi-column extractor reads balance sheet columns from real date cells
    but identifies income statement columns by a `YTD <year>` header, which
    names a year and no date -- so `discover.py` records `period_end=None` and
    nothing downstream ever fills it in. On TS Distributors that left five of
    ten columns with no period at all, and because `check_period_basis` skips
    columns it cannot date, the absence read as a clean bill of health.

    A company's income statement covers the year ending on its balance sheet
    date, so the date is recoverable from the client's own figures rather than
    from any vocabulary: take it from the balance sheet for the same fiscal
    year, preferring the same workbook. Length stays unknown -- `YTD` says
    where a period ended, never how long it ran -- and `check_period_coverage`
    reports that honestly instead of assuming twelve months.
    """
    findings: list[Finding] = []
    bs_by_year: dict[int, list[ExtractedColumn]] = {}
    for c in ss.columns:
        if (
            c.role is ColumnRole.PRIMARY
            and c.statement is StatementType.BS
            and c.period_end is not None
        ):
            bs_by_year.setdefault(c.fiscal_year, []).append(c)

    for c in ss.columns:
        if c.role is not ColumnRole.PRIMARY or c.period_end is not None:
            continue
        if c.statement is not StatementType.IS:
            continue
        siblings = bs_by_year.get(c.fiscal_year)
        if not siblings:
            continue
        same_file = [b for b in siblings if b.source_file == c.source_file]
        donor = (same_file or siblings)[0]
        c.period_end = donor.period_end
        findings.append(
            Finding(
                severity=Severity.INFO,
                code="period_end_from_balance_sheet",
                message=(
                    f"IS FY{c.fiscal_year} carried no period date; took "
                    f"{donor.period_end} from the balance sheet for the same year "
                    f"({donor.source_file.name}). Its length is still unknown."
                ),
                statement=c.statement,
                fiscal_year=c.fiscal_year,
            )
        )
    return findings


def check_period_coverage(ss: StatementSet) -> list[Finding]:
    """Report columns whose period could not be established at all.

    A column with no period end is not a column that passed its checks -- it is
    one that was skipped by them, because every period test needs a date to
    compare. Saying nothing about it is how "unverified" comes to look exactly
    like "verified", which is the failure mode this whole layer exists to
    remove. So say it out loud, per statement, once.
    """
    findings: list[Finding] = []
    undated: dict[StatementType, list[ExtractedColumn]] = {}
    unlengthed: dict[StatementType, list[ExtractedColumn]] = {}

    for c in ss.columns:
        if c.role is not ColumnRole.PRIMARY:
            continue
        if c.period_end is None:
            undated.setdefault(c.statement, []).append(c)
        elif c.statement is StatementType.IS and c.period_months is None:
            unlengthed.setdefault(c.statement, []).append(c)

    for st, cols in undated.items():
        years = ", ".join(str(c.fiscal_year) for c in sorted(cols, key=lambda x: x.fiscal_year))
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="period_unverified",
                message=(
                    f"{st.value} {years}: no reporting date could be established, so "
                    f"these years were not checked for a changed year end or a stub "
                    f"period. Confirm they are full years before relying on them."
                ),
                statement=st,
            )
        )

    for st, cols in unlengthed.items():
        years = ", ".join(str(c.fiscal_year) for c in sorted(cols, key=lambda x: x.fiscal_year))
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="period_length_unverified",
                message=(
                    f"{st.value} {years}: the reporting date is known but the period "
                    f"length is not, so a stub or trailing-twelve-month column here "
                    f"would not be detected. Confirm they cover full years."
                ),
                statement=st,
            )
        )
    return findings


def check_denomination(ss: StatementSet) -> list[Finding]:
    """Refuse figures this tool cannot write as reported.

    Two failures, neither of which any arithmetic can see. A statement stated
    in thousands ties every subtotal and balances every year; so does a column
    of Canadian dollars added to a group reporting in US dollars. The only
    evidence is the line of small type above the columns, which is why the
    declaration is part of a column's identity rather than a formatting detail.

    The response is refusal, not conversion. Rescaling would mean this tool
    silently changing the client's figures, and translating would mean picking
    an exchange rate -- a valuation judgement, made per engagement, by an
    analyst. Both cost one question. Guessing either costs a valuation.
    """
    findings: list[Finding] = []
    primary = [c for c in ss.columns if c.role is ColumnRole.PRIMARY]

    stated = [c for c in primary if c.scale != 1]
    if stated:
        worst = stated[0]
        years = ", ".join(
            f"{c.statement.value} FY{c.fiscal_year}"
            for c in sorted(stated, key=lambda x: (x.statement.value, x.fiscal_year))
        )
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code="scale_not_as_reported",
                message=(
                    f"figures are stated in {worst.scale_label} ({years}). This tool "
                    f"writes figures exactly as the client reported them and does not "
                    f"rescale, so the model would be out by a factor of "
                    f"{worst.scale:,}. Supply statements stated in whole units, or "
                    f"rescale them before running."
                ),
                statement=worst.statement,
                fiscal_year=worst.fiscal_year,
                detail={"scale": worst.scale, "label": worst.scale_label},
            )
        )

    by_stmt: dict[StatementType, list[ExtractedColumn]] = {}
    for c in primary:
        by_stmt.setdefault(c.statement, []).append(c)

    for st, cols in by_stmt.items():
        scales = {c.scale for c in cols}
        if len(scales) > 1:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="scale_mixed",
                    message=(
                        f"{st.value}: years are not stated in the same units "
                        f"({sorted(scales)}). Comparing or trending them would be "
                        f"wrong by orders of magnitude, and every subtotal still "
                        f"ties, so nothing else will catch it."
                    ),
                    statement=st,
                )
            )

        currencies = {c.currency for c in cols if c.currency}
        if len(currencies) > 1:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="currency_mixed",
                    message=(
                        f"{st.value}: figures are reported in more than one currency "
                        f"({', '.join(sorted(currencies))}). Translating them is a "
                        f"valuation judgement and an exchange rate this tool does not "
                        f"have -- supply statements already translated to one currency."
                    ),
                    statement=st,
                    detail={"currencies": sorted(currencies)},
                )
            )
    return findings
