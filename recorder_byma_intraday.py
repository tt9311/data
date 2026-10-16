#!/usr/bin/env python3
"""Record delayed BYMADATA panels once or in a rate-aware recording session."""
import argparse
import csv
import gzip
import io
import json
import logging
import os
from pathlib import Path
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4
import requests

ART = timezone(timedelta(hours=-3))
BASE_API = 'https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free'
LOG = logging.getLogger(__name__)
FIELDS = [
    'snapshot_time_art', 'symbol', 'underlyingSymbol', 'optionType',
    'underlying_spot', 'underlying_bid', 'underlying_offer', 'underlying_vwap',
    'trade', 'bidPrice', 'offerPrice', 'quantityBid', 'quantityOffer',
    'tradeVolume', 'volumeAmount', 'vwap', 'openInterest', 'imbalance',
    'maturityDate', 'daysToMaturity', 'tradeHour', 'closingPrice',
    'openingPrice', 'tradingHighPrice', 'tradingLowPrice', 'settlementPrice',
    'previousSettlementPrice', 'description', 'numberOfOrders', 'tickDirection',
    'securityDesc', 'securitySubType', 'previousClosingPrice', 'settlementType',
    'denominationCcy', 'market', 'volume', 'securityType', 'snapshot_date',
    'schema_version', 'snapshot_id', 'source_delay_minutes',
    'options_request_started_at_utc', 'options_received_at_utc',
    'underlyings_request_started_at_utc', 'underlyings_received_at_utc',
    'underlying_spot_source', 'enrichment_status', 'extra_json',
]

def utcnow():
    return datetime.now(timezone.utc)

def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)

def retry_seconds(value, now=None):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - (now or utcnow())).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60

class RateLimited(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f'HTTP 429: wait at least {seconds:.0f}s')

class Client:
    def __init__(self, output, session=None, clock=time.monotonic, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({'User-Agent': 'BYMA-research-recorder/2.0', 'Accept': 'application/json'})
        self.clock, self.sleep = clock, sleep
        self.last_request = None
        self.requests = 0
        self.http_metadata = {}
        self.cooldown_file = output / 'byma_cooldown.json'
        self.not_before = None
        if self.cooldown_file.exists():
            self.not_before = datetime.fromisoformat(json.loads(self.cooldown_file.read_text())['not_before_utc'])

    def panel(self, name, payload):
        if self.not_before and self.not_before > utcnow():
            raise RateLimited((self.not_before - utcnow()).total_seconds())
        if self.last_request is not None:
            self.sleep(max(0, 1.0 - (self.clock() - self.last_request)))
        self.last_request = self.clock()
        started = utcnow().isoformat()
        self.requests += 1
        response = self.session.post(f'{BASE_API}/{name}', json=payload, timeout=(10, 25))
        received = utcnow().isoformat()
        self.http_metadata[name] = {
            'status': response.status_code,
            'latency_seconds': (datetime.fromisoformat(received) - datetime.fromisoformat(started)).total_seconds(),
            'decoded_body_bytes': len(response.content),
            'headers': {key: response.headers[key] for key in (
                'Date', 'Age', 'ETag', 'Last-Modified', 'Cache-Control', 'Content-Encoding', 'X-Cache'
            ) if key in response.headers},
        }
        if response.status_code == 429:
            seconds = max(60, retry_seconds(response.headers.get('Retry-After')))
            self.not_before = utcnow() + timedelta(seconds=seconds)
            atomic_json(self.cooldown_file, {'not_before_utc': self.not_before.isoformat()})
            raise RateLimited(seconds)
        response.raise_for_status()
        data = response.json()
        items = data.get('data') if isinstance(data, dict) else data
        if not isinstance(items, list) or not items or not all(isinstance(x, dict) for x in items):
            raise ValueError(f'Empty or invalid {name} panel')
        return items, started, received

def collect(client):
    options, started, received = client.panel('options', {})
    # Sequential requests are not simultaneous quotes. Preserve both times.
    spots, spot_started, spot_received, enrichment = {}, '', '', 'ok'
    try:
        equities, spot_started, spot_received = client.panel('leading-equity', {'T1': True, 'page_size': 100})
        spots = {x['symbol']: x for x in equities if x.get('symbol') and str(x.get('settlementType')) == '2'}
    except (requests.RequestException, ValueError, RateLimited) as exc:
        enrichment = 'rate_limited' if isinstance(exc, RateLimited) else 'failed'
        LOG.warning('Underlying enrichment unavailable: %s', exc)
    snapshot_id = str(uuid4())
    captured = datetime.fromisoformat(received).astimezone(ART)
    rows, seen = [], set()
    for item in options:
        if not item.get('symbol') or item.get('securityType', 'OPT') != 'OPT':
            raise ValueError('Non-option or missing symbol in options panel')
        if item.get('optionType') not in (None, '', 'CALL', 'PUT'):
            raise ValueError('Invalid optionType in options panel')
        key = (item['symbol'], item.get('settlementType'))
        if key in seen:
            raise ValueError(f'Duplicate instrument: {key}')
        seen.add(key)
        row = {key: item.get(key, '') for key in FIELDS}
        row.update(schema_version=2, snapshot_id=snapshot_id, snapshot_time_art=captured.isoformat(),
                   snapshot_date=captured.date().isoformat(), source_delay_minutes=20,
                   options_request_started_at_utc=started, options_received_at_utc=received,
                   underlyings_request_started_at_utc=spot_started, underlyings_received_at_utc=spot_received,
                   enrichment_status=enrichment if enrichment != 'ok' else 'missing_underlying')
        spot = spots.get(item.get('underlyingSymbol'))
        if spot:
            source = next((k for k in ('trade', 'closingPrice', 'previousClosingPrice') if spot.get(k)), '')
            row.update(underlying_spot=spot.get(source, ''), underlying_spot_source=source,
                       underlying_bid=spot.get('bidPrice', ''), underlying_offer=spot.get('offerPrice', ''),
                       underlying_vwap=spot.get('vwap', ''), enrichment_status='ok')
        row['extra_json'] = json.dumps({k: v for k, v in item.items() if k not in FIELDS}, ensure_ascii=False, sort_keys=True)
        rows.append(row)
    return rows

def save_snapshot(output, rows):
    # A new version never appends new fields under a legacy CSV header.
    path = output / f"options_intraday_v2_{rows[0]['snapshot_date']}.csv.gz"
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        with gzip.open(path, 'rt', encoding='utf-8', newline='') as handle:
            if next(csv.reader(handle)) != FIELDS:
                raise ValueError(f'Unexpected CSV schema: {path}')
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    if not exists:
        writer.writeheader()
    writer.writerows(rows)
    # Concatenated gzip members avoid re-compressing earlier snapshots.
    with path.open('ab') as handle:
        handle.write(gzip.compress(buffer.getvalue().encode('utf-8'), compresslevel=6, mtime=0))
        handle.flush()
        os.fsync(handle.fileno())
    atomic_json(output / 'options_intraday_latest.json', rows)
    return path

def market_bounds(now):
    local = now.astimezone(ART)
    return (local.replace(hour=11, minute=0, second=0, microsecond=0),
            local.replace(hour=18, minute=0, second=0, microsecond=0))

def run(args):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration_seconds
    client = Client(output)
    interval, streak = args.interval_seconds, 0
    summary = {'started_at_utc': utcnow().isoformat(), 'snapshots': 0, 'rows': 0, 'errors': 0, 'rate_limits': 0}
    failed = False
    try:
        while True:
            cycle = time.monotonic()
            if args.market_hours:
                local = utcnow().astimezone(ART)
                opening, closing = market_bounds(local)
                if local.weekday() >= 5 or local >= closing:
                    break
                if local < opening:
                    wait = (opening - local).total_seconds()
                    if args.duration_seconds == 0 or cycle + wait >= deadline:
                        break
                    time.sleep(wait)
                    continue
            wait = interval
            try:
                rows = collect(client)
                path = save_snapshot(output, rows)
                summary['snapshots'] += 1
                summary['rows'] += len(rows)
                LOG.info('Saved %s rows to %s', len(rows), path)
                streak += 1
                if streak >= 10:
                    interval = max(args.interval_seconds, interval / 2)
                    streak = 0
                wait = max(0, interval - (time.monotonic() - cycle))
                if client.not_before and client.not_before > utcnow():
                    raise RateLimited((client.not_before - utcnow()).total_seconds())
            except RateLimited as exc:
                summary['rate_limits'] += 1
                interval = min(900, max(60, interval * 2))
                streak, wait = 0, max(interval, exc.seconds)
                LOG.warning('%s; next attempt in %.0fs', exc, wait)
                if args.duration_seconds == 0:
                    failed = True
            except (requests.RequestException, ValueError) as exc:
                summary['errors'] += 1
                LOG.error('Snapshot failed: %s', exc)
                streak = 0
                interval = min(900, max(60, interval * 2))
                wait = interval
                if isinstance(exc, requests.HTTPError) and exc.response is not None and 400 <= exc.response.status_code < 500:
                    failed = True
                    break
            if not args.duration_seconds or time.monotonic() + wait >= deadline:
                break
            if args.market_hours and utcnow().astimezone(ART) + timedelta(seconds=wait) >= closing:
                break
            time.sleep(wait)
    finally:
        summary.update(finished_at_utc=utcnow().isoformat(), requests=client.requests, final_interval_seconds=interval)
        atomic_json(output / 'recorder_status.json', summary)
        LOG.info('Session: %s', json.dumps(summary))
    return 1 if failed or ((summary['errors'] or summary['rate_limits']) and not summary['snapshots']) else 0

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='data/intraday')
    parser.add_argument('--interval-seconds', type=float, default=30)
    parser.add_argument('--duration-seconds', type=float, default=0, help='0: collect once')
    parser.add_argument('--market-hours', action='store_true', help='Weekdays 11:00–18:00 ART, including delayed close')
    args = parser.parse_args()
    if args.interval_seconds < 30 or args.duration_seconds < 0:
        parser.error('interval must be >= 30 seconds and duration >= 0')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    return run(args)

if __name__ == '__main__':
    raise SystemExit(main())
