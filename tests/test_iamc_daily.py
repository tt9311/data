import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch
import recorder_iamc_daily as iamc


class IAMCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.clock = patch.object(iamc, 'get_argentina_time', return_value=datetime(2026, 10, 21, tzinfo=timezone.utc))
        self.clock.start()
    def tearDown(self):
        self.clock.stop();os.chdir(self.cwd);self.tmp.cleanup()
    def master(self, value):
        p = Path('data/daily/options_iamc_master_history.csv');p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('fecha_reporte\n'+value+'\n')
    def test_stale_source_is_visible_without_claiming_fresh_data(self):
        self.master('2026-10-05')
        status = iamc.check_freshness(date(2026, 10, 5))
        self.assertEqual(status['status'], 'source_stale')
        self.assertFalse(status['fresh'])
        self.assertEqual(status['dataset_age_days'], 16)
        self.assertEqual(json.loads(Path('data/daily/iamc_status.json').read_text()), status)
    def test_published_report_missing_locally_remains_failure(self):
        self.master('2026-10-05')
        with self.assertRaisesRegex(RuntimeError, 'published report'):
            iamc.check_freshness(date(2026, 10, 20))
        self.assertEqual(json.loads(Path('data/daily/iamc_status.json').read_text())['status'], 'local_lagging')
    def test_index_dates_are_validated_sorted_and_deduplicated(self):
        html = b'<a href="/Informe/InformeDiarioOpciones05102026/">x</a><a href="/Informe/InformeDiarioOpciones20102026/">x</a><a href="/Informe/InformeDiarioOpciones05102026/">x</a><a href="/Informe/InformeDiarioOpciones99132026/">x</a><a href="/Informe/InformeDiarioOpciones22102026/">future</a>'
        with patch.object(iamc.urllib.request, 'urlopen', return_value=io.BytesIO(html)):
            self.assertEqual(iamc.discover_reports(), [date(2026, 10, 5), date(2026, 10, 20)])
    def test_changed_or_inaccessible_index_does_not_silently_pass(self):
        with patch.object(iamc.urllib.request, 'urlopen', return_value=io.BytesIO(b'<html>Login</html>')):
            with self.assertRaisesRegex(RuntimeError, 'no recognized'):
                iamc.discover_reports()
    def test_latest_advertised_report_is_downloaded_even_outside_lookback(self):
        class Response(io.BytesIO):
            status = 200
        page = Response(b'<a href="https://www.iamc.com.ar/sample.pdf">PDF</a>')
        with patch.object(iamc.urllib.request, 'urlopen', side_effect=[page, Response(b'%PDF-test')]):
            pending = iamc.download_missing_iamc(source_reports=[date(2026, 10, 5)])
        self.assertEqual(pending[0][1], '05102026')
        self.assertEqual(Path(pending[0][0]).read_bytes(), b'%PDF-test')
    def test_report_date_mismatch_cannot_overwrite_history(self):
        self.master('2026-10-05')
        with patch.object(iamc, 'discover_reports', return_value=[date(2026, 10, 20)]), patch.object(iamc, 'download_missing_iamc', return_value=[('report.pdf', '20102026', True)]), patch.object(iamc, 'parse_pdf_file', return_value=[{'fecha_reporte': '2026-10-05'}]):
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                iamc.main()
        self.assertEqual(Path('data/daily/options_iamc_master_history.csv').read_text(), 'fecha_reporte\n2026-10-05\n')
