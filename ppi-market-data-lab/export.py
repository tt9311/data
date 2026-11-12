"""Exporta mensajes de mercado y timestamps a JSONL; no exporta secretos."""
import argparse
import json
from pathlib import Path
from replay import events

def export(filename, output, symbol=None):
    output=Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    count=0
    with output.open('w',encoding='utf-8') as stream:
        for event in events(filename,symbol):
            stream.write(json.dumps(event,ensure_ascii=False)+'\n')
            count+=1
    return count

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default='data/options.sqlite')
    parser.add_argument('--output',default='data/events.jsonl')
    parser.add_argument('--symbol')
    args=parser.parse_args()
    print(json.dumps({'exported_events':export(args.db,args.output,args.symbol),'file':args.output}))
