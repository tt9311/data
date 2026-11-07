"""Autenticación PPI y adaptación de transporte. No implementa órdenes."""
import json
import logging
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.python-packages'))
if os.environ.get('PPI_PACKAGE_DIR'):
    sys.path.insert(0, os.environ['PPI_PACKAGE_DIR'])
logging.disable(logging.CRITICAL)

def make_client(public_key, private_key):
    import requests
    from ppi_client.ppi import PPI
    original = requests.sessions.Session.request
    if not getattr(original, '_ppi_timeout', False):
        def bounded(self, method, url, **kwargs):
            kwargs.setdefault('timeout', 25)
            response = original(self, method, url, **kwargs)
            if 'portfoliopersonal.com/' in url and response.content and 'json' not in response.headers.get('Content-Type','').lower():
                error = RuntimeError('PPI devolvió una respuesta fuera del protocolo JSON')
                error.http_status = response.status_code
                raise error
            return response
        bounded._ppi_timeout = True
        requests.sessions.Session.request = bounded
    client = PPI(sandbox=False)
    # El endpoint de PPI acepta WebSocket directo autenticado. En esta prueba,
    # la negociación HTTP seguida de upgrade devolvió 404; el transporte directo
    # funcionó también con el cliente oficial de Microsoft SignalR.
    rest_client = client._PPI__apiClient.get_rest_client()
    build_connection = rest_client.connect_to_websocket
    async def direct_websocket(hub_name):
        connection = await build_connection(hub_name)
        connection.transport.skip_negotiation = True
        return connection
    rest_client.connect_to_websocket = direct_websocket
    client.account.login_api(public_key, private_key)
    return client

