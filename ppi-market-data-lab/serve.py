"""API de lectura para el laboratorio; no conecta al broker ni ejecuta órdenes."""
import argparse
import hmac
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from store import Store

def result(db, path, params):
    if path=='/health':
        row=db.execute('SELECT payload FROM ppi_collector_state WHERE id=1').fetchone()
        collector=json.loads(row[0]) if row else None
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(collector['heartbeat_at'].replace('Z','+00:00'))).total_seconds() if collector else None
        return {'collector':collector,'collector_alive':age is not None and 0<=age<20,
                'stored_events':db.execute('SELECT COUNT(*) FROM ppi_events').fetchone()[0]}
    if path not in ['/quotes','/events','/instruments']: return None
    limit=int(params.get('limit','1000'))
    if not 1<=limit<=10000: raise ValueError('limit debe estar entre 1 y 10000')
    values,conditions=[],[]
    for key in ['symbol','underlying','expiry']+([] if path=='/instruments' else ['settlement']):
        if params.get(key): conditions.append(key+'=?');values.append(params[key])
    if path=='/events':
        after=int(params.get('after','0'))
        if after<0: raise ValueError('after debe ser positivo')
        conditions.append('event_id>?');values.append(after)
    table={'/quotes':'ppi_latest','/events':'ppi_events','/instruments':'ppi_instruments'}[path]
    where=' WHERE '+' AND '.join(conditions) if conditions else ''
    columns='event_id,payload' if path=='/events' else 'payload'
    order='event_id' if path=='/events' else 'symbol'
    rows=db.execute(f'SELECT {columns} FROM {table}{where} ORDER BY {order} LIMIT ?',(*values,limit)).fetchall()
    data=[{'event_id':row[0],'state':json.loads(row[1])} for row in rows] if path=='/events' else [json.loads(row[0]) for row in rows]
    return {'count':len(data),'data':data}

def make_server(filename, host='127.0.0.1', port=8766, token=''):
    if host not in ['127.0.0.1','localhost','::1'] and not token:
        raise ValueError('Configurar API_TOKEN antes de escuchar fuera de localhost')
    Store(filename).close()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_): pass
        def do_GET(self):
            supplied=self.headers.get('Authorization','')
            if token and not hmac.compare_digest(supplied,'Bearer '+token):
                self.send_json(401,{'error':'No autorizado'}); return
            request=urlparse(self.path)
            params={key:values[-1] for key,values in parse_qs(request.query).items()}
            try:
                if request.path=='/metrics':
                    from analyze import report
                    data=report(filename)
                else:
                    with closing(sqlite3.connect(filename,timeout=5)) as db:
                        data=result(db,request.path,params)
                self.send_json(404 if data is None else 200,data or {'error':'Ruta inexistente'})
            except (ValueError,sqlite3.Error):
                self.send_json(400,{'error':'Consulta inválida'})
        def do_POST(self): self.send_json(405,{'error':'Solo lectura'})
        def send_json(self,status,data):
            payload=json.dumps(data,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(payload)
    return ThreadingHTTPServer((host,port),Handler)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default=str(Path(os.environ.get('OPTIONS_DATA_DIR','data'))/'options.sqlite'))
    parser.add_argument('--host',default=os.environ.get('HOST','127.0.0.1'))
    parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','8766')))
    args=parser.parse_args()
    server=make_server(args.db,args.host,args.port,os.environ.get('API_TOKEN',''))
    print(f'API de lectura en {args.host}:{args.port}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
