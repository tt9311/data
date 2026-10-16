# Argentina options research data

## Current collector (v3)

`recorder_byma_changes.py` records **observed changes in delayed snapshots**.
It is not exchange tick data. Polling more often does not remove the provider's
nominal 20-minute delay, reveal changes between polls, or establish exchange
latency, order-book queue position, or executable high-frequency opportunities.

```sh
# One cycle across the available panels:
python recorder_byma_changes.py

# A bounded recording session:
python recorder_byma_changes.py --duration-seconds 1500 --interval-seconds 2 --market-hours
```

- Options and leading equities are requested approximately every **2 seconds**,
  using one connection, with **at least one second between all request starts**.
  Slow responses and additional panels extend the interval; no catch-up bursts.
- The general equity panel is checked every 30 seconds, and CEDEARs every 60
  seconds, retaining the instruments referenced as underlyings by the options.
  These endpoints cover underlying symbols outside the leading panel.
- All source fields for retained instruments are preserved, including unknown
  future fields. No zero prices, absent Greeks, or open interest are imputed.
- Options and underlying states are independent. Each response has UTC request
  start/receipt times, a unique response ID, and selected HTTP/cache metadata.
  HTTP `Date` is not an exchange event timestamp. `tradeHour` is the source's
  last-trade clock and is not the timestamp of each bid/ask update.
- The leading panel retains both CI and 24h settlements. These must not be mixed
  as if they were the same instrument or hedge.
- Changes are compared using source fields, ignoring local capture times and
  response ordering. No old data is deleted.
- Each session, each new date, and every five minutes, a complete baseline is
  stored. Between baselines only changed instruments and removed keys are stored.
- Every successful response is logged, even if it repeats the previous state.
  Errors are logged separately. Duplicate data and collection outages are
  distinguishable. Removal means absent from the tracked panel, not necessarily
  an exchange delisting; supplemental panels track the current option universe.
- HTTP 429 honors `Retry-After`, including HTTP dates, with a minimum 60-second
  pause. The global cooldown survives restarts and pauses all endpoints.
  Errors reduce frequency; repeated successes gradually restore the target.
  There is no published, verified sustainable rate limit for this public endpoint.
- TLS certificates are validated. No fake auth tokens, proxies, or cache-busting.

### Files

- `data/intraday/byma_events_v3_YYYY-MM-DD.jsonl.gz`: baseline/delta events.
  Events identify `panel`, `session_id`, `response_id`, observation times,
  `upserts` (complete source records), and `removed_keys`.
- `data/intraday/byma_observations_v3_YYYY-MM-DD.jsonl.gz`: every response,
  including duplicate counts, source-state hash, and HTTP metadata, plus failures.
- `data/intraday/byma_latest_v3.json`: last full observed panel states, separate
  observation times and underlying coverage. Written at baselines and session end.
- `data/intraday/recorder_status_v3.json`: latest session request/error counts.

Gzip readers support the concatenated gzip members. To reconstruct a panel,
read events in file order: a `baseline` replaces its entire state; a `delta`
replaces/adds `upserts` and deletes `removed_keys`. Instrument identity is
`[symbol, settlementType, denominationCcy, market]`, converting each field to a
string (empty string when absent). Never use a later baseline to fill an earlier
observation. Different panels are not simultaneous; carry an explicit age and
choose only information already observed when joining them.

## What is missing for options and HFT research?

The current intraday options source exposes best bid/ask and sizes, last price,
last-trade clock, aggregate volume, OHLC/VWAP, option type, underlying and expiry.
It has **no explicit strike field, IV, or Greeks in the inspected responses**.
`openInterest` exists but was zero for GGAL/YPFD in the inspected intraday samples;
zero must not be assumed to mean verified absence of positions.

The IAMC daily history includes strikes and some end-of-day IV/Greeks. These
values have different observation times and model assumptions; they must not be
assigned to earlier intraday observations. Strike mapping needs validated exact
instrument/expiry matches, especially adjusted contracts. Multiplier, exercise
style, deliverable, corporate actions, rates, dividends, and expiry conventions
also need a trustworthy instrument/reference source before computing analytics.
Derived IV/Greeks should be stored separately with model version, inputs and
calculation times; they are not observed market events.

To test HFT execution or subsecond lead/lag, acquire an appropriately licensed
feed with exchange timestamps, sequencing/recovery and trades/quotes, plus depth
or order-level events when required. This public snapshot archive cannot recover
missed ticks, cancellations, intrapoll reversals, or queue priority. It can support
exploratory liquidity/surface research and strategy development at its measured
resolution; profitability and fills are not validated by collection alone.

## Scheduled collection

Long-lived sessions start at 10:50 and 16:00 Argentina time, Monday–Friday.
They share a concurrency group, run in 25-minute chunks, checkpoint after each
chunk, and stop at 18:00 ART to include delayed closing data. Each job stays
below GitHub's six-hour hosted-runner limit. GitHub may delay starts; this is not
an uninterrupted feed or precision scheduler. No exchange holiday calendar yet.
A running job uses its checked-out code version; a new version may queue until
the previous job ends.

IAMC runs separately at 19:30 ART. Failures are visible while captures are
preserved. Fallback Actions artifacts are retained for 14 days. IAMC alerts if
no new report is found and the master is over seven calendar days old; it does
not invent missing reports.

Manually run **Record Argentina Options Data** with mode `session`, `snapshot`,
or `daily`. Recorder/workflow changes also start a session; data-only pushes do
not. The v2 CSV recorder remains available as `recorder_byma_intraday.py` for
compatibility, but the workflow now uses v3. Existing v2 and legacy files remain.

## Legacy repair

Forty rows in `options_intraday_2026-10-14.csv` omitted `optionType`, shifting
subsequent fields. They were realigned, and CALL/PUT restored only from
unambiguous exact-symbol matches in existing snapshots. The audit trail is in
`data/intraday/legacy_repair_2026-10-14.json`.

## Validation

```sh
python -m unittest discover -s tests -v
```
