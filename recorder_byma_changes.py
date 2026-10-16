#!/usr/bin/env python3
"""Preserve observed BYMADATA state changes, not an exchange tick feed."""
import argparse
import gzip
import hashlib
import json
import logging
import math
from pathlib import Path
import time
from datetime import datetime
from uuid import uuid4

import requests
from recorder_byma_intraday import ART, Client, RateLimited, atomic_json, market_bounds, utcnow

LOG = logging.getLogger(__name__)
KEY_FIELDS = ('symbol', 'settlementType', 'denominationCcy', 'market')
PANELS = (('options', {}, 0), ('leading-equity', {'page_size': 100}, 0),
          ('general-equity', {'page_size': 500}, 30), ('cedears', {}, 60))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def instrument_key(item):
    if not item.get('symbol'):
        raise ValueError('Missing instrument symbol')
    return canonical([str(item.get(field, '')) for field in KEY_FIELDS])


class ChangeLog:
    def __init__(self, output, baseline_seconds=300, clock=time.monotonic):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.session_id = str(uuid4())
        self.baseline_seconds, self.clock = baseline_seconds, clock
        self.previous, self.last_baseline, self.last_date, self.latest = {}, {}, {}, {}

    def append(self, category, date, record):
        path = self.output / f'byma_{category}_v3_{date}.jsonl.gz'
        payload = (canonical(record) + '\n').encode('utf-8')
        with path.open('ab') as handle:
            handle.write(gzip.compress(payload, compresslevel=6, mtime=0))
            handle.flush()
        return path

    def record(self, panel, items, started, received, http=None):
        indexed = {instrument_key(item): item for item in items}
        if len(indexed) != len(items):
            raise ValueError(f'Duplicate keys in {panel}')
        if panel == 'options' and any(item.get('securityType', 'OPT') != 'OPT' for item in items):
            raise ValueError('Non-option in options response')
        now = self.clock()
        date = datetime.fromisoformat(received).astimezone(ART).date().isoformat()
        old = self.previous.get(panel, {})
        # Compare source fields only: receipt times and request IDs are not prices.
        changed = [indexed[key] for key in sorted(indexed) if key not in old or indexed[key] != old[key]]
        removed = sorted(set(old) - set(indexed))
        baseline = (panel not in self.previous or self.last_date.get(panel) != date
                    or now - self.last_baseline[panel] >= self.baseline_seconds)
        identity = {'schema_version': 3, 'session_id': self.session_id,
                    'response_id': str(uuid4()), 'panel': panel,
                    'source': 'BYMADATA public delayed snapshot', 'nominal_delay_minutes': 20,
                    'request_started_at_utc': started, 'received_at_utc': received}
        if baseline or changed or removed:
            event = {**identity, 'event': 'baseline' if baseline else 'delta',
                     'key_fields': KEY_FIELDS, 'upserts': [indexed[k] for k in sorted(indexed)] if baseline else changed,
                     'removed_keys': [] if baseline else [json.loads(k) for k in removed]}
            self.append('events', date, event)
        digest = hashlib.sha256(canonical([indexed[k] for k in sorted(indexed)]).encode()).hexdigest()
        observation = {**identity, 'status': 'ok', 'contracts': len(items),
                       'changed_instruments': len(changed), 'removed_instruments': len(removed),
                       'duplicate': not changed and not removed and panel in self.previous,
                       'baseline_written': baseline, 'source_state_sha256': digest, 'http': http or {}}
        self.append('observations', date, observation)
        self.previous[panel] = indexed
        self.last_date[panel] = date
        if baseline:
            self.last_baseline[panel] = now
        self.latest[panel] = {**identity, 'records': items}
        if baseline:
            self.persist()
        return observation

    def error(self, panel, started, exc):
        received = utcnow()
        response = getattr(exc, 'response', None)
        self.append('observations', received.astimezone(ART).date().isoformat(), {
            'schema_version': 3, 'session_id': self.session_id, 'panel': panel,
            'status': 'error', 'request_started_at_utc': started,
            'received_at_utc': received.isoformat(), 'error': str(exc),
            'http_status': response.status_code if response is not None else (429 if isinstance(exc, RateLimited) else None),
            'retry_after_seconds': exc.seconds if isinstance(exc, RateLimited) else None})

    def persist(self):
        if self.latest:
            options = self.latest.get('options', {}).get('records', [])
            equities = [row for name, panel in self.latest.items() if name != 'options' for row in panel['records']]
            needed = {x['underlyingSymbol'] for x in options if x.get('underlyingSymbol')}
            available = {x['symbol'] for x in equities if x.get('symbol')}
            atomic_json(self.output / 'byma_latest_v3.json', {
                'schema_version': 3, 'session_id': self.session_id, 'panels': self.latest,
                'coverage': {'required_option_underlyings': sorted(needed),
                             'collected_underlyings': sorted(available),
                             'missing_underlyings': sorted(needed - available)},
                'note': 'Independent delayed snapshots; not simultaneous exchange events'})


def run(args):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    client = Client(output)
    log = ChangeLog(output, args.baseline_seconds)
    deadline = time.monotonic() + args.duration_seconds
    interval, streak, fatal = args.interval_seconds, 0, False
    next_due = {}
    stats = {'started_at_utc': utcnow().isoformat(), 'successful_responses': 0,
             'duplicate_responses': 0, 'changed_instruments': 0, 'errors': 0, 'rate_limits': 0}
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
                    if not args.duration_seconds or cycle + wait >= deadline:
                        break
                    time.sleep(wait)
                    continue
            wait, failed = interval, False
            for panel, payload, refresh_seconds in PANELS:
                # Do not begin a second request after the recording window ends.
                if args.duration_seconds and time.monotonic() >= deadline:
                    break
                if args.market_hours and utcnow().astimezone(ART) >= closing:
                    break
                if time.monotonic() < next_due.get(panel, 0):
                    continue
                started = utcnow().isoformat()
                try:
                    items, started, received = client.panel(panel, payload)
                    next_due[panel] = time.monotonic() + refresh_seconds
                    if panel in ('general-equity', 'cedears'):
                        needed = {x.get('underlyingSymbol') for x in log.latest.get('options', {}).get('records', [])}
                        items = [item for item in items if item.get('symbol') in needed]
                    observation = log.record(panel, items, started, received, client.http_metadata.get(panel))
                    stats['successful_responses'] += 1
                    stats['duplicate_responses'] += int(observation['duplicate'])
                    stats['changed_instruments'] += observation['changed_instruments']
                    LOG.info('%s: %d instruments, %d changed, %d removed, baseline=%s', panel, len(items),
                             observation['changed_instruments'], observation['removed_instruments'], observation['baseline_written'])
                except (RateLimited, requests.RequestException, ValueError) as exc:
                    log.error(panel, started, exc)
                    stats['errors'] += 1
                    failed = True
                    LOG.warning('%s: %s', panel, exc)
                    if isinstance(exc, RateLimited):
                        stats['rate_limits'] += 1
                        wait = max(wait, exc.seconds)
                        break  # One global cooldown for both endpoints.
                    response = getattr(exc, 'response', None)
                    if response is not None and 400 <= response.status_code < 500:
                        fatal = True
                        break
            if failed:
                streak = 0
                interval = min(900, max(60, interval * 2))
                wait = max(wait, interval)
            else:
                streak += 1
                if streak >= 10:
                    interval = max(args.interval_seconds, interval / 2)
                    streak = 0
                wait = max(0, interval - (time.monotonic() - cycle))
            if fatal or not args.duration_seconds or time.monotonic() + wait >= deadline:
                break
            if args.market_hours and (closing - utcnow().astimezone(ART)).total_seconds() <= wait:
                break
            time.sleep(wait)
    finally:
        log.persist()
        stats.update(finished_at_utc=utcnow().isoformat(), requests=client.requests, final_interval_seconds=interval)
        atomic_json(output / 'recorder_status_v3.json', stats)
        LOG.info('Session: %s', json.dumps(stats))
    return int(fatal or (stats['errors'] > 0 and stats['successful_responses'] == 0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='data/intraday')
    parser.add_argument('--interval-seconds', type=float, default=2)
    parser.add_argument('--duration-seconds', type=float, default=0, help='0: one cycle')
    parser.add_argument('--baseline-seconds', type=float, default=300)
    parser.add_argument('--market-hours', action='store_true')
    args = parser.parse_args()
    if (not all(math.isfinite(v) for v in (args.interval_seconds, args.duration_seconds, args.baseline_seconds))
            or args.interval_seconds < 2 or args.duration_seconds < 0 or args.baseline_seconds < 60):
        parser.error('interval >= 2s, duration >= 0, baseline >= 60s; all must be finite')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
