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
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
import urllib.request
import pandas as pd
from build_options_datasets import parse_pdf_file

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def get_argentina_time():
    art_tz = timezone(timedelta(hours=-3))
    return datetime.now(art_tz)

INDEX_URL = 'https://www.iamc.com.ar/informediario/'


def discover_reports():
    request = urllib.request.Request(INDEX_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, context=ctx, timeout=25) as response:
        html = response.read().decode('utf-8', errors='replace')
    dates = set()
    for value in re.findall(r'href=["\'][^"\']*/Informe/InformeDiarioOpciones(\d{8})/?["\']', html, re.I):
        try:
            date = datetime.strptime(value, '%d%m%Y').date()
        except ValueError:
            continue
        if date <= get_argentina_time().date():
            dates.add(date)
    if not dates:
        raise RuntimeError('IAMC: official index contains no recognized options reports; check source/index parser')
    return sorted(dates)


def check_freshness(source_latest):
    master = Path('data/daily/options_iamc_master_history.csv')
    latest = None
    if master.exists():
        history = pd.read_csv(master, usecols=['fecha_reporte'])
        value = pd.to_datetime(history['fecha_reporte']).max()
        if not pd.isna(value):
            latest = value.date()
    age = (get_argentina_time().date() - latest).days if latest else None
    status = 'local_lagging' if latest is None or latest < source_latest else (
        'source_stale' if age > 7 else 'current')
    health = {'checked_at': get_argentina_time().isoformat(), 'status': status,
              'source_index_url': INDEX_URL, 'source_latest_report': source_latest.isoformat(),
              'dataset_latest_report': latest.isoformat() if latest else None, 'dataset_age_days': age,
              'fresh': status == 'current'}
    target = Path('data/daily/iamc_status.json');target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(health, indent=2)+'\n')
    text = f'IAMC: {status}; published={source_latest}; local={latest}; age_days={age}.'
    print(('::warning::' if status == 'source_stale' else '')+text)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write(text+'\n')
    if status == 'local_lagging':
        raise RuntimeError('IAMC: a published report is missing locally; check download/parser')
    return health


def download_missing_iamc(lookback_days=7, source_reports=None):
    now_art = get_argentina_time()
    out_daily_dir = os.path.join("data", "daily")
    out_pdf_dir = os.path.join(out_daily_dir, "pdf")
    os.makedirs(out_pdf_dir, exist_ok=True)

    downloaded = []
    # Use advertised reports, including the latest even when publication stalls.
    source_reports = source_reports if source_reports is not None else discover_reports()
    cutoff = now_art.date() - timedelta(days=lookback_days)
    for dt in source_reports:
        if dt < cutoff and dt != max(source_reports):
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
        except Exception as error:
            print(f"::warning::IAMC: {name} download failed ({type(error).__name__})")

    if not downloaded:
        print("No se encontraron nuevos informes diarios pendientes en IAMC.")
    return downloaded


def main():
    keep_pdf = "--keep-pdf" in sys.argv
    source_reports = discover_reports()
    pending = download_missing_iamc(lookback_days=7, source_reports=source_reports)
    if not pending:
        check_freshness(max(source_reports))
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
        actual_date = datetime.strptime(date_str, '%d%m%Y').date().isoformat()
        if set(df['fecha_reporte']) != {actual_date}:
            raise RuntimeError(f'IAMC: PDF report date does not match requested date {actual_date}')

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

    check_freshness(max(source_reports))


if __name__ == "__main__":
    main()
