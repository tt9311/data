"""Persistencia de mensajes PPI. Sin claves ni tokens en la base."""
import json
import re
import sqlite3
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

def metadata(instrument, underlying='GGAL'):
    description = instrument.get('description', '')
    match = re.search(r'AR\$\s*([\d.]+)\s+Vto\.\s*(\d{2})/(\d{2})/(\d{4})', description)
    if not match or not re.search(r'\b' + re.escape(underlying) + r'\b', description):
        raise ValueError('No se puede identificar strike/vencimiento/subyacente del instrumento')
    if 'compra' in description.lower():
        kind = 'CALL'
    elif 'venta' in description.lower():
        kind = 'PUT'
    else:
        raise ValueError('Tipo de opción desconocido')
    strike, day, month, year = match.groups()
    expiry = datetime(int(year), int(month), int(day)).date().isoformat()
    return {'symbol': instrument['ticker'], 'underlying': underlying, 'type': kind,
            'strike': float(strike), 'expiry': expiry, 'description': description,
            'currency': instrument.get('currency'), 'market': instrument.get('market'),
            'source': 'ppi_websocket'}

class Store:
    def __init__(self, filename):
        if str(filename) != ':memory:':
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(filename, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))

    def session(self, session_id, config):
        with self.db:
            self.db.execute('INSERT INTO capture_sessions VALUES (?,?,NULL,?,?)',
                            (session_id, now(), 'running', json.dumps(config)))

    def end_session(self, session_id, status='stopped'):
        with self.db:
            self.db.execute('UPDATE capture_sessions SET ended_at=?,status=? WHERE session_id=?',
                            (now(), status, session_id))

    def connection(self, session_id, state, detail=None):
        with self.db:
            self.db.execute('INSERT INTO connection_events(session_id,observed_at,monotonic_ns,state,detail) VALUES (?,?,?,?,?)',
                            (session_id, now(), time.perf_counter_ns(), state, detail))

    def catalogue(self, instruments, underlying):
        result = {}
        with self.db:
            for instrument in instruments:
                item = metadata(instrument, underlying)
                result[item['symbol']] = item
                self.db.execute('INSERT OR REPLACE INTO ppi_instruments VALUES (?,?,?,?,?)',
                    (item['symbol'], underlying, item['expiry'], item['type'], json.dumps(item)))
        return result

    def status(self, **status):
        status['heartbeat_at'] = now()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO ppi_collector_state VALUES (1,?)', (json.dumps(status),))

    def ingest(self, message, meta, observed_at=None, timing=None):
        dequeued_ns = timing.get('dequeued_monotonic_ns', time.perf_counter_ns()) if timing else time.perf_counter_ns()
        if message.get('Ticker') != meta['symbol'] or not message.get('Settlement'):
            raise ValueError('El símbolo o plazo no coincide')
        if type(message.get('Trade')) is not bool:
            raise ValueError('Tipo de mensaje PPI desconocido')
        if not message.get('Date'):
            raise ValueError('Falta el timestamp PPI')
        observed_at = observed_at or now()
        symbol, settlement = message['Ticker'], message['Settlement']
        previous = self.db.execute('SELECT payload FROM ppi_latest WHERE symbol=? AND settlement=?',
                                   (symbol, settlement)).fetchone()
        state = json.loads(previous[0]) if previous else {
            **meta, 'settlement': settlement, 'bid': None, 'ask': None,
            'bid_size': None, 'ask_size': None, 'last': None, 'change_percent': None,
            'book': None, 'volume_nominal': None, 'last_trade_observed_at': None,
            'book_observed_at': None, 'book_exchange_timestamp': None,
            'last_trade_exchange_timestamp': None,
        }
        # PPI envía Trade=false con Price=0 y Trade=true con Bids/Offers vacíos.
        # Cada evento actualiza exclusivamente los campos de su propio tipo.
        if message['Trade']:
            state['last'] = message.get('Price')
            state['change_percent'] = message['VarDay'] * 100 if message.get('VarDay') is not None else None
            state['last_trade_quantity'] = message.get('VolumeAmount')
            state['last_trade_observed_at'] = observed_at
            state['last_trade_exchange_timestamp'] = message['Date']
            kind = 'trade'
        else:
            bids, offers = message.get('Bids'), message.get('Offers')
            if not isinstance(bids, list) or not isinstance(offers, list):
                raise ValueError('Libro PPI inválido')
            bids = sorted(bids, key=lambda item: item['Position'])
            offers = sorted(offers, key=lambda item: item['Position'])
            state.update(bid=bids[0]['Price'] if bids else None,
                         bid_size=bids[0]['Quantity'] if bids else None,
                         ask=offers[0]['Price'] if offers else None,
                         ask_size=offers[0]['Quantity'] if offers else None,
                         book={'bids': bids, 'offers': offers}, book_observed_at=observed_at,
                         book_exchange_timestamp=message['Date'])
            kind = 'book'
        state.update(observed_at=observed_at, exchange_timestamp=message['Date'], event_kind=kind,
                     opening_price=message.get('OpeningPrice'), high=message.get('MaxDay'),
                     low=message.get('MinDay'), volume_traded_currency=message.get('VolumeTotalAmount'))
        # Conserva mensajes fuera de orden en el histórico sin retroceder el último estado.
        old_exchange = None
        if previous:
            field = 'last_trade_exchange_timestamp' if message['Trade'] else 'book_exchange_timestamp'
            old_exchange = json.loads(previous[0]).get(field)
        out_of_order = bool(old_exchange and datetime.fromisoformat(message['Date']) < datetime.fromisoformat(old_exchange))
        if out_of_order:
            state['out_of_order'] = True
        payload = json.dumps(state, ensure_ascii=False)
        args = (symbol, meta['underlying'], meta.get('expiry') or '', meta['type'], settlement, observed_at)
        with self.db:
            cursor = self.db.execute('INSERT INTO ppi_events(symbol,underlying,expiry,type,settlement,observed_at,event_kind,payload,raw_message) '
                            'VALUES (?,?,?,?,?,?,?,?,?)', args + (kind, payload, json.dumps(message, ensure_ascii=False)))
            if timing:
                provider = datetime.fromisoformat(message['Date'])
                if provider.tzinfo is None:
                    apparent_lag = None
                else:
                    apparent_lag = timing['received_utc_ns']/1_000_000 - provider.timestamp()*1000
                canonical = json.dumps(message, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
                digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
                self.db.execute('INSERT INTO event_timing VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (cursor.lastrowid, timing['session_id'], timing['received_utc_ns'],
                     timing['received_monotonic_ns'], dequeued_ns,
                     max(0, dequeued_ns-timing['received_monotonic_ns']), message['Date'],
                     apparent_lag, digest, int(out_of_order)))
            if not out_of_order:
                self.db.execute('INSERT OR REPLACE INTO ppi_latest VALUES (?,?,?,?,?,?,?)', args + (payload,))
        return state

    def close(self):
        self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')
        self.db.close()
