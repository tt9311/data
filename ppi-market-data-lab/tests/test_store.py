import json
import unittest
from store import Store, metadata

INSTRUMENT = {'ticker':'GFGC5800DI','description':'Opción compra GGAL AR$ 5800.00 Vto. 18/12/2026',
              'market':'BYMA','currency':'Pesos'}

def message(trade=False, settlement='A-24HS'):
    return {'Ticker':'GFGC5800DI','Type':'OPCIONES','Settlement':settlement,
            'Date':'2026-10-19T15:08:15.100-03:00','Trade':trade,
            'Price':407.5 if trade else 0,'VarDay':-0.203432570322732 if trade else 0,
            'VolumeAmount':20,'VolumeTotalAmount':173327175.9,'OpeningPrice':509,
            'MaxDay':530,'MinDay':402,
            'Bids':[] if trade else [{'Price':407.5,'Quantity':55,'Position':1}],
            'Offers':[] if trade else [{'Price':410.964,'Quantity':6,'Position':1}]}

class PpiTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.meta = self.store.catalogue([INSTRUMENT], 'GGAL')['GFGC5800DI']
    def tearDown(self):
        self.store.close()
    def test_metadata_uses_description(self):
        self.assertEqual(self.meta['strike'],5800)
        self.assertEqual(self.meta['expiry'],'2026-12-18')
        self.assertEqual(self.meta['type'],'CALL')
        put = metadata({'ticker':'GFGV5800DI','description':'Opción venta GGAL AR$ 5800.00 Vto. 18/12/2026'})
        self.assertEqual(put['type'],'PUT')
    def test_book_zero_does_not_overwrite_last_trade(self):
        self.store.ingest(message(True),self.meta)
        state = self.store.ingest(message(False),self.meta)
        self.assertEqual(state['last'],407.5)
        self.assertAlmostEqual(state['change_percent'],-20.3432570322732)
        self.assertEqual(state['bid'],407.5)
        self.assertEqual(state['ask'],410.964)
    def test_trade_empty_book_does_not_remove_quotes(self):
        self.store.ingest(message(False),self.meta)
        state = self.store.ingest(message(True),self.meta)
        self.assertEqual(state['bid_size'],55)
        self.assertEqual(state['ask_size'],6)
        self.assertEqual(state['last'],407.5)
    def test_empty_book_and_settlements_are_distinct(self):
        self.store.ingest(message(False),self.meta)
        other = message(False,'INMEDIATA')
        other['Bids'], other['Offers'] = [], []
        self.store.ingest(other,self.meta)
        rows = self.store.db.execute('SELECT settlement,payload FROM ppi_latest').fetchall()
        states = {settlement:json.loads(payload) for settlement,payload in rows}
        self.assertEqual(states['A-24HS']['bid'],407.5)
        self.assertIsNone(states['INMEDIATA']['bid'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM ppi_events').fetchone()[0],2)
    def test_unknown_message_rejected(self):
        broken = message()
        broken['Trade'] = 'false'
        with self.assertRaises(ValueError): self.store.ingest(broken,self.meta)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM ppi_events').fetchone()[0],0)
    def test_old_quote_cannot_replace_new_quote(self):
        self.store.ingest(message(False),self.meta)
        old = message(False)
        old['Date'] = '2026-10-19T15:08:10.000-03:00'
        old['Bids'][0]['Price'] = 100
        self.store.ingest(old,self.meta)
        latest = json.loads(self.store.db.execute('SELECT payload FROM ppi_latest').fetchone()[0])
        self.assertEqual(latest['bid'],407.5)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM ppi_events').fetchone()[0],2)
    def test_trade_older_than_book_still_updates_trade_fields(self):
        self.store.ingest(message(False),self.meta)
        trade = message(True)
        trade['Date'] = '2026-10-19T15:08:14.000-03:00'
        self.store.ingest(trade,self.meta)
        latest = json.loads(self.store.db.execute('SELECT payload FROM ppi_latest').fetchone()[0])
        self.assertEqual(latest['last'],407.5)
        self.assertEqual(latest['bid'],407.5)

if __name__ == '__main__': unittest.main()
