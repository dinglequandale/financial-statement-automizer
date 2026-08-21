# Held-out clients, and the prediction they test

Written **before** either new client was run, and committed first on purpose:
a prediction recorded after seeing the result is not a prediction.

## The discipline

Five client engagements exist. Three of them (TS Distributors, Commercial
Flooring, AOK Holdings) have been used to build and fix this tool, so every
number measured against them is to some degree fitted. They can no longer
answer "does this generalise?" -- only "does it still work?".

That leaves exactly two clients whose data has never influenced a line of this
codebase, and they are spent the moment they are used:

| Client | Status | Purpose |
|---|---|---|
| `sample_4_Ram Rod` | **In use** | The prediction test below. Run blind, then fixed against. |
| `sample_5_Thompson Custom Homes` | **SEALED** | Opened once, after the sample-4 fixes, to find out whether those fixes generalised or merely fitted sample 4. |

Sample 5 is deliberately **not** registered in `tools/burden.py`. Adding it
before the sample-4 work is finished destroys the only remaining independent
check this project has, and no amount of later care recovers it.

## The prediction

Stated in full so it can fail cleanly:

> A new client requires **no new dimension in the data model** and **no new
> pattern list**. It requires only new *values* in slots that already exist.

The reasoning: a financial statement column is identified by a tuple the domain
fixed long ago -- entity, period end, period length, currency, basis. Those are
dimensions, and they come from accounting, not from whichever client happened to
arrive first. Vocabulary (how a client words a period caption, what it names a
roll-up tab) is unbounded and therefore belongs to a model or to the analyst,
never to a growing regex.

The distinction that makes this testable:

* **A new dimension** = the column identity tuple was incomplete. Prediction
  fails. Example: a client reporting in two currencies, which no current field
  can represent.
* **A new pattern list** = a hard-coded vocabulary had to grow to cope.
  Prediction fails. Example: adding `"Combined Statements"` to the roll-up words
  because one client used it.
* **A new value in an existing slot** = working as designed. Example: a period
  caption the structural parser declines and the model resolves; a roll-up tab
  the analyst picks with `--scope`.

## How sample 4 is scored against it

1. Extract the answer key mechanically; do not read it.
2. Run ingest only, with no mapping and no fixes, and record every failure.
3. Classify each failure as *dimension*, *pattern list*, or *value*.
4. Only then fix.

If step 3 turns up a dimension or a pattern list, the prediction has failed and
the design should be revisited rather than patched -- which is the entire point
of having written this down first.
