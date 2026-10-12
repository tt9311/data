#!/usr/bin/env python3
"""
recorder_byma_intraday.py
Captura un snapshot intradiario de todas las opciones en BYMA y lo appendea
al archivo CSV diario en data/intraday/options_intraday_YYYY-MM-DD.csv
"""

import os
import sys
import json
import random
from datetime import datetime, timezone, timedelta
import requests
import urllib3
import pandas as pd

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_API = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"
BASE_URL = f"{BASE_API}/options"
EQUITY_URL = f"{BASE_API}/leading-equity"

def get_argentina_time():
    art_tz = timezone(timedelta(hours=-3))
    return datetime.now(art_tz)

def _build_headers():
    key_access = "dc826d4c2dde7519e882a250359a23a" + str(random.randint(0, 9))
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache,no-store,max-age=1,must-revalidate",
        "Expires": "1",
        "Token": key_access,
        "Options": "derivados"
    }

def fetch_byma_options():
    headers = _build_headers()
    for payload in ({}, {"excludeZeroPxAndQty": False, "T1": True, "T0": True, "page_number": 1, "page_size": 2000}):
        resp = requests.post(BASE_URL, headers=headers, json=payload, verify=False, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if items:
            return items
    return []

def fetch_byma_underlyings():
    """Obtiene precios spot y puntas simultáneas de los subyacentes en 24hs (T1)."""
    spots = {}
    try:
        headers = _build_headers()
        resp = requests.post(EQUITY_URL, headers=headers, json={"T1": True, "page_size": 100}, verify=False, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for it in items:
            sym = it.get("symbol")
            if sym:
                spots[sym] = {
                    "underlying_spot": it.get("trade") or it.get("closingPrice") or it.get("previousClosingPrice"),
                    "underlying_bid": it.get("bidPrice"),
                    "underlying_offer": it.get("offerPrice"),
                    "underlying_vwap": it.get("vwap"),
                }
    except Exception as e:
        print(f"Aviso: no se pudo consultar leading-equity para enriquecer subyacentes: {e}")
    return spots

def main():
    now_art = get_argentina_time()
    date_str = now_art.strftime("%Y-%m-%d")
    timestamp_iso = now_art.isoformat()
    
    print(f"[{timestamp_iso}] Consultando snapshot de opciones en BYMA...")
    try:
        items = fetch_byma_options()
    except Exception as e:
        print(f"Error al consultar BYMA: {e}")
        sys.exit(1)
        
    if not items:
        print("El panel de opciones devolvió 0 items (mercado cerrado o sin cotizaciones activas).")
        return
        
    print(f"Obtenidos {len(items)} contratos de opciones.")
    spots = fetch_byma_underlyings()

    df = pd.DataFrame(items)
    df["snapshot_time_art"] = timestamp_iso
    df["snapshot_date"] = date_str

    if "underlyingSymbol" in df.columns and spots:
        df["underlying_spot"] = df["underlyingSymbol"].map(lambda s: spots.get(s, {}).get("underlying_spot"))
        df["underlying_bid"] = df["underlyingSymbol"].map(lambda s: spots.get(s, {}).get("underlying_bid"))
        df["underlying_offer"] = df["underlyingSymbol"].map(lambda s: spots.get(s, {}).get("underlying_offer"))
        df["underlying_vwap"] = df["underlyingSymbol"].map(lambda s: spots.get(s, {}).get("underlying_vwap"))
    
    # Columnas de interés ordenadas
    cols_order = [
        "snapshot_time_art", "symbol", "underlyingSymbol", "optionType",
        "underlying_spot", "underlying_bid", "underlying_offer", "underlying_vwap",
        "trade", "bidPrice", "offerPrice", "quantityBid", "quantityOffer",
        "tradeVolume", "volumeAmount", "vwap", "openInterest", "imbalance",
        "maturityDate", "daysToMaturity", "tradeHour", "closingPrice",
        "openingPrice", "tradingHighPrice", "tradingLowPrice", "settlementPrice"
    ]
    present_cols = [c for c in cols_order if c in df.columns] + [c for c in df.columns if c not in cols_order]
    df = df[present_cols]
    
    out_dir = os.path.join("data", "intraday")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"options_intraday_{date_str}.csv")
    
    # Append al CSV del día (o crea si no existe)
    file_exists = os.path.exists(out_file)
    df.to_csv(out_file, mode="a", header=not file_exists, index=False, encoding="utf-8")
    print(f"Snapshot guardado exitosamente en {out_file} (Total filas agregadas: {len(df)})")

    latest_json = os.path.join(out_dir, "options_intraday_latest.json")
    df.to_json(latest_json, orient="records", indent=2, force_ascii=False)
    print(f"Snapshot más reciente exportado a {latest_json}")

if __name__ == "__main__":
    main()
