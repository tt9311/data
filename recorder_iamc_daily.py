#!/usr/bin/env python3
"""
recorder_iamc_daily.py
Descarga el informe oficial diario del IAMC al cierre de la rueda,
extrae toda la cadena de opciones (Calls/Puts, Strikes, Griegas, VI, OI)
y actualiza el histórico consolidado en data/daily/
"""

import os
import re
import ssl
import sys
import glob
from datetime import datetime, timezone, timedelta
import urllib.request
import pandas as pd
from pypdf import PdfReader
from build_options_datasets import parse_pdf_file

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def get_argentina_time():
    art_tz = timezone(timedelta(hours=-3))
    return datetime.now(art_tz)

def download_missing_iamc(lookback_days=7):
    now_art = get_argentina_time()
    out_daily_dir = os.path.join("data", "daily")
    out_pdf_dir = os.path.join(out_daily_dir, "pdf")
    os.makedirs(out_pdf_dir, exist_ok=True)

    downloaded = []
    # Check from oldest to newest within lookback window
    for offset in reversed(range(lookback_days + 1)):
        dt = now_art - timedelta(days=offset)
        if dt.weekday() >= 5:  # Skip Saturday/Sunday
            continue
        d_ddmmyyyy = dt.strftime("%d%m%Y")
        d_iso = dt.strftime("%Y-%m-%d")

        daily_csv = os.path.join(out_daily_dir, f"options_iamc_{d_iso}.csv")
        if os.path.exists(daily_csv) and os.path.getsize(daily_csv) > 1000:
            continue

        # Check if already in local informes_pdf/
        for cand_dir in ("informes_pdf", os.path.join("archive_research", "informes_pdf")):
            local_archive_pdf = os.path.join(cand_dir, f"InformeDiarioOpciones{d_ddmmyyyy}.pdf")
            if os.path.exists(local_archive_pdf) and os.path.getsize(local_archive_pdf) > 100000:
                downloaded.append((local_archive_pdf, d_ddmmyyyy, False))
                break
        else:
            local_archive_pdf = None
        if local_archive_pdf and os.path.exists(local_archive_pdf):
            continue

        name = f"InformeDiarioOpciones{d_ddmmyyyy}"
        url = f"https://www.iamc.com.ar/Informe/{name}/"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=12) as resp:
                if resp.status == 200:
                    html = resp.read().decode("utf-8", errors="ignore")
                    m = re.search(r'href=[\"\'](https?://[^\s\"\'>]+\.pdf)[\"\']', html, re.I)
                    if m:
                        pdf_url = m.group(1)
                        pdf_path = os.path.join(out_pdf_dir, f"InformeDiarioOpciones_{d_ddmmyyyy}.pdf")
                        print(f"Descargando informe {name} desde {pdf_url}...")
                        pdf_req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(pdf_req, context=ctx, timeout=35) as p_resp:
                            with open(pdf_path, "wb") as f_out:
                                f_out.write(p_resp.read())
                        print(f"Descargado con éxito: {pdf_path}")
                        downloaded.append((pdf_path, d_ddmmyyyy, True))
        except Exception:
            pass

    if not downloaded:
        print("No se encontraron nuevos informes diarios pendientes en IAMC.")
    return downloaded


def main():
    keep_pdf = "--keep-pdf" in sys.argv
    pending = download_missing_iamc(lookback_days=7)
    if not pending:
        return

    out_daily_dir = os.path.join("data", "daily")
    os.makedirs(out_daily_dir, exist_ok=True)
    master_file = os.path.join(out_daily_dir, "options_iamc_master_history.csv")
    df_master = pd.read_csv(master_file) if os.path.exists(master_file) else pd.DataFrame()

    latest_df = None
    latest_date = ""

    for pdf_path, date_str, is_temp in pending:
        print(f"Procesando {pdf_path}...")
        records = parse_pdf_file(pdf_path)
        if not records:
            print(f"No se extrajeron registros válidos de {pdf_path}.")
            if is_temp and not keep_pdf and os.path.exists(pdf_path):
                os.remove(pdf_path)
            continue

        df = pd.DataFrame(records)
        actual_date = df["fecha_reporte"].iloc[0]

        daily_csv = os.path.join(out_daily_dir, f"options_iamc_{actual_date}.csv")
        df.to_csv(daily_csv, index=False, encoding="utf-8-sig")
        print(f"Guardado {daily_csv} con {len(df)} contratos.")

        if not df_master.empty:
            df_master = df_master[df_master["fecha_reporte"] != actual_date]
            df_master = pd.concat([df_master, df], ignore_index=True)
        else:
            df_master = df

        if actual_date >= latest_date:
            latest_date = actual_date
            latest_df = df

        if is_temp and not keep_pdf and os.path.exists(pdf_path):
            os.remove(pdf_path)

    if not df_master.empty:
        df_master.sort_values(by=["fecha_reporte", "subyacente", "strike"], inplace=True)
        df_master.to_csv(master_file, index=False, encoding="utf-8-sig")
        print(f"Actualizado master history en {master_file} (Total acumulado: {len(df_master)} filas).")

    if latest_df is not None:
        latest_json = os.path.join(out_daily_dir, "options_iamc_latest.json")
        latest_df.to_json(latest_json, orient="records", indent=2, force_ascii=False)
        print(f"Actualizado snapshot JSON más reciente en {latest_json}.")


if __name__ == "__main__":
    main()
