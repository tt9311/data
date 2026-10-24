"""Replay por llegada y markouts hipotéticos de cotizaciones; no modela fills."""
import argparse
import bisect
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

def events(filename, symbol=None):
    db=sqlite3.connect(f'file:{Path(filename).resolve().as_posix()}?mode=ro',uri=True)
    try:
        where='WHERE e.symbol=?' if symbol else ''
        sql=('SELECT e.event_id,e.payload,e.raw_message,t.session_id,t.received_monotonic_ns '
             'FROM ppi_events e JOIN event_timing t ON t.event_id=e.event_id '+where+' ORDER BY e.event_id')
        for event_id,payload,raw,session,mono in db.execute(sql,(symbol,) if symbol else ()):
            yield {'event_id':event_id,'state':json.loads(payload),'raw':json.loads(raw),
                   'session_id':session,'received_monotonic_ns':mono}
    finally: db.close()

def markouts(filename, symbol=None, latency_ms=50, horizon_ms=1000, threshold=.6, fee_bps=0, max_gap_ms=1000):
    if min(latency_ms,horizon_ms,fee_bps,max_gap_ms)<0 or not 0<=threshold<=1:
        raise ValueError('Parámetros fuera de rango')
    groups=defaultdict(list)
    for event in events(filename,symbol):
        s=event['state']
        if s['event_kind']!='book' or s.get('out_of_order') or s['type']=='UND': continue
        if s.get('bid') is None or s.get('ask') is None or not 0<s['bid']<=s['ask']: continue
        if s.get('bid_size') is None or s.get('ask_size') is None: continue
        if (s.get('bid_size') or 0)+(s.get('ask_size') or 0)<=0: continue
        groups[(event['session_id'],s['symbol'],s['settlement'])].append(event)
    samples,skipped=0,0
    scores=[]
    for group in groups.values():
        times=[row['received_monotonic_ns'] for row in group]
        for index,event in enumerate(group):
            state=event['state']
            imbalance=(state['bid_size']-state['ask_size'])/(state['bid_size']+state['ask_size'])
            if abs(imbalance)<threshold: continue
            samples+=1
            target=times[index]+round(latency_ms*1e6)
            entry_index=bisect.bisect_left(times,target,lo=index)
            if entry_index>=len(times) or times[entry_index]-target>max_gap_ms*1e6:
                skipped+=1; continue
            exit_target=times[entry_index]+round(horizon_ms*1e6)
            exit_index=bisect.bisect_left(times,exit_target,lo=entry_index)
            if exit_index>=len(times) or times[exit_index]-exit_target>max_gap_ms*1e6:
                skipped+=1; continue
            entry,exit=group[entry_index]['state'],group[exit_index]['state']
            if imbalance>=0:
                score=(exit['bid']-entry['ask'])/entry['ask']*10000
            else:
                score=(entry['bid']-exit['ask'])/entry['bid']*10000
            scores.append(score-2*fee_bps)
    return {'mode':'hypothetical_quote_crossing_markout','latency_ms':latency_ms,'horizon_ms':horizon_ms,
            'imbalance_threshold':threshold,'fee_bps_per_side':fee_bps,'max_gap_ms':max_gap_ms,
            'candidate_signals':samples,'scored_signals':len(scores),'skipped_without_timely_quotes':skipped,
            'mean_quote_markout_bps':sum(scores)/len(scores) if scores else None,
            'positive_markout_fraction':sum(value>0 for value in scores)/len(scores) if scores else None,
            'limitations':['No simula fills, prioridad, rechazos, impacto, capital ni latencia de ejecución.',
                           'Señales superpuestas: los resultados no representan PnL de una cartera.',
                           'La latencia se aplica sobre llegada al recolector; no estima tiempo de ida al mercado.']}

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default='data/options.sqlite')
    parser.add_argument('--symbol')
    parser.add_argument('--latencies-ms',default='0,10,50,100,500')
    parser.add_argument('--horizon-ms',type=float,default=1000)
    parser.add_argument('--threshold',type=float,default=.6)
    parser.add_argument('--fee-bps',type=float,default=0)
    parser.add_argument('--output')
    args=parser.parse_args()
    result=[markouts(args.db,args.symbol,float(value),args.horizon_ms,args.threshold,args.fee_bps)
            for value in args.latencies_ms.split(',')]
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True,exist_ok=True)
        Path(args.output).write_text(text,encoding='utf-8')
    else: print(text)
