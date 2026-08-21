# Financial Statement Automizer — Production Plan

**Goal:** Convert inconsistent client-supplied financial statements into Weaver's standardized BVAL Model template, automatically, with an auditable trail.

**Status:** Plan. No code written yet.
**Evidence base:** `Konrad Project/` — Weaver's blank BVAL template, one delivered client model (TS Distributors), and six raw client workbooks (FY2020–FY2025).

---

## 1. What we are actually automating

Reverse-engineered from the delivered `TS Distributors, Inc. BVAL Model.xlsx`, the current manual process is four steps:

| # | Step | Who/how today | Automate? |
|---|------|---------------|-----------|
| 1 | Collect N single-year client workbooks | Email | No |
| 2 | Stitch them into one multi-year sheet | Manual copy/paste | **Yes** |
| 3 | Paste that as *values* into `Historical BS` / `Historical IS` tabs of the model | Manual paste-special | **Yes** |
| 4 | Hand-write mapping formulas from those tabs into the standardized `BS` / `IS` tabs | Manual, cell by cell | **Yes — this is the bottleneck** |

Step 4 produces formulas like:

```excel
BS!K11  (Inventory)      = SUM('Historical BS'!G18:G21, 'Historical BS'!G27)
BS!K24  (Accum. Deprec.) = 'Historical BS'!G41 + K112 + K111 + K113
IS!F34  (Other Inc/Exp)  = 'Historical IS'!Z126 + 'Historical IS'!Z16 - SUM('Historical IS'!Z136:Z137)
```

Everything downstream in the model — DCF, WACC, NWC, Deprec, ratios, the PDF exhibit — already keys off the standardized `BS`/`IS` tabs. **We do not touch any of it.** Fill those two tabs correctly and the rest of the model works unchanged.

### Source data shapes (verified)

**Balance sheet** — each client file, sheet `Balance Sheet`:
- Labels in col **A**, current-year values in col **C**, prior-year comparative in col **E**, % change in col **G**.
- Header row 5 carries the period dates; row 3 carries "As of December 31, 2024".

**Income statement** — each client file, sheet `Consolidated IS`:
- Labels in col **A**, monthly Jan–Dec in **B..Y** as (value, %) pairs.
- Col **Z** header is literally `YTD <year>` — **this is the only column we need.**
- Cols **AC..AZ** repeat the prior year monthly, **BA** = `YTD <prior year>` → a free independent cross-check.
- Also present per file: per-location tabs (Houston, Chicago, Austin, Prescott, Birmingham, Miami, Indital Houston), `Trial Balance`, `Worksheet`, `SUBLEDGER REC`. **Ignore all of these in v1** — the consolidated tabs are what feeds the model.

---

## 2. Why this is hard (measured, not assumed)

### 2.1 The client's chart of accounts drifts every year
- `Certificate of Deposit` present FY2021–FY2023, absent FY2024–25.
- `Goodwill - Exclusivity Contract` and `Other Payables` first appear FY2024.
- `Inventory - Indital Beg Balance Variance` appears FY2022.
- The same account is renamed mid-history: `Accounts Receivable - Lawler` (FY2023) → `Accounts Receivable - Indital` (Weaver's consolidation).
- Weaver silently re-labels on paste: `Accumulated Depreciation & Amortization` → `Accumulated Depreciation & Amort.` in **every** year.
- Typos are load-bearing: Weaver's consolidation says `Other Paybles`, the client file says `Other Payables`.
- Row positions therefore shift: Accounts Payable is row 50 in FY2021, row 49 in FY2024, row 52 in the consolidation.

**⇒ Matching must be by normalized label + alias table, never by row position.**

### 2.2 The standardized template is extensible, not fixed
The template ships 5 fixed-asset rows (`Computers and Software`, `Furniture and Fixtures`, `Equipment`, `Leasehold Improvements`, `Land and Buildings`). The delivered TS model has 6, and the analyst got there two different ways:
- **Repurposed** the unused `Land and Buildings` row → relabeled `Vehicles`.
- **Inserted** a brand-new row → `Software`.

Same on the asset side: `Employee Advances` inserted into current assets, `Exclusivity Contract (Net)` and `Deposits` into other assets. Total Assets moved from template row 33 → row 36.

**⇒ The writer must plan row allocation per block, and prefer repurposing an unused template row before inserting.**

### 2.3 Some rows are structurally load-bearing
Template rows carry markers in col A: `Do Not Change >>>` (BS rows 9–11, 29, 41–44, 47–50) and `Do not add row before/after >>` (IS rows 35, 39). These are referenced by absolute position elsewhere — e.g. `BS!E60 (Debt) = E44+E42+E47+E50+E43+E49`, which the template annotates *"Debt throughout model flows from here; confirm calc."*

**⇒ Row inserts are legal only *within* a block and never across an anchor.**

### 2.4 The current manual process is silently producing errors
I diffed every client file against Weaver's hand-built consolidation, matching on account label:

| FY | Result |
|----|--------|
| 2020 | Clean |
| 2021 | Clean (1 deliberate reclass: AR-Trade → AR-Indital) |
| **2022** | **9 line items misaligned by one row** |
| **2023** | **9 line items misaligned by one row** |
| 2024 | Clean |
| 2025 | 12 differences — all deliberate adjustments, not errors |

The FY2023 cascade, as an example: real Inventory of \$16,881,097 landed on the `Inventory - Offsite` line, `Notes Receivable` landed on `Employee Advances`, `NSF Clearing` landed on `Notes Receivable`, and so on for nine consecutive rows. Cause: the client dropped rows (`Certificate of Deposit`, `Accounts Receivable - Lawler`) that the consolidation's row list still had, and the paste didn't account for the offset.

**Why it is invisible.** Over the affected span the client's list has one row Weaver's lacks (`Certificate of Deposit`) and Weaver's has one the client lacks (`Inventory - Indital Beg Balance Variance`). Eleven rows either way — so the block *fits*, `Total Current Assets` lands on `Total Current Assets`, and the column ties to the penny. The model's own balance check (`BS!E59`) never fires.

**Impact — scoped precisely.** I traced every consumer of the affected lines:
- `NWC` and `CapCF-NWC` read `BS!E15` (**Total Current Assets**) and `BS!E48` (Total Current Liabilities) — subtotals, which tie. **The DCF is arithmetically unaffected.**
- `Entity Ratios` (22 cells) and `FS Charts` read `BS!E10` (AR) and `BS!E11` (Inventory) **individually**. That is where the damage lands.
- FY2022 is the material year: the client's \$3,000,000 `Certificate of Deposit` sits inside `Accounts Receivable`, overstating AR by \$2,998,050.
- Consequence: **DSO overstated by ≈7 days in both FY2022 and FY2023** (24.9 vs 17.8; 24.3 vs 17.6), and the reported AR trend is *sign-inverted* — the exhibit shows AR falling \$6.44M → \$4.32M (−33%) when it actually rose \$3.44M → \$4.34M (+26%).
- DIO and Quick Ratio are negligibly affected (<0.02 days; the CD nets out of the quick-ratio numerator if classified as cash).

So: not a valuation-arithmetic error, but a corrupted trend in a client-facing exhibit that also informs the analyst's risk and multiple-selection judgment.

FY2020, FY2021 and FY2024 were pasted correctly by the same process. **This is human inconsistency under repetition — exactly what automation removes.**

### 2.5 Ingestion and analyst judgment must stay separate
FY2025's 12 differences are *not* errors — they're deliberate adjustments (+\$21,294.79 to both Prepaid Inventory and Accounts Payable, an accrual reclass; +\$7,500 Machinery & Equipment; +\$17,778 Vehicles). Likewise `IS!F34` folds `Other Revenues` out of revenue and into other income, and `BS!K24` layers amortization schedules onto accumulated depreciation.

**⇒ Two distinct layers, always: (a) faithful ingestion of what the client sent, (b) named, reviewable adjustments on top. Never conflate. The reconciliation report must show both.**

---

### 2.6 Sample 2 (Commercial Flooring) breaks every ingest assumption — and hands us a better source

TS Distributors was one client in one shape: Excel workbooks, one statement per sheet, multi-year columns side by side, flat account lists. Sample 2 shares almost none of that. Measured, not assumed:

| Dimension | TS Distributors | Commercial Flooring |
|---|---|---|
| Format supplied | `.xlsx` | **`.pdf`** (originals), hand-converted by the team to `.ods` / `.xlsx` |
| Producing system | spreadsheet model | **QuickBooks print-to-PDF** |
| Statements per sheet | one | **two, stacked vertically** (BS then P&L on one sheet) |
| Years per file | many columns | **one per file/page** |
| Account hierarchy | flat, ~1 level | **nested to 5 levels** |
| Label uniqueness | unique | **duplicated across depths** |
| Period coverage | uniform FY | **FY25 exists twice: an 8-month stub and a full year** |

Five findings that change the design:

1. **The PDFs are text, not scans.** Zero embedded images, ~150 extractable words per page. No OCR required.

2. **Label x-offset encodes the QuickBooks hierarchy exactly.** Body labels sit at x = 191.0, 197.9, 204.7, 211.6, 218.3, 225.1 — a dead-regular 6.85pt ladder yielding integer depths with no rounding ambiguity. Page furniture (title, timestamp) falls at clearly separated offsets. **Depth is the strongest structural signal we have ever had**: it gives section membership, header-vs-detail, and subtotal scope directly, replacing the formatting heuristics §6 needs for Excel.

3. **The team's PDF→Excel conversion is lossy, and lossy in exactly the field that matters.** The `.ods` carries no indentation — not as a column offset, not as leading whitespace. `'Accounts Receivable'` the group header and `'Accounts Receivable'` the detail line become byte-identical strings; nothing downstream can separate them. The same collapse hits `Credit Cards` and `Insurance Expense`. **We should read the PDF and skip the conversion entirely** — it deletes a manual step *and* yields strictly better data.

4. **Labels are truncated at source.** `Total Other Current Liabili...`, `Worker's Compensation Insura...` — QuickBooks truncates on print, so the full string is unrecoverable from any downstream artifact. Matching must tolerate a truncated prefix; depth tells us what the row is even when its name is cut off.

5. **Period metadata is load-bearing and conflicting.** `Financial Statements 25.ods` covers *January through August 2025*; `2025 P&L.pdf` covers *January through December 2025*. Dropping both into a year-keyed table silently mixes an 8-month stub with full years — a valuation error far larger than any mapping mistake. Separately, pages 1–2 of the 2020–2024 PDF are byte-identical duplicates of pages 3–4. **Period parsing and de-duplication are required, not optional.**

---

### 2.7 Architecture response: a reader seam, not a second intermediate format

The instinct to add "an intermediary phase that maps company inputs to a standardized pre-template format" is right, and most of it already exists: `ConsolidatedTable` **is** that standardized intermediate, and it is already what mapping, review and writing consume. The defect was never a missing layer — it was that the layer had exactly one door: openpyxl, fused to TS-shaped assumptions.

So we add readers *below* the existing intermediate rather than inventing a new one:

```
.pdf ──► pdf reader   ─┐
.ods ──► ods reader   ─┼──► RawDoc ──► interpret ──► StatementSet ──► ConsolidatedTable ──► (unchanged)
.xlsx ─► xlsx reader  ─┘   (universal)   (format-agnostic)
```

- **`RawDoc`** is a positional grid: rows of text + optional `depth` + values + provenance. Deliberately dumb — it knows nothing about accounting.
- **`interpret`** holds all the accounting judgment (which rows are statements, what period, header vs detail vs subtotal) and is written once, format-agnostic. Readers that supply `depth` get the precise path; readers that cannot fall back to today's heuristics.
- Adding format #4 means writing one reader, not touching the pipeline.

One deliberate departure: the standardized intermediate stays **in memory as typed data, not as an Excel file**. Materialising it as a workbook mid-pipeline would add a lossy serialize/reparse round-trip in the middle of the chain — the very failure we just measured in the team's own conversion step. It is still *exportable* as a sheet for inspection and trust (`out/BS.csv` already does this), but export is a view, never the channel.

**Non-negotiable:** a scanned (image-only) PDF must fail loudly at ingest with a clear message, never silently yield an empty or partial statement.

---

## 3. Decisions (confirmed)

| Decision | Choice |
|---|---|
| Delivery | **Local Windows app** — Python, packaged as a single `.exe`. No client data leaves the machine. |
| Mapping engine | **Deterministic core, LLM only for leftovers.** Alias dictionary + fuzzy match + per-client learned profile. Unmatched *account names only* (never amounts, never client identity) may go to Claude for a suggestion a human confirms. |
| v1 scope | **Consolidate + map BS/IS.** Ingest N single-year files → build `Historical BS`/`Historical IS` → generate the standardized `BS`/`IS` mapping formulas. |
| Generality | **Client-agnostic engine, seeded by TS.** Per-client mapping profile saved and reused; TS Distributors is profile #1 and the golden regression test. |

---

## 4. The critical technical constraint — read this before writing any code

**openpyxl cannot be used to write the BVAL model. It must be Excel COM automation (`pywin32`).**

Three independently fatal reasons, all verified against the actual template:

1. **openpyxl does not update formula references on row insert.** `insert_rows()` shifts cells but rewrites nothing. The template has 47 sheets and thousands of cross-sheet references into `BS`/`IS`. A single insert would silently corrupt the model — wrong numbers, no error.
2. **openpyxl destroys template features on round-trip.** Loading the template already emits `Conditional Formatting extension is not supported and will be removed` and `Data Validation extension is not supported and will be removed`. It also drops charts — and the template has a `FS Charts` sheet plus chart objects on `VAL Comp`, `GPC Comp`, `Entity Ratios`.
3. **The template carries third-party add-in state** — `DS_INTERNAL_*` (DealStats) and `_CIQHiddenCacheSheet` (CapIQ) sheets, marked `veryHidden`. openpyxl round-tripping risks breaking the add-ins the valuation team relies on.

**Architecture that follows:**
- **Read** client workbooks with **openpyxl** (fast, read-only, Excel not required).
- **Write** the model through **Excel COM** — Excel itself performs row inserts and updates every reference natively, and every template feature survives because the file is never re-serialized by us.
- Always operate on a **copy** of the template. Never open the original.
- COM is slow per-cell → batch writes as 2-D range assignments, and wrap in `Application.ScreenUpdating = False`, `Calculation = xlCalculationManual`, restoring both in a `finally`.
- Excel is installed on every analyst machine at an accounting firm, so requiring it costs nothing.

---

## 5. Architecture

```
fsa/
  app.py                  # GUI entry point (PySide6 or Tkinter)
  cli.py                  # scriptable entry point; used by tests
  ingest/
    discover.py           # locate BS/IS sheets; detect label col, value col, period dates, scale
    extract_bs.py         # balance sheet → SourceAccount[]
    extract_is.py         # income statement → SourceAccount[] (YTD column only)
    normalize.py          # label normalization: case, punctuation, whitespace, abbreviations
  model/
    schema.py             # SourceAccount, Period, StatementSet, Adjustment
    template.py           # TemplateSpec: BS/IS taxonomy, blocks, anchors, subtotal formulas
  mapping/
    profile.py            # per-client mapping profile: load/save/version/diff
    aliases.py            # global curated alias dictionary
    matcher.py            # L0–L3 deterministic matching
    llm.py                # L4 optional Claude assist (names only)
  validate/
    checks.py             # all reconciliation checks
    report.py             # human-readable + machine-readable report
  write/
    rowplan.py            # plan row repurposing/insertion per block
    formulas.py           # generate SUM-range mapping formulas
    com_writer.py         # Excel COM driver
  profiles/               # saved client profiles (YAML, git-tracked, shared)
  tests/
    golden/               # TS Distributors baseline
```

### 5.1 Core data model

```python
@dataclass(frozen=True)
class SourceAccount:
    raw_label: str          # exactly as it appears in the client file
    norm_label: str         # normalized for matching
    statement: Literal["BS", "IS"]
    section: str | None     # client's own header: "Current Assets", "Fixed Assets", ...
    fiscal_year: int
    value: float
    is_subtotal: bool       # client's own subtotal rows — extracted for validation, never mapped
    source: CellRef         # file, sheet, cell — full provenance

@dataclass
class MappingRule:
    match: Matcher                  # exact | normalized | alias | regex
    target: str                     # canonical template line name
    sign: Literal[1, -1]
    rationale: str | None
    confidence: float
    decided_by: str                 # "profile" | "alias" | "fuzzy" | "llm" | "human"
    decided_at: datetime
```

### 5.2 Pipeline

```
discover → extract → cross-year reconcile → validate → map → row-plan → write (COM) → recalc → verify → report
```

Each stage is pure and independently testable except `write`.

---

## 6. Extraction rules

**Always take each year from its own file's current-year column. Never trust the comparative column as a data source.** I verified the comparative column in file N is aligned to the *prior* year's row list, not file N's — which is precisely how the FY2022/23 errors were born.

Use the comparative column instead as a **free validation signal**: file N's prior-year column must equal file N−1's current-year column, account by account. Any disagreement is either a client restatement or an extraction bug — flag it, never silently resolve it.

If the earliest year has no standalone file, fall back to the oldest file's comparative column and mark every account from it `provenance = comparative, unverified`.

**Detection heuristics** (all fall back to human confirmation in the GUI):
- BS value column: the numeric column whose header row parses as a date matching the file's stated period end.
- IS value column: header matching `^YTD\s+(\d{4})$`.
- Section headers: non-numeric rows in the label column that match a known section vocabulary.
- Client subtotal rows: label starts with `Total ` **or** the row's value equals the sum of the preceding un-subtotaled block. Extract them, use them to validate, and exclude them from mapping.
- Scale detection: check for "in thousands"/"in 000s" in rows 1–5; verify against magnitude sanity.

---

## 7. Mapping engine

### The shape of the problem: cold start vs steady state

The deterministic layers below are a **cache of decisions already confirmed**, not a
first-line solver. On a brand-new client the profile is empty, the alias dictionary covers
only common accounts, and fuzzy matching has nothing to match against — so deterministic
alone leaves ~120 of ~177 accounts for a human. That is exactly the generalization problem
this project exists to solve, so the LLM belongs at **first contact**, not as an optional
garnish:

| | New client, year 1 | Same client, year 2+ |
|---|---|---|
| Who proposes | LLM, over unmatched account **names** | Profile lookup (L0) |
| Who decides | Analyst confirms, tiered by confidence | Nobody — already decided |
| Cost / latency | ~$0.10, seconds | Zero |

**Model choice is not a cost decision.** Measured on the TS chart of accounts (177 accounts,
~3.3K in / 3.5K out): Haiku 4.5 ≈ \$0.02, Sonnet 5 ≈ \$0.06, Opus 5 ≈ \$0.10 — **once per
client**. One bad mapping costs more analyst time than a year of model spend, so default to
the strongest model and benchmark cheaper ones against the eval set (§7.2) rather than
assuming. Batch API and prompt caching are irrelevant here: this runs once, interactively.

### 7.0 What the target set actually is — measured, and it is not one problem

`fsa/mapping/template.py` derives the taxonomy from the blank template's own formulas
(never hardcoded rows). Checking it against the 60 ground-truth labels produced the finding
that most changes Phase 1:

> **Of the 33 template lines the analyst actually used, 15 do not exist in the blank
> template.** Three are renames of existing slots (`Unearned Revenue`→`Deferred Revenue`,
> `Goodwill`→`Goodwill (Net)`, `Other Income (Expense)`→`Other Income (Exp.)`); twelve are
> lines the analyst inserted or repurposed (`Vehicles`, `Software`, `Employee Advances`,
> `Deposits`, `Exclusivity Contract (Net)`, `Salaries & Wages`, `SG&A Expenses`, …).

So **mapping is not classification into a fixed taxonomy.** An engine that could only choose
among the template's existing lines would be structurally unable to reproduce ~45% of a real
engagement. The engine must also be able to *create* a target: name a slot, or insert a line
into the correct block.

The two statements are different problems, and the numbers make that concrete:

| | Targets | Shape | Task |
|---|---|---|---|
| **BS** | 37, all named | `Cash and Cash Equivalents`, `Accounts Receivable`, … | **Classification** into a real taxonomy |
| **IS** | 28 = 12 named + **16 placeholder slots** | `Revenue Component 1..3`, `COGS Component 1..3`, `Operating Expense 1..5`, `Other Income (Expense) 1..5` | **Grouping and naming** — the slots carry no meaning until the analyst names them |

Consequences for the design:

1. **A proposal is one of three kinds**, not one: `assign` to an existing named line, `name` a
   placeholder slot, or `insert` a new line into a named block. The eval must score all three
   — scoring only `assign` would measure the wrong thing and look deceptively good.
2. **Placeholder slots are capacity, not categories.** Fuzzy matching cannot route
   `Sales - Processing` to `Revenue Component 1`, because the slot label carries no signal.
   For the IS the useful context is the *block* (`Revenue`, `Operating Expenses`) plus the
   sibling accounts being grouped — which is why `taxonomy_prompt()` groups targets by block.
3. **Block subtotals are collapsible targets.** Verified in the delivered model: the client
   reported one `Total Cost of Goods Sold` with no breakdown, so the analyst wrote it straight
   into `Cost of Revenue`, overwriting `=SUM(E15:E17)` and leaving the three COGS component
   slots empty. Legal because a block subtotal sums only its own block; cross-block roll-ups
   (`Total Assets`) and computed results (`Gross Profit`, `Operating Income`, `Net Income`)
   are never collapsible — overwriting those desynchronizes the model.

### 7.0b Client subtotals are mapping *sources*, and on the IS they are the main one

`fsa/model/schema.py` says subtotal rows are extracted for validation and never mapped.
Measured against ground truth, that rule is wrong: **4 of the 60 labels map *from* a client
subtotal.**

| Client account (a subtotal) | Template line |
|---|---|
| `Total Owners Equity` | Total Equity |
| `Total Cost of Goods Sold` | Cost of Revenue |
| `Total Salaries & Wages` | Salaries & Wages |
| `Total Operating Expenses` | SG&A Expenses |

This is the mirror of collapsible template subtotals (§7.0): when the client's breakdown is
finer than the template needs, the analyst maps the client's *total* and ignores the detail.

**It reframes the income statement.** The IS has **98 data accounts but only 16 ground-truth
labels** — because four rollups absorb ~90 detail accounts. So the IS task is not "classify 98
accounts"; it is largely "recognise that the client's own subtotal structure already matches
the template's granularity, and map at that level." An engine that dutifully classifies all 98
detail accounts is solving a problem the analyst deliberately avoided, which is exactly why
the deterministic IS score is so low.

**Safety rule:** a client subtotal may be mapped only if none of its components are also
mapped — otherwise the value is double-counted. That is mechanically checkable and is
precisely what arithmetic verification (§7.3) catches.

### 7.0c Overfitting: which knobs are actually at risk

There is no gradient descent here and no memorization -- the model's weights are fixed, and
accounting vocabulary has real cross-client regularity (Cash is Cash everywhere; the template
is Weaver's, not derived from TS). So the risk is genuinely lower than in a learned-model
setting. But it is not zero, because **every free parameter in this system was set by looking
at one client**, with no held-out set:

| Knob | Risk | Why |
|---|---|---|
| Template parsing, three-kind targets, subtotal-as-source, sign handling, arithmetic verification | **Low** | Structural facts about the template and about accounting; derived from Weaver's file, not TS's data |
| Redaction mechanism | Low | Structural signal (a name recurs across stems), though `MIN_STEMS = 3` is untested elsewhere |
| Alias dictionary (84 keys) | Medium | Vocabulary is generic, but *which* aliases exist was driven by what TS has |
| `FUZZY_THRESHOLD = 0.86` | Medium | Plausible default, validated only on TS |
| `RENAME_THRESHOLD = 0.73` | **High** | Fitted to three TS points (0.80, 0.75) against one near-miss (0.70) |
| Granularity instruction in the prompt | **High** | Inferred from one analyst's choices on one engagement -- may be house style, personal style, or distributor-specific |
| Rename-tolerant scoring | **High** | The *metric* was loosened after seeing TS renames; this inflates the number without improving the product |

**The sharpest lesson came from the residual errors.** After the granularity fix, 8 of the 12
remaining errors are the model naming a slot differently but validly --
`General & Administrative` vs `SG&A Expenses`, `Salaries, Wages & Benefits` vs
`Salaries & Wages`. Closing that gap would mean teaching the engine *this analyst's naming
habits*, which is overfitting by definition and would not transfer. **So most of the apparent
IS headroom is metric artifact, not accuracy headroom** -- a strong argument against tuning
further on n=1.

Two concrete de-overfitting actions taken: the prompt no longer names TS's specific account
types (`vehicles, software, deposits, organizational costs` were literally this client's
inserted lines), and the granularity rule is now split BS-vs-IS, since the evidence says
analysts *split* asset classes and *group* revenue/expense detail -- a single global
"be granular" rule was fitted to balance-sheet evidence and was pushing the income statement
the wrong way.

**The test that matters** is a second client, scored *before* any tuning. Expect a drop; the
question is whether it lands near 82% (validated) or near 60% (we were tuning to one company,
and per-client profiles carry the value instead).

### 7.1 Layers

First confident hit wins. Every decision — LLM-proposed or human-made — is persisted to the
client profile, which is what makes year 2 free.

| Layer | Mechanism | Notes |
|---|---|---|
| **L0** | Client profile lookup by `norm_label` | Exact prior decision. Instant, deterministic, reproducible. The steady state. |
| **L1** | Global alias dictionary | Curated, cross-client: `Operating Account`→Cash, `Petty Cash`→Cash, `NSF Clearing Account`→Accounts Receivable, … |
| **L2** | Structural inference | Constrain candidates using the client's own section (`Current Assets` ⇒ only current-asset targets) and sign. |
| **L3** | Fuzzy match | Token-set ratio vs L0+L1 keys. Threshold-gated; below threshold → escalate, never auto-accept. |
| **L4** | **LLM proposal** | Every account not resolved above. Sends normalized **account names**, their section, and the template taxonomy. **Never amounts, never client identity** — `Warehouse - Equipment & Racks` is classifiable without knowing it is \$1,015,696.55. |
| **L5** | Human review UI | Confirms/overrides, tiered by confidence and by whether §7.3 verification passed. Writes back to L0. |

### 7.2 Measuring it — we have ground truth

The delivered TS model encodes a complete expert mapping in its formulas
(`BS!K11 = SUM('Historical BS'!G18:G21, G27)` is an analyst saying "these five accounts are
Inventory"). `tools/extract_ground_truth.py` recovers it: **60 labeled pairs across 33
template lines**, written to `eval/ts_distributors_mapping.yaml`.

So Phase 1 is measurable, not hopeful: run the mapper blind over the client's account names,
score against these labels, and **set the auto-accept confidence threshold from the resulting
precision curve.** Report top-1 accuracy, and separately the cross-block error rate — the
errors §7.3 cannot catch.

#### Measured results (TS Distributors, cold start, empty profile)

```
python tools/score_mapping.py --template "<blank template>" --clients "<client dir>" \
       --delivered "<delivered model>" [--llm]
```

| | Deterministic | + LLM, v1 | **+ LLM, v2** |
|---|---|---|---|
| Coverage | 62% | 93% | **98-100%** |
| Precision | 95% | 75% | **80-83%** |
| End-to-end | 58% | 70% | **80-82%** |
| **Block placement** | 58% | 80% | **85-87%** |
| **Cross-block errors** | 1 | 1 | **1** |
| BS end-to-end | 70% | 82% | **91%** |
| IS end-to-end | 25% | 38% | **50-56%** |

**v1 -> v2 was two free changes, worth ~+12 points.** No model change, no extra spend:

1. **Corrected a prompt instruction that was causing the errors.** v1 said "prefer an
   existing named template line... use insert only when no existing line fits." Five of six
   visible BS errors were then exactly that: the model reused a generic line
   (`Employee Advances`->Accounts Receivable, `Software`->Computers and Software) where the
   analyst inserted a dedicated one. The instruction was backwards for valuation work, where
   the point is visibility into distinct asset classes. Inverting it (dedicated line is the
   norm; generic buckets for genuine residue) took BS from 82% to 91%.
2. **Surfaced client subtotals as candidates** (§7.0b) and showed the client's own hierarchy
   in the payload, so rollup-vs-detail became an explicit choice. IS coverage went 81% -> 100%.

**Model choice was worth ~nothing; framing was worth everything.** Measured at equal effort
and identical prompts:

| Model | e2e | block placement | cost/run |
|---|---|---|---|
| Opus 5 | 80% | 85% | $0.349 |
| **Sonnet 5** | **82%** | **87%** | **$0.210** |

The 2-point gap is inside run-to-run variance, so the honest reading is *indistinguishable at
40% less cost* -> **Sonnet 5 is now the default.** Note Sonnet spent *more* output tokens
(19.4K vs 12.4K) and still cost less; per-token price dominated.

**The important row is the last error row, not the headline.** L4 bought +31 points of
coverage and +22 of block placement while adding **zero** additional cross-block errors. Its
mistakes are line choices *within the correct block* — `Employee Advances`→Accounts
Receivable, `Computer Equipment`→Computers and Software (a line the template actually has;
the analyst chose Equipment). Those still tie every subtotal, leave the valuation unchanged,
and are cheap to fix in review. That is the risk profile §7.3 was designed around, and it is
now measured rather than assumed.

Caveats to keep honest:
- **Run-to-run variance is real.** Two identical runs scored 43 and 42 correct (IS 7 then 6).
  Report a range, and re-run before attributing a change to a code edit.
- **Precision falls as coverage rises**, necessarily — L4 only ever attempts what the
  deterministic layers already refused, i.e. the hard residue.
- Cost per full run: ~$0.10 at Opus 5 pricing.
- 4 labels are still `missing`: they map *from* client subtotals, which the matcher does not
  yet consider (§7.0b). Fixing that is the cheapest remaining win.

#### Measured results (Commercial Flooring — the generalization test)

Cold, empty profile, `--delivered`, 2 runs. **This is the first accuracy number for a client the engine was never developed against.**

| | TS Distributors | **Commercial Flooring** |
|---|---|---|
| End-to-end | 73–75% | **30%** |
| BS end-to-end | 86% | **58%** |
| IS end-to-end | 38–44% | **11%** |
| Coverage | 87–90% | 47–50% |
| Precision | 81–87% | 60–64% |
| Block placement | 82–83% | 40–47% |
| **Cross-block errors** | 1 | **0** |

Both runs scored identically end-to-end (9/30), so this is a stable result, not a bad draw.

**The honest headline: accuracy roughly halves on an unseen client.** The gap decomposes into three very different causes, and only the third is a defect:

1. **Invented target names — 20 of the 30 ground-truth targets do not exist in the blank template.** The analyst renamed placeholder slots to `Rent`, `Insurance`, `Payroll Expense`, `Professional Fees`, `Interest Income & Dividends`, `Autos & Trucks`… Scoring by exact label therefore asks the model to guess the analyst's precise English (`Rent Expense` → `Rent`; `Repairs and Maintenance` → `Repairs & Maintenance`). Several "errors" are the *right row under its original name* — `Security Deposits` was scored wrong for proposing `Other Assets`, which is the very row the analyst relabelled. TS had the same effect at ~45%; here it is 67%, which alone accounts for much of the drop.
2. **Level disagreement — ~11 labels.** The model rolled operating expenses into `Operating Expenses`; the analyst broke them into 8 named lines. Given §7.4, this is the judgement call landing on the other branch, not an error — and the analyst's own formula (`Total Expense - SUM(K21:K28)`) shows they mapped the rollup *too*, then subtracted.
3. **Genuine mistakes — about 2.** `Autos and Trucks` → `Equipment` (same block, minor) and `Other Income Schwab *Div` → `Other Income (Expense)` where the analyst grouped it with interest income.

**The safety result is the one that matters, and it held: zero cross-block errors on both runs.** Every mistake was a line choice *within* the correct subtotal, so every total still ties and the valuation arithmetic is unaffected — exactly the risk profile §7.3 was designed around, now confirmed on a client the design never saw. Ingest was likewise clean: 141/141 subtotals tie and every balance sheet balances.

**What this changes about the product.** Auto-accept was never planned, and this kills any residual temptation: at 30% strict e2e on an unseen client, the deliverable is *a reviewed draft*, not an answer. The value is ingest + validation + arithmetic + a review surface that makes correction fast — which is where the measured strength actually is. It also raises the value of the profile: flooring's second engagement should start from year one's confirmed decisions rather than from 30%.

**Eval caveat.** `eval/commercial_flooring_mapping.yaml` has 30 labelled pairs against TS's 60, and 9 of its mappings are *year-variant* (the analyst changed them mid-history); the canonical value takes the most recent year. Treat single-digit differences as noise.

#### 7.2b Label matching is the wrong metric — measure the numbers

Challenged on whether "the eval is unfair" was an excuse, the claim was tested rather than argued, and it turned out to be **half right and half a cover for a real bug**.

The strict metric asks "did you name the same template line?". That penalises differences which change nothing: mapping the rollup `Total Checking/Savings → Cash` and mapping its three children individually to `Cash` put identical money on an identical line, yet one scores 1/1 and the other 0/1. Two thirds of flooring's ground-truth targets are names the analyst invented, so much of the score was measuring whether the model guesses an analyst's English.

So `value_score.py` compares **block subtotals** instead: does our mapping reproduce the delivered model's Total Current Assets, Operating Expenses, Pre-Tax Income? Naming-independent, level-independent, and the thing an analyst actually cares about. Two exclusions keep it honest: block-years the client never sent, and blocks carrying **analyst adjustments** — TS nets amortization into its intangibles (`='Historical BS'!G34-K111`), which is judgement layered on ingestion (§2.5) and not this layer's job to reproduce.

**The two metrics invert:**

| | strict label e2e | block-subtotal reproduction |
|---|---|---|
| TS Distributors | 73% | **35%** (6/17) |
| Commercial Flooring | 33% | **92%** (22/24) |

Neither number alone is the truth. Together they say something the label metric hid completely: **the engine does not do systematically worse on the unseen client.** TS's 35% is two distinct errors repeated across five years — chiefly `Inventory Receipts - Clearing Account` mapped as a current asset when it is a payable, which is the one cross-block error §7.2 already reported. Flooring's 8% is a single $1.37M brokerage-classification disagreement, self-flagged at 0.59 confidence, which lands at the top of the review sheet by construction.

**The bug this exposed — double-counts were detected but still written.** `reconcile` raised its `double_count` ERROR and then left *both* rules in place, so `build_plan` summed both and the money landed twice. Measured: flooring's Total Current Assets came out **$736,592 high**. Fixing it took block reproduction from **46% → 92%** on flooring.

Which side wins is not a preference; completeness decides it, because keeping the *total* right outranks keeping the *split* right:

* every member mapped → the detail covers the group, so drop the rollup and keep the finer breakdown;
* otherwise → only the rollup is guaranteed to cover every member, so drop the mapped members.

The losing side becomes a visible, reversible exclusion, and the ERROR still fires.

#### 7.2c What the analyst actually breaks out — and it is not materiality

The second answer key showed the prompt rule *"INCOME STATEMENT: group more, split less"* was wrong. It was over-generalised from TS's `Total Operating Expenses → SG&A`, ignoring that TS **also** split out Salaries & Wages and four revenue lines.

Flooring gave its own line to: Payroll, Compensation to Officer, Retirement Account, Rent, Professional Fees, Insurance, Franchise & Property Taxes, Repairs & Maintenance. It bucketed: janitorial, postage, printing, dues, office supplies, bank charges. **Every broken-out line is a standard valuation normalization category** — owner compensation, related-party rent, owner benefit plans, non-recurring professional fees. The bucketed ones are costs nobody ever adjusts.

**Materiality was tested and refuted.** Ranked by FY2025 amount the two groups interleave: `Auto and Truck` at \$15,131 is bucketed while `Repairs and Maintenance` at \$10,449 gets its own line; `Travel` at \$12,685 bucketed, `Professional Fees` at \$8,718 dedicated. Decisively, **`Retirement Account` is \$0 in FY2025 and still has a dedicated line** — you reserve a line for a zero balance because you intend to normalize it, not because it is large. This is why the design's refusal to send amounts (§7.1) costs nothing here.

The prompt now states the rule directly — break out what a valuation normalizes, bucket the rest, and use the empty `Operating Expense N` slots the template ships for exactly that. Measured on both clients, 2 runs each: flooring coverage **50% → 77%**, e2e 30% → 33%, IS 11% → 17%; TS essentially unchanged (e2e 73/72 vs 73/75). Helps one client, neutral on the other — which is the bar a prompt change has to clear now that there are two answer keys.

Two structural findings from the extraction, both of which change the data model:

1. **A mapping is `(account, template line, sign)` — not a pair.** Three of the 60 are contra
   items: `Taxes - State Income/Franchise` and `Penalties/Fines` are *subtracted* inside
   Other Income; `Interest Expense` arrives positive and is negated. Sign must be an explicit
   field on `MappingRule`, proposed by the LLM and verified arithmetically — never inferred at
   write time.
2. **Ground truth is year-dependent.** `Goodwill (Net)` includes
   `Goodwill - Exclusivity Contract` for FY2020–FY2024 and excludes it in FY2025, once the
   contract got its own line. The canonical label is the most recent year's decision; the
   profile must therefore record *which year* a rule was decided in, and the eval must not
   penalise the mapper for the superseded variant.

### 7.3 Verification: the LLM proposes, arithmetic disposes

Every client statement carries its own subtotals, which gives a free exact check that needs no
judgment: if all accounts are mapped and `Total Current Assets` does not reconcile, something
is misclassified.

**Honest limit:** this catches *cross-block* errors (an asset landing in liabilities, a
current item landing in non-current) but **not within-block** ones — `Prepaid Expenses` vs
`Other Current Assets` both leave the subtotal intact. Cross-block errors are the ones that
move the valuation (current-vs-non-current drives working capital; debt classification drives
capital structure), so this is a strong check, not a complete one. Within-block confidence
comes from the eval in §7.2 and from human review, and the review UI must not imply otherwise.

**Sign handling is explicit per rule, never inferred at write time.** Verified cases: Accumulated Depreciation arrives negative and stays negative (`Net Fixed Assets = SUM(gross, accum)`); Interest Expense arrives positive at `Historical IS` row 135 and the model negates it (`=-'Historical IS'!Z135`).

**Rename detection.** When an account disappears in year N and an unmapped one appears with a similar normalized label and a plausible value continuation, propose a rename link (`Lawler` → `Indital`) for human confirmation. Record it in the profile so history stays continuous.

**No silent drops, ever.** Every non-subtotal source account must end in exactly one of: mapped to a template line, or explicitly marked excluded with a reason. The report proves this per block (§8).

**And the review sheet must show the exclusions, not just the mappings.** `reconcile` auto-excludes every account a mapped rollup already covers — 40 rows on sample 2's income statement. Exporting only `ms.rules` hid all of them, which quietly broke the rule above: a decision the analyst cannot see is a decision they cannot reverse. Excluded accounts now appear pre-filled with the exclude token, so leaving one accepts it and overtyping it reinstates the account as a real mapping.

---

### 7.4 Run-to-run variance: measured, decomposed, and mostly one decision

Two identical cold runs on sample 2 disagreed badly — the income statement left 47 unresolved once and 11 the next. Before treating that as "the model is flaky", it was measured properly at k=5 per statement, separating two things a single "how many unresolved" number conflates:

* **coverage variance** — does it answer about a given account at all?
* **decision variance** — when it answers, does it answer the same thing?

**First: one avenue is closed at the API.** `temperature` is *deprecated* on Claude 5 models and returns HTTP 400. Sampling variance cannot be dialled out. (There is also no seed parameter.) So the remedy has to be a better-posed question, not a colder one.

**Measured, k=5, cold, sample 2:**

| | balance sheet | income statement |
|---|---|---|
| proposals per run | 18, 19, 14, 15, 14 | **34, 8, 8, 43, 8** |
| answered in *all* runs | 6 of 40 | 4 of 55 |
| answered in *some* runs | 18 | 43 |
| unanimous / split decisions | 19 / 2 | 2 / **33** |

**The income statement is bimodal, not noisy.** 34/8/8/43/8 is two distinct strategies, not a spread. And the 33 "split" accounts are not 33 independent coin flips — they are the *same* binary observed 33 times: `Operating Expenses` (one bucket) versus an invented group (`Occupancy & Office Expenses`, `Payroll & Benefits`, `Professional Fees`). The balance sheet shows the same thing on a different axis: the model alternates between mapping `Total Checking/Savings` and mapping `Checking`, `New Checking`, `Undeposited Funds` individually.

**Crucially, it is not confused about where an account goes.** Given that it answered, it named the same target in 19 of 21 balance-sheet cases; `Checking → Cash and Cash Equivalents` and `Visa → Other Payables` every single run. Only the *level* wobbles — which is exactly the rollup-vs-detail judgement §7.0b identified, left implicit in the prompt and therefore re-litigated on every call.

The prompt already said *"Never map both a subtotal and any of its components"*, and the model violated it anyway (see the sample 2 double-counts). **Instructions do not enforce invariants; structure does.**

#### Remedies considered

| Avenue | Verdict |
|---|---|
| `temperature=0` | **Impossible** — deprecated on Claude 5, HTTP 400. No seed parameter either. |
| Self-consistency / k-run majority vote | **Rejected.** Wrong tool for a *cascading* decision: voting per account on a whole-statement strategy can elect an incoherent mixture (some rows rolled up, some broken out) that double-counts or strands accounts. Costs k×. And the residual splits already self-report low confidence (0.59, 0.62), so the review sheet's 0.85 threshold routes them to a human anyway. |
| Lower effort | Not pursued — treats the symptom, and effort was not the variable. |
| Force a statement-wide level | **Tried, rejected.** Collapsed variance on sample 2 (BS 14/14/14/14; IS 4/5/5/5; ~65% cheaper) but cost TS 8 of its 16 income-statement labels: the TS ground truth is *mixed within one statement* — revenue on its own lines (`Freight Income`, `Finance Revenues`), expenses rolled into `SG&A Expenses`. |
| Ask the level per group (`AUTO` + `break_out` + second pass) | **Tried, rejected — it made variance worse.** |

#### The negative result, in full

TS, `--delivered`, 2 runs per arm:

| arm | e2e | coverage | BS e2e | IS e2e | block placement |
|---|---|---|---|---|---|
| `all` (control) | 75%, 73% | 87%, 90% | 86%, 86% | 44%, 38% | 83%, 82% |
| `auto` | **77%, 62%** | 88%, 67% | 86%, 73% | 50%, 31% | 85%, 63% |

`auto` produced both the best run and the worst — range 62–77% against the control's 73–75%.

**Why it backfired, and the general lesson.** Under `AUTO` the first pass shows only subtotals. When the model maps a subtotal rather than breaking it out, its detail rows are never shown at all, and `reconcile` then excludes them as covered. Whether it breaks out is the same coin flip, now with far more leverage on the outcome.

> **Filtering what the model may see converts a *recoverable* wrong mapping into an *unrecoverable* silent rollup.** Showing everything means the worst case is a bad proposal a human fixes in review; showing less means the worst case is an account that was never considered.

That asymmetry, not the variance number, is what decides the default. `ALL` stays.

#### The second answer key settles what the level actually depends on

Commercial Flooring's delivered model (`Draft Schedules ... (12.31.25)v2.xlsx`) is the second ground truth. On the surface it is the **mirror image** of TS:

| | revenue | operating expenses |
|---|---|---|
| **TS Distributors** | detail — `Freight Income`, `Finance Revenues` on their own lines | **rollup** — `Total Operating Expenses` → `SG&A Expenses` |
| **Commercial Flooring** | **rollup** — `Total COGS` → `Cost of Revenue` | detail — 8 named lines (Rent, Insurance, Payroll, …) |

Had we hard-coded a level policy from either client, it would have been wrong for the other. But one rule explains both, and it is not house style:

> **Map at the level where the client's group corresponds to exactly one template line.**

Flooring's evidence, from a single statement:
* `Total Checking/Savings` → `Cash & Cash Equivalents` — the whole group is cash, one line, **roll up**.
* `Machinery and Equipment`, `Furniture and Equipment`, `Autos and Trucks` → three *different* template lines, **break out**.
* `Total Insurance Expense` → `Insurance` — a *nested* subtotal rolled up, while its siblings in the same expense group were broken out individually.
* `Payroll Expenses` + `Payroll Taxes` → one line, `Payroll Expense` — **group two details**, neither rollup nor one-to-one.

So the level is a function of **the template's capacity for that particular group**, which is a semantic judgement about what the accounts mean. That is precisely the judgement we can only get by showing the model everything and letting it decide — which is the evidential case for `ALL` that the A/B could only argue negatively.

**Two further confirmations from the same file:**

1. **`Other Operating Expenses` = `'Dec 25 P&L'!C43 - SUM(K21:K28)`** — the analyst mapped the `Total Expense` rollup *and subtracted the eight lines already broken out*. That is the third option §7.0c reserved, and it is why `reconcile` treats opposite signs as legitimate rather than as a double-count. The construction is real and in production use.
2. **Both write paths are exercised.** The income statement shows a **+3 row shift** — three rows inserted into the operating-expense block (5 placeholder slots became 8 named lines) — plus 8 `NAME_SLOT` renames. The balance sheet used renames only, including a *cascade*: `Other Payables` → `Credit Cards` at row 38, and `Accrued Expenses` → `Other Payables` at row 39.

**Also measured: the analyst is not internally consistent either.** Nine mappings change partway through the history — `Accounts Payable` reads `Total Accounts Payable` in FY2020-22 and the detail `Accounts Payable` in FY2025; `Other Payables` reads `Total Other Current Liabilities` early and `Retainage` at the end. The rollup-vs-detail call genuinely wobbles *for the human expert too*, across years of one engagement. That is the strongest possible evidence that it is a judgement call to be surfaced and pinned, not a bug to be engineered out (§7.4).

#### What the variance actually means

The honest reading is that rollup-vs-detail is **not a defect to engineer out of the LLM layer** — it is a real judgement call that different competent analysts would answer differently, and the model's inconsistency is an accurate reflection of that. So the design answer is not determinism, it is *containment*:

1. **`reconcile` makes any level choice safe** — exactly-once holds whichever way it lands (§7.0c).
2. **Review surfaces the choice**, including the auto-exclusions, so it is visible and reversible.
3. **The profile pins it.** What matters for a valuation is not that a cold first run is reproducible, but that **year 2 matches year 1** — comparability across periods. That is the profile's job, and it makes wiring profile persistence the highest-value remaining work rather than any further prompt engineering.

**Standing rule: no LLM change may be attributed on a single run.** Two identical runs already scored 43 then 42 correct. Every A/B here runs both arms ≥2× with `--delivered`, and `--level all` is kept as the unfiltered control so any future policy stays measurable. Note the fresh control measured 73–75% e2e where §7.2 records 80–82% — that earlier figure was the top of a range, not a fixed point.

---

## 8. Validation and reconciliation — the feature that sells this

Run every time; ship as a report alongside the model.

**Per source file**
- Client's own subtotals recompute from their components.
- Total Assets = Total Liabilities + Equity.
- Every account row is classified (data / subtotal / section header).

**Cross-year**
- File N's comparative column == file N−1's current column, per account. *(This is the check that catches the FY2022/23 class of error.)*
- Chart-of-accounts diff year over year: added / removed / renamed, each requiring acknowledgement.

**Post-mapping — coverage proof**
- For each template block: `Σ(mapped source values) == template block subtotal`, per year.
- Every source account mapped or explicitly excluded.
- Rebuilt Total Assets / Total L&E tie back to the client's own reported totals, per year.

**Post-write**
- Force full recalc via COM, read back, and confirm `BS!<balance check row>` is blank for every year.
- Regenerate and diff against the previous run when re-running an existing engagement.

**Adjustments** are reported as their own layer: ingested value → adjustment → final value, with the analyst's rationale attached.

---

## 9. Row planning and formula generation

1. Compute the required target-line set per block = mapped targets ∪ template defaults that must persist.
2. Allocate within the block: fill template rows in canonical order → repurpose unused rows (relabel) → insert additional rows only if still short. Never cross a `Do Not Change` / `Do not add row` anchor.
3. Execute inserts **through COM, top-down, before writing values**, so Excel updates every downstream reference.
4. Re-resolve all target row indices *after* inserts — never cache pre-insert row numbers.
5. Generate mapping formulas as contiguous `SUM(range)` where the source rows are adjacent, else `SUM(r1:r2, r3)` — matching the house style already in the delivered model, so a reviewer sees familiar formulas.
6. Rewrite block subtotal formulas to span the final row range.
7. Write `Inputs` (valuation date, entity name, # historical periods, FYE) from the extraction.

**Design rule: emit formulas, not values.** The analyst must be able to click any standardized line and trace it back to the client account. This preserves reviewability and keeps the tool's output indistinguishable in kind from hand-built work.

---

## 10. User flow

1. **New / open engagement** → pick client, or start from a saved profile.
2. **Drop in client workbooks** (any number of years).
3. **Auto-detect + validate** → show the extraction summary and every validation finding. Blocking errors must be resolved; warnings acknowledged.
4. **Review mapping** → a two-panel screen: client accounts left, template lines right. Pre-filled from the profile. Unmapped items surface at the top. Confidence is visible; anything below threshold is highlighted.
5. **Adjustments** (optional) → add named adjustments with rationale.
6. **Generate** → pick the template, write to a new copy, recalc, verify.
7. **Report** → reconciliation report + the populated model. Profile saved for next year.

Steps 3–5 collapse to a single confirmation screen on re-runs, which is the common case.

---

## 11. Build phases

| Phase | Deliverable | Rough effort |
|---|---|---|
| **0 — Extract + validate** | **DONE.** See "Phase 0 outcome" below. | ~1 week |
| **1 — Mapping engine** | Alias dictionary, profile persistence, L0–L3 matching, **L4 LLM proposal**, arithmetic verification, review UI. Gate: scores against `eval/ts_distributors_mapping.yaml` and reproduces the delivered model's mapping. | ~2.5 weeks |
| **2 — COM writer** | **DONE.** See "Phase 2 outcome" below. | ~2 weeks |
| **3 — Ship** | Packaging (`.exe`), GUI polish, docs, pilot on a second real client. | ~1 week |
| **4 — Second client** | Prove generalization on a real non-TS chart of accounts. The honest test of the whole design. | ~1 week |
| **5 — Wire it up** | **DONE.** `prepare` / `build` CLI, job directories, profile persistence connected, `.env` loading, README. See "Phase 5 outcome" below. | — |

Phase 0 is deliberately standalone: it delivers real value on its own (it alone would have caught both historical errors) and de-risks everything after it.

### Phase 5 outcome — wiring the pipeline found four bugs that unit tests could not

`fsa/job.py` + two CLI commands (`prepare`, `build`) now run the whole thing:
files in → review workbook → populated BVAL model → saved profile. Verified end
to end on Commercial Flooring: 96 accounts read from client PDFs, mapped, written
through Excel COM, balance checks read back per year.

Everything below this seam already had unit tests and passed them. Composing the
parts is what surfaced these, and it is the argument for wiring earlier than
feels necessary:

1. **Nothing loaded `.env`.** The eval scripts each imported a scratchpad
   helper that did it; no shipping code did. `--llm` could never have worked for
   an actual user. Fixed with `fsa/env.py` (stdlib, `.env` is a default that the
   real environment overrides).
2. **`requirements.txt` listed 3 of 6 dependencies** — no `PyYAML`, no
   `PyMuPDF`, no `anthropic`. A clean install could not have run.
3. **Omitting `--client` produced an unreadable job.** `save_profile` happily
   wrote `client_name: ''`; `load_profile` rejects it. `prepare` succeeded and
   `build` then failed on its own output.
4. **The review sheet lost decisions when two client rows shared a display
   label.** Commercial Flooring prints `Total Credit Cards` twice at two nesting
   levels; the consolidator disambiguates by section, but `import_review` keyed
   on the visible text, so one row's decision was applied to both and the other
   was reported as an unknown account. Silent data loss. The sheet now carries a
   hidden identity column.

**A fifth was a real correctness bug, not plumbing.** `reconcile` covered the
descendants of a *mapped* rollup but never the mirror case: a client subtotal
whose members are all accounted for. Those were left UNRESOLVED, so the review
sheet asked the analyst to map `Total Current Assets` — and doing as asked
produces exactly the double-count the module exists to prevent. It is now an
explicit exclusion, by the same completeness test already used for conflicts.
On flooring this cut rows presented as open questions from 27 to 4.

The rule cannot move the value metric — it only ever converts an UNRESOLVED rule
into an exclusion, and the scorer skips UNRESOLVED rules — so it is a review-
surface and correctness fix, not an accuracy claim. `reconcile(...,
cover_complete_rollups=False)` exists to A/B it against ground truth without LLM
variance on top.

**Profile replay, measured.** Second run on the same client with `--profile` and
`--no-llm`: 51 proposed, 41 auto-excluded, **0 accounts needing manual mapping,
$0 in API cost.** The "year 2 is nearly free" claim in §7.1 is no longer a
design intention.

### Phase 4 outcome — ingest generalizes; the reader seam works

Sample 2 (Commercial Flooring) was run **cold**: no aliases added, no thresholds touched, nothing inspected before the first run. Ingest now reads `.pdf`, `.ods` and `.xlsx` through `fsa/ingest/readers/*` into `RawDoc`, and `fsa/ingest/interpret.py` turns any of them into the existing `StatementSet`.

**Result from the original PDFs:** 12 columns (BS + IS × FY2020–2025), 41-row BS and 55-row IS consolidated tables, **0 errors**, 6 warnings — all six genuine rename candidates for an analyst, e.g. `Checking → New Checking`.

**Arithmetic, the check that cannot be fooled:** 141 subtotals across all 12 columns, **141 tie exactly**, and **every balance sheet balances**. On a client and a file format the code had never seen.

New checks and what forced them:

| Change | Why |
|---|---|
| `check_hierarchy` | Depth makes subtotal scope exact, so *every* level can be checked — nested groups and grand totals included, which `subtotal_mismatch` deliberately declines to guess at. |
| `subtotal_mismatch` stands down when depth exists | It cannot see where a nested group starts, so it charges a parent with its older siblings. All 12 warnings it produced on sample 2 were false; e.g. `Total Insurance Expense` flagged at 118,705.50 against a correct 90,608.30, the gap being exactly the five unrelated expense rows printed above the group. |
| Positional label/value split | `Visa AE - 6777` is an account name containing a card number. A numeric regex read 6777 as the balance and broke FY2022 by $3,761.54. Values are now identified by the right-aligned gutter, not by looking like numbers. |
| Per-page indent ladder | Pooling x-offsets across pages mixed the BS margin (191.0) with the P&L margin (184.6), halving the inferred step and silently discarding 43 of 51 P&L rows. |
| `_plausible` gate | The P&L workbook ships a DataSnipper index sheet listing `2025 Balance Sheet.pdf`; the title pattern matched the *filename* and fabricated a statement out of the tool's progress figures. A document that names a statement is not one. |
| `period_inferred` | Conversion to Excel strips the masthead — `2025_Balance_Sheet.xlsx` has no period text at all, only a tab reading `Dec 2025`. Inferred periods are read but flagged, never treated as printed fact. |
| Dedup on statement + period + content | The 2020–2024 PDF repeats FY2020 verbatim, but the copies carry different print timestamps, so page-level hashing misses them; content alone would wrongly merge distinct years. |

**Measured cost of the team's manual PDF→Excel step:** identical numbers, but the converted files yield **12 spurious subtotal warnings and one unreadable period** where the original PDFs yield **zero of each**, because the conversion discards the indentation that carries the hierarchy. Reading the PDF directly is both less work for the team and strictly more accurate.

**Regression note:** the sample directory was reorganised into `sample_1_TS/`, which had silently turned the TS acceptance and COM-writer tests into skips. Paths repointed; suite is **247 passing, 0 skipped**, so sample 1 is provably unaffected by all of the above.

### Phase 4 outcome, part 2 — mapping on a second client, and the exactly-once invariant

Cold deterministic run on sample 2: **BS 26%, IS 2%** of DATA rows resolved, against much higher on TS. Diagnosed before changing anything, and the diagnosis says *do not tune*:

* **The misses are semantic, not lexical.** `Merrill Lynch → Cash and Cash Equivalents` scores 0.33 on string similarity; `Checking → Cash and Cash Equivalents` 0.35; `Visa → Other Payables` 0.27. No threshold or alias list reaches these. The deterministic layers correctly returned *unresolved* rather than guessing — which is the designed behaviour, and the case for the LLM layer.
* **The one near-threshold candidate would have been wrong.** `New Investments → Investments` scored 0.85 against a 0.86 cut-off. It sits under `Schwab` under `Checking/Savings` — a brokerage sweep account, i.e. a *current* asset, whereas the template's `Investments` line lives in Other Assets. Lowering the threshold to "fix" this one sample would have bought an error. **`FUZZY_THRESHOLD` stays at 0.86.**
* **`IS 2%` was measuring the wrong thing.** The IS template has 12 target lines; the client has 46 detail accounts. The correct mapping is `Total Income → Revenue`, `Total COGS → Cost of Revenue`, `Total Expense → Operating Expenses` (PLAN 7.0b). Counting "rows with a rule" scores the right answer as near-zero.

#### The exactly-once invariant (`fsa/mapping/reconcile.py`)

No single layer can enforce it, because it is a property of the decision *set*. Sample 2 produced the canonical failure: the alias layer matched the detail `Accounts Receivable` at 0.95, the LLM independently matched the rollup `Total Accounts Receivable` at 0.90, and both landed on the template's `Accounts Receivable` line. Each defensible alone; together a double-count that no check would have caught.

| Behaviour | Rationale |
|---|---|
| **Report double-counts, never resolve them** | Map-the-rollup, map-the-detail, and map-the-rollup-minus-the-detail are all legitimate; the delivered TS model uses the third (`=...+Z16-SUM(Z136:Z137)`). Opposite signs are therefore *not* a conflict. |
| **Cover descendants of a mapped rollup** | `Total Expense → Operating Expenses` accounts for the 30 rows beneath it. Excluding them with a reason naming the rollup is a deterministic consequence of the hierarchy, and keeps 40 settled rows out of the review sheet. |
| **`ASSIGN` on a placeholder becomes `NAME_SLOT`** | Otherwise the delivered model ships `Other Income (Expense) 2` — the template's filler text — as a line item. |
| **`coverage()` counts accounted-for, not ruled** | The metric fix implied above. |

Measured after adding it: **3 real double-counts caught on the BS**, and accounting reaches **100% of DATA accounts on both statements** (from 26% / 2% under the old metric). On TS the pass is a correct no-op (0 conflicts, 0 exclusions), since TS's subtotals are still unresolved at the deterministic stage.

**Honest caveat — LLM non-determinism.** Two identical cold runs differed materially: BS left 17 vs 21 unresolved, IS left 47 vs 11. Same prompt, same model, same effort. This is why the reconcile pass and human review are the safety net, and why no LLM proposal may reach a delivered model unreviewed (§7.1). It also means **single-run LLM scores are not a reliable metric** — any future tuning must average several runs.

**Cost:** ~$0.21 per client for both statements (2 calls, Sonnet 5, medium effort, ~6.8k in / 12.4k out).

**No ground truth exists for client 2** — Weaver has not produced a BVAL model for it — so precision is unmeasurable here. What is measured is coverage, conflict detection and arithmetic. Precision still rests on TS alone.

### Phase 2 outcome

`fsa/write/plan.py` (pure row allocation + formula generation) and
`fsa/write/com_writer.py` (COM driver). **183 tests green**, including Excel-dependent ones.
End-to-end: a populated BVAL model in ~18s.

Verified on the output file, not on the writer's own claims:

| Check | Result |
|---|---|
| New `#REF!` cells | **0** (the 44 BS / 14 IS error cells are pre-existing `#DIV/0!` in the blank template; our write *resolved* 31 of them) |
| Sheets | 49 = 47 template + 2 Historical tabs |
| `FS Charts` chart objects | 9 -> 9 (the openpyxl failure mode, avoided) |
| Template file | byte-identical, never opened for writing |
| Orphan `EXCEL.EXE` | 0 |
| Sample formula | `BS!K9 = SUM('Historical BS'!F2:F3)` -> 7,242,782.92, matching the delivered model |

**Two bugs, both found only by end-to-end verification:**

1. `Application.Calculation` is rejected before a workbook is open -- calculation mode belongs
   to the active workbook's window, not the application. Set it after opening.
2. **The dangerous one.** Inserts were planned at `block.last_row + 1`. Excel widens
   `=SUM(E9:E13)` only when the insertion point is *strictly inside* the range, so inserting
   at row 14 left the formula untouched, pushed the subtotal down, and orphaned the new row
   outside its own total -- written, visually correct, reaching no total. The balance check
   named it exactly: off by `Employee Advances` in all five years. Correct position is the
   highest **non-frozen** row inside the block (inserting at `last_row` would displace a
   `Do not add row` line). Both locked behind regression tests.

The one residual imbalance is not a defect: FY2022 is off by exactly -$3,000,000, the
`Certificate of Deposit` that has no ground-truth mapping because Weaver's misaligned
consolidation filed that $3M under `Accounts Receivable - Trade` (§2.4). **The balance check
re-detected the original misalignment from the opposite direction.**

**Insert path stress-tested.** TS needed one insert per statement, so the riskiest code had
almost no exercise. Forcing every current-asset account onto its own dedicated line drove
**19 inserts into a 5-row block**: the subtotal moved from row 14 to 33, Excel expanded
`SUM(E9:E13)` to `SUM(K9:K32)`, members summed to the subtotal exactly
(35,678,702.62 -- the client's own reported Total Current Assets for FY2025), and zero `#REF!`
appeared. The insert path holds under load, not just for a single row.

Not yet built: the review UI. `build_plan(require_confirmed=True)` refuses unconfirmed LLM
proposals by design, so today confirmation happens in code.

### Phase 0 outcome

Built and passing: `fsa/model/schema.py`, `fsa/ingest/{normalize,discover,extract}.py`,
`fsa/consolidate.py`, `fsa/validate/{checks,report}.py`, `fsa/cli.py`. 106 tests green,
including `tests/test_acceptance.py` against the real sample files.

```
python -m fsa.cli ingest "<client dir>" --exclude "Dec 2020 -2025 Financias (Weaver edited).xlsx" \
       --audit-against "<...>/Dec 2020 -2025 Financias (Weaver edited).xlsx"
```

Acceptance criterion met, with zero manual configuration:
- Five client workbooks ingested; BS 69 accounts x FY2021-25, IS 108 accounts x FY2021-25.
- FY2022 and FY2023 flagged `row_alignment_suspected` (ERROR), correctly sized at 10 and 11
  consecutive accounts, naming the `Certificate of Deposit` / \$3,000,000 pair.
- FY2021 and FY2024 correctly **not** flagged.
- FY2025's differences reported as `value_delta`, not misalignment.
- Exit code 1 on the two genuine errors.

**What Phase 0 taught us, beyond the plan:**

1. **Prior-period restatement is routine** (PLAN 2.4 addendum / SPEC "Prior-period restatements").
   This forced the severity policy: ERROR only when *our* output is untrustworthy.
2. **The `subtotal_mismatch` check needed two corrections** that generalize beyond this client.
   Derived lines (`Total Assets`, `Gross Margin`, `Net Profit/Loss`) are not the sum of the block
   above them, and lines like `EBITDA` are subtotals despite carrying no "Total" in their name --
   left unhandled, `EBITDA` was classified as data and its \$7.2M polluted the next block's sum.
   Both produced confident, wrong warnings. Fixed via `is_total_label` / `is_derived_line`.
   The general lesson for Phase 1: **a check that cannot be made reliable should stay silent
   rather than guess.** False positives cost more trust than missing findings gain.
3. **Rename detection only pairs accounts across adjacent years.** `Accounts Receivable - Lawler`
   (FY2023 only) and `Accounts Receivable - Indital` (FY2025 only) are plausibly one account, but
   are not paired because neither appears in the year the other vanishes. Immaterial here (Lawler
   is 0.00) but a real limitation to revisit when mapping profiles land in Phase 1.
4. **openpyxl's `insert_rows` limitation is confirmed as the Phase 2 pivot** -- nothing in Phase 0
   contradicts it, and reading stayed comfortably on openpyxl as planned.

---

## 12. Testing

**Golden regression: TS Distributors.** The delivered model is the oracle — with one deliberate caveat.

> **The delivered model contains the FY2022/FY2023 misalignment.** A blind "match the human output" test would therefore encode two known errors as requirements. Instead: build a **curated baseline** = the delivered model's `BS`/`IS` tabs with those nine-row cascades corrected. The generated output must match the curated baseline cell-for-cell, and must *differ* from the as-delivered model in exactly the expected cells. Those expected divergences are asserted explicitly in the test.

Also:
- **Unit tests** per pipeline stage on small synthetic fixtures.
- **Property tests**: totals tie; no account silently dropped; mapping is idempotent; row planning never crosses an anchor.
- **Mutation fixtures** — synthetic client files that inject the failure modes actually observed: dropped row, renamed account, inserted account, changed label capitalization, shifted comparative column, scale change to thousands.
- **COM integration test** on a scratch copy of the real template, asserting post-recalc balance checks and that charts / conditional formatting / add-in sheets survive.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| COM automation is brittle (Excel dialogs, stuck processes, version drift) | Strict context manager: `DisplayAlerts=False`, explicit `Quit()` in `finally`, orphan-process reaping. Integration-test on a real template copy in CI-equivalent. |
| Next client's format differs far more than TS's | Detection heuristics all degrade to a human confirmation step. The profile system means format novelty costs one session, not a rewrite. |
| Analysts don't trust a black box | Emit formulas not values; ship the reconciliation report every run; keep the review UI mandatory on first run per client. |
| Template version changes | Read the taxonomy from the template at runtime (labels + anchor markers in col A) rather than hardcoding row numbers. Fail loudly on an unrecognized template. |
| Mapping profile rot across analysts | Profiles are YAML, git-tracked, diffable, with `decided_by` / `decided_at` provenance on every rule. |
| Scope creep into valuation judgment | Hard line: v1 populates `Historical BS`/`IS` and standardized `BS`/`IS` only. CapEx, Adj, Rent, SEAM stay manual. |

---

## 14. Open questions for Weaver

1. **Template stability** — how often does the BVAL template change, and is there a version marker in it we can key off?
2. **Profile sharing** — network share, or git repo, or bundled with the app?
3. **Should the tool flag suspected errors in *past* delivered models?** The FY2022/23 finding suggests real value in a retro-audit mode, but that's a sensitive conversation for the firm to have on its own terms.
4. **Multi-entity clients** — TS has per-location tabs (Houston, Chicago, …). Does any engagement need those mapped separately, or is the consolidated view always sufficient?
5. **Interim periods** — the template supports a stub period (`Inputs!B10 = "Year End" | "Interim"`, TTM vs annualized YTD). Is that in scope for v1, or year-end only?

---

## 15. First concrete step

Build Phase 0 against the six TS files. Success criterion, stated up front so it's falsifiable:

> The tool ingests all six client workbooks with zero manual configuration, emits a clean multi-year consolidated table, and its validation report independently flags the FY2022 and FY2023 one-row misalignments **and** correctly classifies the FY2025 differences as deliberate adjustments rather than errors.
