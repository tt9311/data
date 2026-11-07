import json
import sqlite3
import tempfile
import shutil
import uuid
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from analyze import report
from replay import markouts,events
from export import export
from serve import make_server
from store import Store

INSTRUMENT={'ticker':'TESTC100','description':'Opción compra GGAL AR$ 100.00 Vto. 18/12/2026','currency':'Pesos','market':'BYMA'}

def quote(bid,ask,bid_qty=50,ask_qty=50):
    return {'Ticker':'TESTC100','Type':'OPCIONES','Settlement':'A-24HS',
            'Date':'2026-10-19T15:00:00.000-03:00','Trade':False,'Price':0,'VarDay':0,
            'Bids':[{'Price':bid,'Quantity':bid_qty,'Position':1}],
            'Offers':[{'Price':ask,'Quantity':ask_qty,'Position':1}]}

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp_root=Path(__file__).resolve().parent.parent/'.test-data'
        self.tmp_path=self.tmp_root/uuid.uuid4().hex
        self.tmp_path.mkdir(parents=True)
        self.filename=self.tmp_path/'test.sqlite'
        self.store=Store(self.filename)
        self.meta=self.store.catalogue([INSTRUMENT],'GGAL')['TESTC100']
        self.store.session('session-1',{})
    def tearDown(self):
        self.store.close()
        target=self.tmp_path.resolve()
        if target.parent != self.tmp_root.resolve(): raise RuntimeError('Ruta temporal inesperada')
        shutil.rmtree(target)
    def ingest(self,msg,mono=1_000_000_000,session='session-1'):
        provider_ns=round(datetime.fromisoformat(msg['Date']).timestamp()*1e9)
        return self.store.ingest(msg,self.meta,timing={'session_id':session,
            'received_utc_ns':provider_ns+500_000_000,'received_monotonic_ns':mono})
    def seed(self):
        self.ingest(quote(100,101,90,10))
        self.ingest(quote(102,103),1_100_000_000)
        self.ingest(quote(104,105),1_200_000_000)
    def test_timing_raw_events_and_lag_are_preserved(self):
        self.ingest(quote(100,101))
        row=self.store.db.execute('SELECT apparent_timestamp_lag_ms,queue_delay_ns,message_sha256 FROM event_timing').fetchone()
        self.assertAlmostEqual(row[0],500,places=3)
        self.assertGreaterEqual(row[1],0)
        self.assertEqual(len(row[2]),64)
        raw=json.loads(self.store.db.execute('SELECT raw_message FROM ppi_events').fetchone()[0])
        self.assertEqual(raw['Bids'][0]['Quantity'],50)
    def test_quality_counts_duplicates_without_dropping_them(self):
        self.ingest(quote(100,101))
        self.ingest(quote(100,101),1_100_000_000)
        self.store.connection('session-1','disconnected')
        result=report(self.filename)
        self.assertEqual(result['events'],2)
        self.assertEqual(result['repeated_payloads_in_timing_sample'],1)
        self.assertEqual(result['connection_events']['disconnected'],1)
        self.assertAlmostEqual(result['feed_interarrival_ms_within_session']['p50'],100)
    def test_quote_markouts_use_delayed_entry_and_fees(self):
        self.seed()
        immediate=markouts(self.filename,latency_ms=0,horizon_ms=100,fee_bps=1)
        delayed=markouts(self.filename,latency_ms=50,horizon_ms=100)
        self.assertEqual(immediate['scored_signals'],1)
        self.assertAlmostEqual(immediate['mean_quote_markout_bps'],(102-101)/101*10000-2)
        self.assertAlmostEqual(delayed['mean_quote_markout_bps'],(104-103)/103*10000)
        self.assertEqual(markouts(self.filename,latency_ms=500,horizon_ms=100)['scored_signals'],0)
    def test_sessions_are_not_stitched_to_invent_an_exit(self):
        self.ingest(quote(100,101,90,10),session='session-1')
        self.store.session('session-2',{})
        self.ingest(quote(110,111),1_200_000_000,session='session-2')
        self.assertEqual(markouts(self.filename,latency_ms=0,horizon_ms=100)['scored_signals'],0)
    def test_illiquid_quote_does_not_fake_a_short_horizon(self):
        self.ingest(quote(100,101,90,10))
        self.ingest(quote(110,111),10_000_000_000)
        result=markouts(self.filename,latency_ms=0,horizon_ms=100,max_gap_ms=1000)
        self.assertEqual(result['scored_signals'],0)
        self.assertEqual(result['skipped_without_timely_quotes'],1)
    def test_export_contains_raw_and_arrival_order(self):
        self.seed()
        filename=self.tmp_path/'events.jsonl'
        self.assertEqual(export(self.filename,filename),3)
        rows=[json.loads(line) for line in filename.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[0]['raw']['Bids'][0]['Price'],100)
        self.assertLess(rows[0]['event_id'],rows[1]['event_id'])
    def test_read_api_auth_and_filters(self):
        self.seed()
        server=make_server(self.filename,port=0,token='test-only-token')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_address[1]}'
        try:
            with self.assertRaises(HTTPError) as error: urlopen(base+'/quotes')
            self.assertEqual(error.exception.code,401)
            request=Request(base+'/events?after=1&limit=1',headers={'Authorization':'Bearer test-only-token'})
            with urlopen(request) as response: data=json.load(response)
            self.assertEqual(data['count'],1)
            self.assertEqual(data['data'][0]['event_id'],2)
            request=Request(base+'/quotes?symbol=%27%20OR%201%3D1--',headers={'Authorization':'Bearer test-only-token'})
            with urlopen(request) as response: self.assertEqual(json.load(response)['count'],0)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)
    def test_external_bind_requires_token(self):
        with self.assertRaises(ValueError): make_server(self.filename,host='0.0.0.0')

if __name__=='__main__': unittest.main()
