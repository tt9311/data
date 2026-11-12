"""Recolector continuo de opciones de GGAL mediante el SDK oficial de PPI."""
import argparse
import json
import logging
import os
import queue
import signal
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from ppi_access import make_client, ROOT
from store import Store, now

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials-stdin', action='store_true')
    parser.add_argument('--seconds', type=int, default=0, help='0: continuar hasta interrupción')
    parser.add_argument('--settlement', action='append', help='Por defecto A-24HS; se puede repetir')
    parser.add_argument('--underlying', default='GGAL')
    parser.add_argument('--prefix', default='GFG')
    parser.add_argument('--expiry', help='Filtrar vencimiento YYYY-MM-DD')
    parser.add_argument('--symbol', action='append', help='Suscribir solo estos símbolos y el subyacente')
    parser.add_argument('--require-events', action='store_true', help='Fallar si la captura termina sin datos')
    args = parser.parse_args()
    if args.seconds < 0:
        parser.error('--seconds no puede ser negativo')
    credentials = json.load(sys.stdin) if args.credentials_stdin else {
        'public_key': os.environ.get('PPI_PUBLIC_KEY'), 'private_key': os.environ.get('PPI_PRIVATE_KEY')}
    if not all(credentials.get(key) for key in ['public_key','private_key']):
        print('Faltan PPI_PUBLIC_KEY y PPI_PRIVATE_KEY', flush=True)
        return 1
    from ppi_client.models.instrument import Instrument
    store = Store(Path(os.environ.get('OPTIONS_DATA_DIR', str(ROOT / 'data'))) / 'options.sqlite')
    settlements = args.settlement or ['A-24HS']
    end = time.monotonic() + args.seconds if args.seconds else float('inf')
    stop = False
    def request_stop(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    received, connected, subscriptions = 0, False, 0
    dropped, reported_dropped = 0, 0
    session_id = str(uuid.uuid4())
    store.session(session_id, {'underlying':args.underlying,'prefix':args.prefix,
                              'expiry':args.expiry,'symbols':args.symbol,'settlements':settlements})
    connection = None
    phase, last_error = 'starting', None
    def status():
        store.status(phase=phase, connected=connected, received_messages=received,
                     subscriptions=subscriptions, settlements=settlements, last_error_type=last_error,
                     session_id=session_id, dropped_received_messages=dropped,
                     host_clock_synchronized='not_verified', queue_capacity=50000)
    try:
        while not stop and time.monotonic() < end:
            messages = queue.Queue(maxsize=50000)
            def enqueue(event, value=None):
                nonlocal dropped
                try: messages.put_nowait((event,value))
                except queue.Full: dropped += 1
            def on_data(data):
                # Captura ambos relojes al comienzo del callback, antes de parsear/escribir.
                utc_ns, mono_ns = time.time_ns(), time.perf_counter_ns()
                enqueue('data', (utc_ns,mono_ns,data))
            try:
                client = make_client(credentials['public_key'], credentials['private_key'])
                catalogue = client.marketdata.search_instrument(args.prefix, '', 'BYMA', 'OPCIONES')
                metas = store.catalogue(catalogue, args.underlying)
                if args.expiry: metas = {key:value for key,value in metas.items() if value['expiry']==args.expiry}
                if args.symbol: metas = {key:value for key,value in metas.items() if key in args.symbol}
                if not metas: raise ValueError('La selección no contiene opciones')
                metas[args.underlying] = {'symbol':args.underlying,'underlying':args.underlying,'expiry':None,
                                 'type':'UND','strike':None,'source':'ppi_websocket'}
                available = client.configuration.get_settlements()
                if any(value not in available for value in settlements):
                    raise ValueError('Plazo de liquidación inválido')
                phase, last_error = 'connecting', None
                status()
                store.connection(session_id,'connecting')
                client.realtime.connect_to_market_data(
                    lambda: enqueue('open'), lambda *_: enqueue('closed'), on_data)
                logging.disable(logging.CRITICAL)
                # SDK 1.3.0: se corrige la firma del callback de cierre para SignalR.
                connection = client._PPI__apiClient.ws_connection_marketdata
                connection.on_close(lambda *_: enqueue('closed'))
                connection.on_error(lambda _: enqueue('subscription_error'))
                websocket = connection.transport._ws
                original_error = websocket.on_error
                def transport_error(app, error):
                    enqueue('transport_error', type(error).__name__)
                    if original_error: original_error(app,error)
                websocket.on_error = transport_error
                disconnected_since = time.monotonic()
                heartbeat = 0
                while not stop and time.monotonic() < end:
                    try:
                        event, value = messages.get(timeout=1)
                        dequeued_ns = time.perf_counter_ns()
                    except queue.Empty:
                        event, value = None, None
                    if event == 'open':
                        connected, phase, subscriptions = True, 'streaming', 0
                        store.connection(session_id,'connected')
                        for meta in metas.values():
                            if stop or time.monotonic() >= end:
                                break
                            for settlement in settlements:
                                client.realtime.subscribe_to_element(Instrument(
                                    meta['symbol'], 'ACCIONES' if meta['type']=='UND' else 'OPCIONES', settlement))
                                subscriptions += 1
                                time.sleep(0.02)
                        print(json.dumps({'connected':True,'catalogue_options':len(catalogue),
                                          'subscriptions':subscriptions,'settlements':settlements}), flush=True)
                    elif event == 'data':
                        utc_ns, mono_ns, data = value
                        observed_at = datetime.fromtimestamp(utc_ns/1_000_000_000,timezone.utc).isoformat(timespec='microseconds')
                        msg = json.loads(data) if isinstance(data, str) else data
                        meta = metas.get(msg.get('Ticker'))
                        if meta and msg.get('Settlement') in settlements:
                            store.ingest(msg, meta, observed_at, timing={'session_id':session_id,
                                'received_utc_ns':utc_ns,'received_monotonic_ns':mono_ns,
                                'dequeued_monotonic_ns':dequeued_ns})
                            received += 1
                    elif event == 'closed':
                        connected, phase = False, 'reconnecting'
                        disconnected_since = time.monotonic()
                        store.connection(session_id,'disconnected')
                        print(json.dumps({'connected':False,'reconnecting':True}), flush=True)
                    elif event == 'subscription_error':
                        last_error = 'SubscriptionError'
                        store.connection(session_id,'subscription_error')
                    elif event == 'transport_error':
                        last_error = value
                        store.connection(session_id,'transport_error',value)
                        print(json.dumps({'transport_error':value}), flush=True)
                    if dropped != reported_dropped:
                        store.connection(session_id,'queue_overflow',json.dumps({'dropped':dropped}))
                        reported_dropped = dropped
                    if time.monotonic() - heartbeat >= 5:
                        status()
                        heartbeat = time.monotonic()
                    if not connected and time.monotonic() - disconnected_since > 45:
                        break
                if connection:
                    connection.stop()
                    connection = None
            except Exception as error:
                # No imprime excepciones completas: algunos transportes incluyen tokens en URLs.
                last_error = type(error).__name__
                connected, phase = False, 'retrying'
                status()
                store.connection(session_id,'retrying',last_error)
                print(json.dumps({'retrying':True,'error_type':last_error,
                                  'http_status':getattr(error,'http_status',None)}), flush=True)
                if connection:
                    try: connection.stop()
                    except Exception: pass
                    connection = None
            if not stop and time.monotonic() < end:
                for _ in range(15):
                    if stop or time.monotonic() >= end: break
                    time.sleep(1)
    finally:
        if connection:
            try: connection.stop()
            except Exception: pass
        phase, connected = 'stopped', False
        status()
        store.connection(session_id,'stopped')
        store.end_session(session_id,'completed' if args.seconds and received else 'stopped')
        store.close()
    print(json.dumps({'stopped':True,'received_messages':received}), flush=True)
    return 2 if args.require_events and not received else 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'error_type':type(error).__name__}), flush=True)
        raise SystemExit(1)
