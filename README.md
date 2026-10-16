# Argentina options research data

## Intraday recorder

`recorder_byma_intraday.py` collects the **delayed** public BYMADATA options
panel and the leading-equity panel. Polling more often does not remove the
provider's nominal 20-minute delay or guarantee tick-by-tick coverage.

```sh
# One snapshot, useful for diagnostics:
python recorder_byma_intraday.py

# Record for 25 minutes, targeting one full snapshot every 30 seconds:
python recorder_byma_intraday.py --duration-seconds 1500 --interval-seconds 30 --market-hours

# Read the new daily files:
python -c "import pandas as pd; print(pd.read_csv('data/intraday/options_intraday_v2_2026-10-16.csv.gz').head())"
```

- Version 2 uses a fixed CSV schema. Missing fields remain empty; additional
  source fields are retained in `extra_json`.
- New files are `data/intraday/options_intraday_v2_YYYY-MM-DD.csv.gz`.
  Ordinary gzip readers and pandas support the concatenated gzip members.
- Both requests have separate UTC start/receive timestamps. `snapshot_time_art`
  is the options response receipt time, **not** the market event time.
- `underlying_spot_source` distinguishes last trade from closing-price fallbacks.
  Enrichment failures are marked explicitly and do not discard captured options.
- The minimum snapshot interval is 30 seconds. All requests, including the
  underlying request, are spaced at least one second apart. No load test is used
  to discover the provider's blocking threshold.
- HTTP 429 honors `Retry-After`, including HTTP dates, with a minimum 60-second
  pause. The cooldown survives restarts. Failures slow collection up to a
  15-minute interval; a longer server-requested cooldown is always respected.
- `recorder_status.json` records counts and errors for the latest session.
  TLS certificates are validated; there are no fabricated auth tokens.

## Scheduled collection

The workflow starts long-lived sessions at 10:50 and 16:00 Argentina time,
Monday–Friday. Sessions share a concurrency group, run in 25-minute chunks,
checkpoint data after each chunk, and stop collection at 18:00 ART to allow
capture of delayed closing data. Each job stays below the six-hour hosted-runner
limit. GitHub may delay job starts; this setup does not promise uninterrupted
coverage or a precision scheduler. Weekdays are filtered, but there is no
exchange-holiday calendar yet.

IAMC runs separately at 19:30 ART. Collection failures fail the job while
preserving available captures. A fallback Actions artifact is retained for
14 days if pushing a checkpoint fails. The IAMC recorder alerts if no new report
is found and the master is over seven calendar days old; it does not invent
missing reports.

From GitHub Actions, run **Record Argentina Options Data** manually using mode
`session`, `snapshot`, or `daily`. Pushing changes to the recorder/workflow also
starts a session. Pushes containing only collected data do not start another run.

## Legacy data

Legacy `.csv` files are preserved separately from the v2 schema. Forty rows in
`options_intraday_2026-10-14.csv` originally omitted `optionType`, shifting all
subsequent fields when read with the header. They were realigned, and CALL/PUT
was restored only using unambiguous exact-symbol matches in other saved
snapshots. See `data/intraday/legacy_repair_2026-10-14.json` for the affected lines.
This repair does not turn off-hours snapshots into market-hours observations.

The IAMC master and BYMADATA snapshots have different timestamps, coverage, and
pricing conventions. They should not be joined as simultaneous executable quotes.

## Validation

```sh
python -m unittest discover -s tests -v
```
