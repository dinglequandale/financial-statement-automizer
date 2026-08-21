# Phase 0 Implementation Spec — Ingest + Validate

Read `PLAN.md` §1, §2 and §6 first for context. This document is the build contract.

**Phase 0 writes nothing to Excel.** It reads client workbooks, merges them into an aligned
multi-year grid, and reports what it found. No COM, no template, no mapping.

## Acceptance criterion

```
python -m fsa.cli ingest "Konrad Project/Konrad Project/TS Distributors Financial Statements (from client)" \
    --exclude "Dec 2020 -2025 Financias (Weaver edited).xlsx" \
    --audit-against "Konrad Project/Konrad Project/TS Distributors Financial Statements (from client)/Dec 2020 -2025 Financias (Weaver edited).xlsx"
```

must, with **zero manual configuration**:
1. Ingest the five single-year client workbooks (FY2021–FY2025).
2. Emit a correct multi-year consolidated table for BS and IS.
3. Pass cross-year validation (each file's comparative column agrees with the prior file's primary column).
4. In audit mode, flag the FY2022 and FY2023 row misalignments in Weaver's hand-built consolidation, and report the FY2025 differences separately as value-level deltas rather than alignment faults.

## Environment

- Python 3.12, Windows. Deps: `openpyxl`, `pytest`. No `pandas`, no `rapidfuzz` (Phase 0 uses `difflib` via `fsa.ingest.normalize`).
- Always `openpyxl.load_workbook(path, data_only=True)` — client files contain formulas whose cached values are what we want.
- `load_workbook` emits `UserWarning` about unsupported conditional-formatting/data-validation extensions. Suppress with `warnings.catch_warnings()` around the load; do not let it reach the CLI output.
- Console output must be ASCII-safe. Windows consoles here default to cp1252 and **will** raise `UnicodeEncodeError` on box-drawing characters or arrows. Use plain ASCII in all printed output.

## Already built — do not modify

- `fsa/model/schema.py` — all dataclasses/enums. This is the contract. If you need a field that isn't there, add it in a backward-compatible way and say so in your report.
- `fsa/ingest/normalize.py` — `normalize()`, `similar()`, `similarity()`, `is_total_label()`.

---

## Verified file-format facts

These were confirmed by direct inspection of the sample files. Encode them as *defaults with detection*, never as hardcoded constants.

### Balance sheet — client single-year file

Sheet name: `Balance Sheet` (note: Weaver's consolidation uses the plural `Balance Sheets`). Detect by trying, in order: exact `Balance Sheet`, exact `Balance Sheets`, then any sheet whose normalized name contains `balance sheet`.

| Element | Location | Notes |
|---|---|---|
| Entity name | `A1` | e.g. `TS Distributors, Inc.` |
| Statement title | `A2` | `Balance Sheet` |
| Period caption | `A3` | `As of December 31, 2024` **or** `For the month Ending December 31, 2021` — wording varies |
| Date header row | row 5 | date cells mark the value columns |
| Labels | col **A** | |
| Current-year values | col **C** | |
| Prior-year comparative | col **E** | |
| % change | col **G** | never extract |

**Dates are unreliable by a day or two.** FY2024's header date is `2024-12-30`, not `2024-12-31`. Match on **year only**; never on exact date.

**Columns beyond G may contain Weaver's scratch work.** `Dec 2021 Financials.xlsx` has helper values in cols I and K (partial roll-ups Weaver typed while consolidating). Only ever extract columns whose header row cell parses as a date.

### Balance sheet — Weaver's reference consolidation

Sheet `Balance Sheets`. Labels col **A**, six value columns **B..G** = FY2020..FY2025, date header row 5 (`2020-12-01` .. `2025-12-01` — first-of-month, so again match on year only).

### Income statement — client single-year file

Sheet name: `Consolidated IS`.

| Element | Location | Notes |
|---|---|---|
| Labels | col **A** | |
| Header row | row **7** | |
| Monthly columns | **B..Y** | `January`,`%`,`February`,`%`,… — **ignore all of these** |
| Current-year total | col **Z** | header is literally `YTD 2021` |
| Prior-year monthly | **AC..AZ** | headers like `January 2020` — ignore |
| Prior-year total | col **BA** | header `YTD 2020` |

**Detection rule for the IS: regex `^YTD\s+(\d{4})$` against the header row.** This yields exactly two matches per client file; the higher year is PRIMARY, the lower is COMPARATIVE. Do not hardcode Z/BA.

The IS files also contain per-location tabs (`Houston`, `Chicago`, `Austin`, `Prescott`, `Birmingham`, `Miami`, `Indital Houston`), `Trial Balance`, `Worksheet`, `SUBLEDGER REC`. **Ignore all of them in Phase 0.**

### Prior-period restatements — the client rewrites history

Measured across the sample set: file N's comparative column disagrees with file N-1's own
current-year column for **20 accounts in FY2021, 4 in FY2022, 8 in FY2023, 9 in FY2024**
(out of ~103 matched income-statement accounts each year). These are real restatements, not
extraction faults. Some are material:

| Account | Original filing | Restated in next year's filing |
|---|---|---|
| Total Salaries & Wages (FY2023) | 9,937,524.83 | 9,787,524.83 |
| Total Overhead (FY2023) | 14,443,013.39 | 14,293,013.39 |
| Cost of Goods Sold (FY2021) | 49,648,351.75 | 49,637,307.64 |

Weaver's delivered model uses the **original filing** for each year (`Historical IS!AC59` =
9,937,524.83). That matches our PRIMARY-column rule, so the rule stands — but the tool must
*surface* every restatement rather than silently pick, because which version to use is an analyst
judgement, not a mechanical one.

The balance sheet has one instance, and it is a genuine puzzle worth showing a human. `Dec 2021
Financials.xlsx` reports FY2021 `Certificate of Deposit` = 0 and `Accounts Receivable - Trade` =
4,105,570.88. `Dec 2022 Financials.xlsx`, whose comparative column has *identical row labels*,
reports those two values **swapped**. Weaver's consolidation puts the same $4.1M under a third
label, `Accounts Receivable - Indital`. Three documents, three classifications, one $4.1M balance.
Report it; never auto-resolve it.

Consequence for `comparative_disagreement`: it must be a WARNING. Making it an ERROR would mean
this client's files never pass, which is why the severity policy above draws the line where it does.

### Row classification

Apply in this order:

1. Label empty/whitespace -> `BLANK` (regardless of value; the IS uses `-` filler rows as separators).
2. Worksheet row < the date header row -> `TITLE`.
3. Label present, value cell empty or non-numeric -> `SECTION_HEADER`. Observed BS sections: `Assets`, `Current Assets`, `Fixed Assets`, `Other Assets`, `Liabilities`, `Current Liabilities`, `Long Term Liabilities`, `Owners Equity`. Observed IS sections: `REVENUE`, `COST OF GOODS SOLD`, `Salaries & Wage Expense`, and similar.
4. `is_total_label(label)` is True -> `SUBTOTAL`.
5. Otherwise -> `DATA`.

Every `DATA` and `SUBTOTAL` row carries the most recent `SECTION_HEADER` seen above it in `section`.

`is_total_label` is deliberately loose. Cross-check every candidate `SUBTOTAL`: does its value equal the sum of the `DATA` rows since the previous subtotal (tolerance 0.01)? If not, still classify as `SUBTOTAL` but emit an INFO finding — a mismatch usually means the client's own statement has nested subtotals.

---

## Module contracts

### Agent A — `fsa/ingest/`

#### `fsa/ingest/discover.py`

```python
@dataclass(frozen=True)
class ValueColumn:
    column: str          # Excel letter
    fiscal_year: int
    period_end: date | None
    header_raw: str | None

@dataclass(frozen=True)
class SheetLayout:
    sheet: str
    statement: StatementType
    label_column: str
    header_row: int
    value_columns: list[ValueColumn]   # ordered left to right
    entity_name: str | None
    period_caption: str | None

def find_statement_sheet(wb, statement: StatementType) -> str | None: ...
def analyze_layout(wb, sheet: str, statement: StatementType) -> SheetLayout: ...
```

`analyze_layout` must raise `LayoutError` (define it) with an actionable message when it cannot
identify a label column or any value column — never guess silently and never return an empty layout.

Label-column detection: the column with the highest count of non-numeric string cells below the
header row. In practice always `A`, but detect it.

#### `fsa/ingest/extract.py`

```python
def extract_workbook(path: Path, statements=(StatementType.BS, StatementType.IS)) -> list[ExtractedColumn]:
    """One client workbook -> its PRIMARY and COMPARATIVE columns."""

def extract_reference_grid(path: Path, sheet: str, statement: StatementType) -> ConsolidatedTable:
    """A multi-year grid (Weaver's consolidation) -> ConsolidatedTable, for audit mode."""
```

Role assignment: highest fiscal year = `PRIMARY`, all others = `COMPARATIVE`.
Populate `CellRef` on every extracted row. Blank numeric cells become `None`, not `0.0` — the
distinction matters (FY2022 `Prepaid Taxes` is genuinely blank, not zero).

Merge `extract_bs`/`extract_is` into this single module; the two differ only in layout, and
`analyze_layout` already abstracts that.

### Agent B — `fsa/consolidate.py`, `fsa/validate/`, `fsa/cli.py`

#### `fsa/consolidate.py`

```python
def consolidate(ss: StatementSet, statement: StatementType) -> ConsolidatedTable: ...
```

Merge PRIMARY columns across years into one grid, keyed on `norm_label`.

- Row order follows the **most recent** year's statement.
- Accounts appearing only in earlier years are inserted after the account that precedes them in
  their own year and is also present in the output. Falls back to end-of-section.
- Typo-variant labels (`similar()` at >= 0.92) unify into one row; record every raw spelling seen.
- `raw_label` on the merged row = the most recent year's spelling.
- A year where an account is absent gets `None`, not `0.0`.

**Never merge two accounts that both have a value in the same year** — that means they are distinct
accounts, not spelling variants. Emit a WARNING and keep them separate.

#### `fsa/validate/checks.py`

Each check is `(...) -> list[Finding]`. Stable `code` slugs, exactly as listed:

### Severity policy (settled after inspecting the real files — see "Prior-period restatements" below)

- **ERROR** means *our own output cannot be trusted*: unparseable layout, a balance sheet that
  does not balance. ERROR sets exit code 1 and blocks.
- **WARNING** means *the source documents disagree with each other and a human must decide*.
  It never blocks. The client restating a prior period is normal, not a fault.
- **INFO** means *something changed and we handled it*: accounts added, removed, renamed.

| code | severity | what it checks |
|---|---|---|
| `subtotal_mismatch` | WARNING | a client subtotal != sum of its DATA rows (tol 0.01) |
| `balance_sheet_unbalanced` | ERROR | Total Assets != Total Liabilities + Equity (tol 0.01) |
| `comparative_disagreement` | WARNING | file N's COMPARATIVE column != file N-1's PRIMARY, per account |
| `comparative_alignment_suspected` | WARNING | those disagreements form a positional shift or swap |
| `account_added` | INFO | account present this year, absent prior |
| `account_removed` | INFO | account absent this year, present prior |
| `account_renamed` | WARNING | suspected rename (see below) |
| `row_alignment_suspected` | ERROR | audit mode: run of >= 3 consecutive accounts where the reference's value for account *i* equals our value for account *i±1* |
| `value_delta` | WARNING | audit mode: same account, different value, no alignment pattern |
| `unparsed_row` | WARNING | a row with a label and a numeric value that could not be classified |

Rename detection: account A vanishes in year N, account B appears in year N, neither matches an
existing pair, and `similarity(A,B) >= 0.6`. Report both labels and let a human decide.

**`row_alignment_suspected` is the headline check.** It must detect a *run*, not isolated
differences — that is exactly what distinguishes FY2022/FY2023 (systematic shift) from FY2025
(genuine adjustments). Report the run's extent, the shift direction, and the affected accounts.
Emit at most one finding per run, not one per row.

#### `fsa/validate/report.py`

```python
def render_text(report: ValidationReport, table: dict[StatementType, ConsolidatedTable]) -> str: ...
def render_json(report: ValidationReport, table: dict[StatementType, ConsolidatedTable]) -> str: ...
```

Text output: ASCII only, grouped by severity then statement then year, errors first. Lead with a
one-line verdict (`OK` / `N error(s), M warning(s)`). Findings must name the account and the source
cell so a reviewer can go straight to it.

#### `fsa/cli.py`

```
python -m fsa.cli ingest <dir-or-files...> [--exclude NAME]... [--audit-against FILE]
                        [--json OUT.json] [--csv-dir DIR] [-v]
```

Exit 0 when no ERROR findings, 1 otherwise. `--csv-dir` writes `BS.csv` / `IS.csv` of the
consolidated grid (years as columns) so a human can eyeball the result.

---

## Testing

`pytest`, tests under `tests/`.

- **Unit tests on synthetic fixtures** built in-memory with `openpyxl.Workbook()` — do not depend on
  the real client files for unit tests.
- **Mutation fixtures** covering the observed failure modes: dropped row, inserted row, renamed
  account, label typo, shifted comparative column, blank-vs-zero.
- **`tests/test_acceptance.py`** runs against the real sample files and asserts:
  - all five client workbooks ingest without ERROR findings,
  - `row_alignment_suspected` fires for FY2022 and FY2023 against Weaver's consolidation,
  - it does **not** fire for FY2020, FY2021, FY2024,
  - FY2025 produces `value_delta` findings, not `row_alignment_suspected`,
  - the FY2022 audit specifically identifies the `Certificate of Deposit` / `Accounts Receivable - Trade`
    pair (a $3,000,000 delta).
  Mark it `@pytest.mark.sample` and skip cleanly if the sample directory is absent.

Sample data lives at `Konrad Project/Konrad Project/` relative to the repo root. Treat it as
**read-only** — never write into it.

## Ground truth for the acceptance test

FY2023, client file vs Weaver's consolidation. Weaver's label for each value is one row *earlier*
than the client's, across rows 10-20; row 21 realigns:

| Client label | Value | Weaver's label for that value |
|---|---|---|
| Certificate of Deposit | 0 | Accounts Receivable - Trade |
| Accounts Receivable - Trade | 4,317,772.76 | Accounts Receivable - Indital |
| Accounts Receivable - Lawler | 0 | A/R terms - Allowed write off |
| A/R terms - Allowed write off | -10.06 | Allowance for doubtful accounts |
| Allowance for doubtful accounts | 0 | Other A/R |
| Other A/R | 2,407.53 | NSF Clearing Account |
| NSF Clearing Account | 21,020.25 | Notes Receivable |
| Notes Receivable | 25,547.44 | Employee Advances |
| Employee Advances | 4,703.16 | Inventory |
| Inventory | 16,881,097.15 | Inventory - Offsite |
| Inventory - Offsite | -790,884.25 | Inventory - Indital Beg Balance Variance |
| Inventory - Capitalized Sec 236A | 196,412.62 | Inventory - Capitalized Sec 236A (realigned) |

FY2022: same fault, offset begins after row 11. Weaver's `Accounts Receivable - Trade` holds the
client's `Certificate of Deposit` value of **3,000,000.00**; Weaver's `Accounts Receivable - Indital`
holds the client's `Accounts Receivable - Trade` value of 3,412,281.79.

FY2025 (expected as `value_delta`, NOT alignment): `Prepaid Inventory` +21,294.79,
`Accounts Payable` +21,294.79, `Machinery & Equipment` +7,500.00, `Vehicles` +17,777.98.
