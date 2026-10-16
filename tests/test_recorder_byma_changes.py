import copy
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from recorder_byma_changes import ChangeLog, instrument_key, run
from recorder_byma_intraday import RateLimited

OPTION = {'symbol':'GFGC6000OC','settlementType':'2','denominationCcy':'ARS','market':'BYMA','securityType':'OPT','optionType':'CALL','bidPrice':20,'offerPrice':22,'quantityBid':10,'unknown_future_field':{'value':1}}
START = '2026-10-16T17:00:00+00:00'
RECEIVED = '2026-10-16T17:00:01+00:00'

class ChangeLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.now = 0
        self.log = ChangeLog(self.output,clock=lambda:self.now)

    def read(self, category):
        rows=[]
        for path in sorted(self.output.glob(f'byma_{category}_*.jsonl.gz')):
            with gzip.open(path,'rt') as f:
                rows.extend(json.loads(line) for line in f)
        return rows

    def record(self, items, received=RECEIVED, panel='options'):
        return self.log.record(panel,items,START,received)

    def test_receipt_time_and_order_changes_are_not_market_changes(self):
        other={**OPTION,'symbol':'GFGV6000OC','optionType':'PUT'}
        self.record([OPTION,other])
        obs=self.record([other,OPTION],'2026-10-16T17:00:03+00:00')
        self.assertTrue(obs['duplicate'])
        self.assertEqual(len(self.read('events')),1)
        self.assertEqual(len(self.read('observations')),2)
        self.assertNotEqual(self.read('observations')[0]['response_id'],obs['response_id'])

    def test_replay_removes_disappeared_contracts_and_keeps_raw_extra_fields(self):
        other={**OPTION,'symbol':'GFGV6000OC','optionType':'PUT'}
        self.record([OPTION,other])
        changed={**OPTION,'bidPrice':21,'quantityBid':4}
        self.record([changed])
        state={}
        for event in self.read('events'):
            if event['event']=='baseline': state={}
            for item in event['upserts']: state[instrument_key(item)]=item
            for key in event['removed_keys']: state.pop(json.dumps(key,sort_keys=True,ensure_ascii=False,separators=(',',':')))
        self.assertEqual(state,{instrument_key(changed):changed})
        self.assertEqual(self.read('events')[1]['removed_keys'],[['GFGV6000OC','2','ARS','BYMA']])

    def test_underlying_stream_is_independent_and_preserves_both_settlements(self):
        self.record([OPTION])
        stock={'symbol':'GGAL','securityType':'CS','market':'BYMA','settlementType':'1','trade':6000}
        self.record([stock,{**stock,'settlementType':'2','trade':6010}],panel='leading-equity')
        self.assertEqual(len(self.read('events')),2)
        self.assertEqual(len(self.read('events')[1]['upserts']),2)
        self.assertEqual(self.log.latest['options']['records'],[OPTION])

    def test_periodic_baseline_even_when_nothing_changes(self):
        self.record([OPTION]);self.now=301
        obs=self.record([OPTION])
        self.assertTrue(obs['duplicate'])
        self.assertTrue(obs['baseline_written'])
        self.assertEqual([x['event'] for x in self.read('events')],['baseline','baseline'])

    def test_every_session_and_day_start_with_baselines(self):
        self.record([OPTION])
        restarted=ChangeLog(self.output)
        restarted.record('options',[OPTION],START,RECEIVED)
        self.record([OPTION],'2026-10-17T15:00:01+00:00')
        self.assertEqual(len(self.read('events')),3)
        self.assertTrue(all(x['event']=='baseline' for x in self.read('events')))

    def test_source_change_is_not_mutated_by_future_calls(self):
        item=copy.deepcopy(OPTION)
        self.record([item])
        updated=copy.deepcopy(OPTION);updated['unknown_future_field']['value']=2
        obs=self.record([updated])
        self.assertEqual(obs['changed_instruments'],1)
        self.assertEqual(self.read('events')[0]['upserts'][0]['unknown_future_field']['value'],1)

    def test_duplicate_keys_fail_without_writing_observations(self):
        with self.assertRaises(ValueError): self.record([OPTION,OPTION])
        self.assertEqual(self.read('observations'),[])

    def test_429_stops_both_panels_without_immediate_retry(self):
        args=SimpleNamespace(output_dir=str(self.output),baseline_seconds=300,duration_seconds=0,interval_seconds=2,market_hours=False)
        client=Mock(requests=1,http_metadata={})
        client.panel.side_effect=RateLimited(600)
        with patch('recorder_byma_changes.Client',return_value=client):
            self.assertEqual(run(args),1)
        client.panel.assert_called_once_with('options',{})
        self.assertEqual(self.read('observations')[0]['http_status'],429)
        self.assertFalse(list(self.output.glob('byma_events*')))

    def test_supplemental_panels_cover_missing_underlyings_without_extra_universe(self):
        args=SimpleNamespace(output_dir=str(self.output),baseline_seconds=300,duration_seconds=0,interval_seconds=2,market_hours=False)
        client=Mock(requests=4,http_metadata={})
        stock=lambda symbol: {'symbol':symbol,'settlementType':'2','market':'BYMA','securityType':'CS','trade':100}
        options=[{**OPTION,'underlyingSymbol':'BHIP'}, {**OPTION,'symbol':'VISTC100OC','underlyingSymbol':'VIST'}]
        client.panel.side_effect=[(options,START,RECEIVED),([stock('GGAL')],START,RECEIVED),
                                  ([stock('BHIP'),stock('UNRELATED')],START,RECEIVED),
                                  ([stock('VIST'),stock('UNRELATED')],START,RECEIVED)]
        with patch('recorder_byma_changes.Client',return_value=client):
            self.assertEqual(run(args),0)
        latest=json.loads((self.output/'byma_latest_v3.json').read_text())
        self.assertEqual(latest['coverage']['missing_underlyings'],[])
        self.assertEqual([x['symbol'] for x in latest['panels']['general-equity']['records']],['BHIP'])
        self.assertEqual([x['symbol'] for x in latest['panels']['cedears']['records']],['VIST'])

    def test_zero_quotes_and_absent_analytics_are_preserved_without_imputation(self):
        self.record([{**OPTION,'bidPrice':0,'quantityBid':0,'openInterest':0}])
        raw=self.read('events')[0]['upserts'][0]
        self.assertEqual(raw['bidPrice'],0)
        self.assertEqual(raw['openInterest'],0)
        self.assertNotIn('delta',raw)

if __name__=='__main__': unittest.main()
