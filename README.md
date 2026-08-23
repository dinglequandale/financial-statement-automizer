# Financial Statement Automizer

Turns a client's financial statements -- however they arrive -- into a populated
Weaver BVAL model, with a mandatory human review step in the middle.

It is a **drafting tool, not an autopilot.** It reads the files, proposes a
mapping, catches double-counts and out-of-balance years, and hands you a
checklist. You approve or correct it; only then does anything get written.

---

## For analysts: the window

Two double-clicks, once each:

1. **`Install.cmd`** — run this one time. It sets everything up and tells you
   what to do if Python is missing.
2. **`Launch Automizer.cmd`** — this is the tool. Use it every time.

The window asks for four things: the client's name, the folder holding whatever
they sent, the blank BVAL template, and — on a repeat client — last year's saved
decisions. Then it reads the statements, opens the review sheet in Excel, and
writes the model once you have corrected column D.

Everything it produces goes in `Documents\BVAL Models\<client>\<date>`. You
never have to choose a folder.

### When you are done

Press **Package this job to send**. It makes one small zip beside the job
folder holding what was read, what the tool suggested, what you changed, and
the notes it raised. Not the model -- that is yours.

Send that file back. The gap between what was suggested and what you chose is
how the tool learns which lines you actually want broken out.

### One thing to watch for

If a client sends **one spreadsheet with every year side by side** -- 2019 in
one column, 2020 in the next -- the tool currently reads the first column only,
and says so:

> *A file lays out several columns and only the first was read.*

If those column headings are dates or years, stop and send that file on rather
than building from it. If they are company names (a group with subsidiaries),
nothing is missing and you can carry on.

**It stops rather than guesses.** If a year's figures do not add up, if one
period covers eight months instead of twelve, or if a file holds several
companies and it cannot tell which one you are valuing, it says so in plain
English and refuses to build. That is the point: a model you cannot trust is
worse than no model.

---

## For everyone else: the command line

```
pip install -e .
```

That installs an `fsa` command. (`pip install -r requirements.txt` still works
if you would rather not install the package; then use `python -m fsa.cli`.)

If `fsa` is not found afterwards, Python's Scripts directory is not on your
PATH — a common default on Windows. Either add it, or keep using
`python -m fsa.cli`, which is always equivalent:

```
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```

Windows with Excel installed is required to *write* a model -- the BVAL template
is filled through Excel itself, never through a Python library, because the
template's formulas do not survive a round trip through `openpyxl`. Ingest,
mapping and review work anywhere.

For the suggestion step, put your key in a `.env` file next to this README:

```
ANTHROPIC_API_KEY=sk-ant-...
```

It is picked up automatically. Without it the tool still runs -- it falls back
to the deterministic matching layers and leaves the rest for you.

---

## The two commands

### 1. `prepare` -- read the files, draft the mapping

```
python -m fsa.cli prepare "path\to\client files" ^
    --template "BVAL Model (Weaver Template).xlsx" ^
    --job jobs\acme-2025 ^
    --client "Acme Flooring, Inc."
```

Point it at a folder or at individual files. It reads `.pdf`, `.xlsx` and
`.ods`; anything else in the folder is ignored.

This creates a **job directory**:

| file | what it is |
|---|---|
| `review.xlsx` | **the only file you open.** One row per client account. |
| `findings.json` | every validation finding, in full |
| `job.json` | what was read, from where, against which template |
| `proposed.yaml` | the machine's mapping, before your review |

Useful flags:

- `--profile last-year.yaml` — replay a saved client profile. **This is the
  single biggest lever in the tool**: measured across all three sample clients,
  it takes the expected saving from 39–63% to 60–71% and drops the rows needing
  a from-scratch decision to nearly zero. Always pass it on a repeat client.
- `--scope consolidated` — which entity to value, when a workbook holds several.
  Group statements often ship one tab per subsidiary plus a roll-up; the tool
  prefers a tab that names itself a roll-up and says so, and stops with an error
  when it cannot tell. This flag is the answer.
- `--no-llm` — deterministic layers only. No API calls, no cost.
- `--exclude "name.xlsx"` — skip a file (repeatable).

### What it refuses to do

Ingest declines rather than guesses, because a wrong period or a wrong entity
silently misstates a valuation while a refusal costs you one question:

- **a bare year is not a year end.** `FY2023` does not say when the client
  closes, so it is reported, not assumed to be December 31.
- **a stub is not a year.** A March balance sheet or a trailing-twelve-month
  income statement among December year ends stops the run (`period_basis_mismatch`).
  On a real engagement this caught a file named `...balance sheet - 2024.xlsx`
  whose every tab read *"for the period ended June 30, 2024"*.
- **a subsidiary is not the group.** Four tabs claiming one fiscal year is a
  question, not a coin flip.

### 2. Review

Open `review.xlsx`. **Edit column D only.** Everything else is context.

- Rows needing attention are at the top, biggest amounts first.
- Red = no proposal, we need you. Amber = low confidence. Green = confirmed.
- Column D has a dropdown of every valid template line, plus `-- EXCLUDE --`.
  Free text is allowed, so you can name a line the tool never considered.
- **Leaving a row alone means you accept it.** That is what review means.
- `-- EXCLUDE --` drops the account. Rows already showing it were excluded
  automatically -- usually because a rollup that contains them is mapped, or
  because their own components are mapped individually. Overtype to reverse.

Save and close.

### 3. `build` -- write the model

```
python -m fsa.cli build --job jobs\acme-2025 ^
    --out "Acme BVAL Model.xlsx" ^
    --save-profile profiles\acme.yaml
```

Excel opens invisibly, the template is copied (never modified), rows are
inserted where the client needs lines the template lacks, formulas are written,
and the balance check is read back and reported per year.

`--save-profile` is what makes next year cheap: it records every decision you
confirmed. Pass it back to `prepare` next time via `--profile`.

Other flags: `--overwrite` to replace an existing output, `--visible` to watch
Excel work (debugging).

---

## Reading the output

`prepare` ends with something like:

```
  BS: 41 rows, years 2020, 2021, 2022, 2023, 2024, 2025
  validation: no errors, 6 warning(s) -- see findings.json
  llm: BS: 35 proposed ($0.06); IS: 54 proposed ($0.08)
  2 double-count conflict(s) resolved automatically
  104 row(s) to review: 54 proposed, 10 auto-excluded, 40 need mapping
       (+27 client subtotal(s) left alone)
```

"Client subtotals left alone" is normal and correct. If `Total Current Assets`
has all its members mapped, mapping the total as well would count that money
twice, so the tool deliberately does not.

`build` ends with a per-year balance check. **`OUT BY` is the number to care
about.** It usually means accounts were left unmapped or excluded, not that the
arithmetic is wrong -- but either way it is not a finished model.

Exit codes: `0` clean, `1` errors or unmapped accounts (output still written so
you can look at it), `2` the run could not start.

---

## What it is good and bad at

**Good:** reading messy sources. Client PDFs, team-converted spreadsheets, and
OpenOffice files all go through the same path. On a client it had never seen,
every one of 141 subtotals added up and no balance sheet was out of balance.

**Good:** catching mistakes. It refuses to let one account reach the model
twice, and it checks arithmetic the delivered models have historically got
wrong.

**Weak:** picking the right template line first time on a *new* client. Expect
to correct a meaningful share of the proposals. It is measured against two real
delivered models and the numbers are in `PLAN.md` §7.2 -- read them before
setting expectations.

**Strong on repeat clients.** Once a profile exists, the same client next year
is close to a confirm-and-go.

---

## Development

```
python -m pytest              # full suite
python -m pytest -m "not sample"   # skip tests needing real client data + Excel
python tools/score_mapping.py TS --llm    # label accuracy vs delivered models
python tools/value_score.py FLOOR --llm   # do the block subtotals reproduce
```

`PLAN.md` is the design record, including the measurements behind every
non-obvious decision and the negative results.
