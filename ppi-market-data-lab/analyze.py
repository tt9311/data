"""Informe offline de calidad del feed y del proceso de captura."""
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

def percentiles(values):
    values = sorted(values)
    if not values: return {'count':0,'p50':None,'p95':None,'p99':None,'max':None}
    def percentile(p):
        index = (len(values)-1)*p
        lower = int(index)
        upper = min(lower+1,len(values)-1)
        return values[lower]+(values[upper]-values[lower])*(index-lower)
    return {'count':len(values),'p50':percentile(.5),'p95':percentile(.95),
            'p99':percentile(.99),'max':values[-1]}

def report(filename, sample_limit=100000):
    db = sqlite3.connect(f'file:{Path(filename).resolve().as_posix()}?mode=ro',uri=True)
    try:
        counts = dict(db.execute('SELECT event_kind,COUNT(*) FROM ppi_events GROUP BY event_kind'))
        rows = db.execute('SELECT session_id,received_monotonic_ns,queue_delay_ns,apparent_timestamp_lag_ms,message_sha256,out_of_order '
                          'FROM event_timing ORDER BY event_id DESC LIMIT ?', (sample_limit,)).fetchall()[::-1]
        previous, intervals = {}, []
        for session, mono, *_ in rows:
            if session in previous and mono >= previous[session]: intervals.append((mono-previous[session])/1e6)
            previous[session] = mono
        duplicates = Counter((row[0],row[4]) for row in rows)
        connections = dict(db.execute('SELECT state,COUNT(*) FROM connection_events GROUP BY state'))
        return {
            'events':sum(counts.values()),'event_kinds':counts,
            'instruments_with_events':db.execute('SELECT COUNT(DISTINCT symbol) FROM ppi_events').fetchone()[0],
            'catalogue_options':db.execute('SELECT COUNT(*) FROM ppi_instruments').fetchone()[0],
            'timing_sample_size':len(rows),'timing_sample_limit':sample_limit,
            'queue_delay_ms':percentiles([row[2]/1e6 for row in rows]),
            'feed_interarrival_ms_within_session':percentiles(intervals),
            'apparent_provider_timestamp_lag_ms':percentiles([row[3] for row in rows if row[3] is not None]),
            'negative_apparent_lag_events':sum(row[3] is not None and row[3]<0 for row in rows),
            'repeated_payloads_in_timing_sample':sum(count-1 for count in duplicates.values()),
            'out_of_order_in_timing_sample':sum(row[5] for row in rows),
            'connection_events':connections,
            'limitations':[
                'La diferencia UTC/proveedor no mide latencia de red: incluye semántica del timestamp, feed y desajuste de relojes.',
                'Sin secuencia del mercado no se puede demostrar que no faltan actualizaciones.',
                'Las repeticiones se conservan: no hay identificador del mercado para deduplicar operaciones con certeza.',
                'PPI no proporciona en estos mensajes prioridad en cola, órdenes individuales ni timestamps de ejecución propia.'
            ]}
    finally: db.close()

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default='data/options.sqlite')
    parser.add_argument('--output')
    args=parser.parse_args()
    result=json.dumps(report(args.db),ensure_ascii=False,indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True,exist_ok=True)
        Path(args.output).write_text(result,encoding='utf-8')
    else: print(result)
