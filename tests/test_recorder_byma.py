import csv
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta
import requests
import recorder_byma_intraday as recorder

OPTION = {'symbol': 'GFGC6000OC', 'underlyingSymbol': 'GGAL', 'optionType': 'CALL', 'securityType': 'OPT', 'settlementType': '2', 'maturityDate': '2026-10-16', 'bidPrice': 20, 'offerPrice': 22}

class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)

    def snapshot(self, item=None, equity_error=None):
        client = Mock()
        equity = {'symbol': 'GGAL', 'settlementType': '2', 'trade': 6000, 'bidPrice': 5990, 'offerPrice': 6010}
        started, received = '2026-10-16T17:00:00+00:00', '2026-10-16T17:00:01+00:00'
        client.panel.side_effect = [([item or OPTION], started, received), equity_error or ([equity], '2026-10-16T17:00:02+00:00', '2026-10-16T17:00:03+00:00')]
        return recorder.collect(client)

    def test_missing_fields_and_unknown_fields_do_not_shift_csv(self):
        recorder.save_snapshot(self.output, self.snapshot())
        changed = {**OPTION, 'new_api_field': 'retained'}
        del changed['optionType']
        recorder.save_snapshot(self.output, self.snapshot(changed))
        with gzip.open(next(self.output.glob('*.csv.gz')), 'rt') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['optionType'], '')
        self.assertEqual(rows[1]['underlying_spot'], '6000')
        self.assertEqual(json.loads(rows[1]['extra_json']), {'new_api_field': 'retained'})
        self.assertNotIn(None, rows[1])

    def test_enrichment_failure_retains_options_and_flags_missing_spot(self):
        row = self.snapshot(equity_error=requests.Timeout())[0]
        self.assertEqual(row['enrichment_status'], 'failed')
        self.assertEqual(row['underlying_spot'], '')
        self.assertEqual(row['bidPrice'], 20)

    def test_separate_receive_times_and_nominal_delay(self):
        row = self.snapshot()[0]
        self.assertNotEqual(row['options_received_at_utc'], row['underlyings_received_at_utc'])
        self.assertEqual(row['snapshot_time_art'], '2026-10-16T14:00:01-03:00')
        self.assertEqual(row['source_delay_minutes'], 20)

    def test_invalid_option_type_rejected(self):
        with self.assertRaises(ValueError):
            self.snapshot({**OPTION, 'optionType': 6000})

    def test_refuses_to_append_to_another_schema(self):
        path = self.output / 'options_intraday_v2_2026-10-16.csv.gz'
        with gzip.open(path, 'wt') as f:
            f.write('wrong,header\n')
        with self.assertRaises(ValueError):
            recorder.save_snapshot(self.output, self.snapshot())

    def test_retry_after_numeric_and_http_date(self):
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.assertEqual(recorder.retry_seconds('120', now), 120)
        self.assertEqual(recorder.retry_seconds('Fri, 16 Oct 2026 00:05:00 GMT', now), 300)
        self.assertEqual(recorder.retry_seconds('invalid', now), 60)

    def test_429_cooldown_survives_restart_without_another_request(self):
        session = Mock()
        response = Mock(status_code=429, headers={'Retry-After': '1200'})
        response.content = b''
        session.post.return_value = response
        client = recorder.Client(self.output, session=session)
        with self.assertRaises(recorder.RateLimited) as caught:
            client.panel('options', {})
        self.assertEqual(caught.exception.seconds, 1200)
        restarted = recorder.Client(self.output, session=session)
        with self.assertRaises(recorder.RateLimited):
            restarted.panel('options', {})
        self.assertEqual(session.post.call_count, 1)

    def test_global_request_spacing_includes_both_endpoints(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.content = b'[]'
        session.post.return_value.headers = {}
        session.post.return_value.json.return_value = [OPTION]
        sleep = Mock()
        client = recorder.Client(self.output, session=session, clock=lambda: 10, sleep=sleep)
        client.panel('options', {})
        client.panel('leading-equity', {})
        sleep.assert_called_once_with(1.0)

    def test_api_error_does_not_look_like_empty_success(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.content = b'{}'
        session.post.return_value.headers = {}
        session.post.return_value.json.return_value = {'status': 'ERROR'}
        with self.assertRaises(ValueError):
            recorder.Client(self.output, session=session).panel('options', {})

    def test_market_window_keeps_delayed_close(self):
        opening, closing = recorder.market_bounds(datetime(2026,10,16,20,40,tzinfo=timezone.utc))
        self.assertEqual(opening.hour, 11)
        self.assertEqual(closing.hour, 18)
        self.assertEqual(closing.utcoffset(), timedelta(hours=-3))

if __name__ == '__main__':
    unittest.main()
