---
name: analyze
description: >-
  Answer data questions from a spreadsheet or CSV with checked numbers. Profiles the file first
  (messy dates, totals rows, duplicates, case variants), finds what drove a change with an exact
  segment and rate/mix breakdown, tests whether a difference is real, draws clean charts and builds a
  one-page report. Use when the user shares a CSV, Excel, JSON file or pasted data and asks "why did X
  drop", "is A better than B", "what is the trend", "is this significant", "analyze this", "how many
  users do I need", or wants a chart, KPI check or formal data report. Not for building dashboards or
  apps, forecasting or machine learning, or writing SQL pipelines.
argument-hint: "<question or file>"
license: Apache-2.0
metadata:
  brand: How to AI by Ruben Hassid
  version: 2.0.0
  updated: 2026-09-29
  source: https://github.com/anthropics/knowledge-work-plugins/blob/main/data/skills/analyze/SKILL.md
  source-license: Apache-2.0
---

# Analyze: answer data questions with numbers you can defend

Scripts do what models do badly (parse messy files, count, decompose, test, draw). You do the judgment: which
question, which definition, what the result means, what could be wrong. Python 3.8+, standard library only,
nothing to install. Run commands from this folder, like `python3 scripts/profile.py FILE` (use `python` if
`python3` is missing). Every script has `--help` and most have `--json`.

## Pick the level
- **Quick answer** (one number, a lookup): profile, define the metric, compute, answer in 1-3 lines.
- **Full analysis** (why, compare, trend): steps 1-6. Lead with the finding, one chart, caveats, next questions.
- **Formal report** (stakeholders, decisions): full analysis plus step 7, with method and data-quality sections.

## Workflow
1. **Get the data.** Use the file path (CSV, TSV, XLSX, JSON, NDJSON). Pasted data: save it to a file, or pipe it
   with `-`. Ask only for what is missing: the question, the metric, the periods. Excel with several sheets:
   check `python3 scripts/dataio.py FILE` for the sheet list, then pass `--sheet NAME`.
2. **Profile first, always.** `python3 scripts/profile.py FILE`. Read the TRUST REPORT at the top: at most 5
   issues that could change the answer, each with what to do. Ambiguous dates (03/04/2026): ask which order
   the file uses, never guess, then pass `--date-order dmy|mdy`. Case variants, totals rows, duplicates,
   negative counts: handle them and say so in the answer. `--write-clean OUT.csv --log LOG.txt` makes a
   conservatively cleaned copy and logs every change; it never deletes a row.
3. **Define the metric before computing.** `python3 scripts/stats.py define churn` gives formula, denominator,
   common mistakes and a sanity range (`--list` shows all). State the definition in the answer. If the user's
   definition differs, use theirs and say so.
4. **Analyze with the scripts, not by hand.**
   - Why did it change? `python3 scripts/drivers.py FILE --metric revenue --date order_date --a 2026-07 --b 2026-08`.
     Ratios (conversion: `--ratio orders,sessions`; average order value: `--ratio revenue,count`) split the change exactly into
     rate (segments got better or worse) and mix (traffic moved between segments). `--auto` compares the latest
     complete period with the one before. It ranks dimensions, drills into the top segment and runs a HISTORY
     CHECK ("within normal wobble" or not).
   - Is the difference real? `stats.py compare` (numbers by group), `props` (conversion, churn), `chi2` (two
     categories), `corr` (two numbers). Which test, and what the verdict lines mean: see the table below.
   - Trend, odd point, sample size? `stats.py trend`, `anomaly`, `ci` (bootstrap), `power` (how many users).
5. **Chart** (`python3 scripts/chart.py line|bar|hbar|scatter|hist|waterfall ... --out x.svg`). One idea per
   chart, `--title` states the takeaway, zero-based bars, direct labels. The revenue bridge:
   `drivers.py ... --json > d.json` then `chart.py waterfall --from-drivers d.json --out bridge.svg`.
6. **Validate before presenting.** Row counts match the profile. Shares from `drivers.py` add to 100%. Magnitudes
   sit inside the sanity range in `assets/metrics.json`. No unexplained gaps or partial periods (trend, anomaly and
   charts drop partial first and last periods and say so; drivers warns). Segment totals equal the overall total. A number that looks too
   good is a bug until proven otherwise.
7. **Report** (formal level). `python3 scripts/report.py --example > r.json`, fill it (fields in
   `assets/report.schema.json`), `python3 scripts/report.py r.json --check`, then `--out report.html`. One
   self-contained page: headline, confidence with reason, KPI cards, charts, tables, method, caveats, next questions.

## Which statistic
| Question | Command | Read the verdict as |
|---|---|---|
| Do two groups differ on a number? | `stats.py compare FILE --value V --group G` | difference with CI, Welch t and rank test; they should agree |
| Did conversion or churn differ? | `stats.py props FILE --group G --success S` (or `--a 96/1000 --b 126/1000`) | difference in points, relative lift, Fisher used when counts are small |
| Are two categories linked? | `stats.py chi2 FILE --a A --b B` | p says if, Cramer's V says how strongly |
| Do two numbers move together? | `stats.py corr FILE --x X --y Y` | r with CI; big gap between Pearson and Spearman means outliers or a curve |
| Rising or falling? | `stats.py trend FILE --value V --date D --by month` | slope with CI; "no clear trend" is a real answer |
| Is this month odd? | `stats.py anomaly FILE --value V --date D` | flags are leads, not errors |
| How sure is this average? | `stats.py ci FILE --value V --stat median` | seeded bootstrap interval |
| How many observations? | `stats.py power props --p1 0.10 --lift 0.2 --daily 800` | per-group n and days |

## Rules that keep the answer honest
- Answer first, then the evidence. Never report a number the scripts did not produce; quote them as printed.
- State confidence as high, medium or low with the reason. High: clean data, large n, methods agree, change well
  outside normal wobble. Medium: one open caveat (definition unconfirmed, borderline p, partial data). Low: small
  n, unresolved ambiguity, one source, or nothing to compare against.
- Significance is not importance: give the size of the effect and whether it matters. "Not significant" means no
  reliable difference detected, not proof of none.
- Correlation is not cause; `drivers.py` says where a change sits, not why. Offer reasons as hypotheses to check.
- Many comparisons produce false alarms: say how many you ran. Never stop a test the moment it looks good.
- Say what data would change the conclusion (more months, the missing region, a control group, the raw refunds).
- Rates: quote points for differences of rates (2% to 3% is +1 point, +50% relative). Never average percentages;
  recompute from totals. Compare like periods (same month last year for seasonal data).

## When code cannot run
Ask the user to paste the data (or read the file directly). Do the same checks by hand: row count, empty cells,
duplicates, ranges, subtotals versus total, a metric definition read from `assets/metrics.json`. Show the
arithmetic, use only numbers you recomputed, and label the result "unverified: computed without the scripts".

## Warehouse (one short section)
If a SQL warehouse connector is available, explore the schema, write the query that returns one row per order or
event (or pre-aggregated by date and the dimensions), check the row count and nulls, save the result as CSV and
run the same scripts on it. If it fails, fix column names and dialect and retry. Show the query for reproducibility.

## Limits
Reads CSV, TSV, JSON, NDJSON, XLSX (not old .xls; save as .xlsx). Up to 250 MB, but files over about 100 MB take
10-30 seconds per step and about eight times their size in memory: say so, or use `--max-rows` to sample. Dates
are never guessed when day and month could swap. Percent columns are stored in points (12 means 12%). No
forecasting, no machine learning, no network.

## File map
| Path | Open or run it when | What you get |
|---|---|---|
| `scripts/dataio.py` | first look at a file, sheet list, type check | typed table; columns, types, warnings |
| `scripts/profile.py` | always, before any number | TRUST REPORT, column stats, cleaned copy + log |
| `scripts/drivers.py` | "what drove the change", ratio metrics | exact segment breakdown, rate/mix, drill, history check, waterfall bridge |
| `scripts/stats.py` | tests, trend, bootstrap, sample size, metric lookup | verdicts with CI and cautions; `define` glossary lookup |
| `scripts/chart.py` | any chart | clean SVG (line, bar, hbar, scatter, hist, waterfall) |
| `scripts/report.py` | formal report | one self-contained HTML page from JSON |
| `scripts/_parse.py`, `scripts/_xlsx.py`, `scripts/_dist.py`, `scripts/_svg.py` | never run; internal parts | number/date parsing, XLSX reader, distributions, SVG toolkit |
| `assets/metrics.json` | before defining any metric | formula, denominator, mistakes, sanity range for 38 metrics |
| `assets/report.schema.json` | filling a report | field list and rules for `report.py` |
| `NOTICE` | attribution | derived from Anthropic's Apache-2.0 `analyze`; modified by How to AI |
