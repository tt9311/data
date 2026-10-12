import os
import glob
import re
import pandas as pd
import json
from pypdf import PdfReader

TICKER_RE = re.compile(r'^[A-Z0-9]{3,5}[CV][0-9\.]+[A-Z0-9]{1,3}$')
COMP_RE = re.compile(r"^([A-Z0-9\s\.\,\/\-\&]+)\s+\(([A-Z0-9]+)\)$")
EXP_RE = re.compile(r"Vencimiento\s+(?:en\s+)?([A-Za-z]+)\s*-\s*(\d{2}/\d{2}/\d{4})", re.I)
CALL_PUT_RE = re.compile(r"OPCIONES DE (COMPRA|VENTA)\s*\((CALL|PUT)\)", re.I)
STOP_PREFIXES = (
    "Vencimiento", "Días al", "Tasa Libre", "Índice Caución",
    "Power BI", "Symbol Strike", "OPCIONES DE"
)


def _to_float(val):
    if val is None or val == "\xa0":
        return None
    clean = val.replace("%", "").replace(",", "").strip()
    if not clean:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def _to_int(val, default=0):
    f = _to_float(val)
    return int(f) if f is not None else default


def parse_option_line(line, underlying, company, option_type, expiry_month, expiry_date, report_date):
    cols = line.strip(" \r\t").split(" ")
    if not cols:
        return None
    ticker = cols[0].strip()
    if not TICKER_RE.match(ticker):
        return None

    # Infer option type from ticker (4th or 5th char: C=CALL, V=PUT)
    tipo = option_type
    for char in ticker[3:5]:
        if char == 'C':
            tipo = "CALL"
            break
        elif char == 'V':
            tipo = "PUT"
            break

    record = {
        "fecha_reporte": report_date,
        "ticker": ticker,
        "subyacente": underlying,
        "empresa": company,
        "tipo_opcion": tipo,
        "mes_vencimiento": expiry_month,
        "fecha_vencimiento": expiry_date,
        "strike": None,
        "distancia_itm_otm_pct": None,
        "moneyness": None,
        "precio_subyacente": None,
        "apertura_prima": None,
        "min_prima": None,
        "max_prima": None,
        "ultimo_precio_prima": None,
        "var_prima_pct": None,
        "hora_ultimo_trade": None,
        "volumen_efectivo_ars": 0.0,
        "cant_operaciones": 0,
        "open_interest": 0.0,
        "var_open_interest_pct": None,
        "precio_teorico": None,
        "desvio_vs_teorico": None,
        "valor_temporal": None,
        "vol_historica_40r": None,
        "vol_implicita_pct": None,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "rho": None,
        "operado": False
    }

    # Exact 25-column layout from Power BI export (preserving \xa0 empty cells)
    if len(cols) == 25:
        record["strike"] = _to_float(cols[1])
        record["distancia_itm_otm_pct"] = _to_float(cols[2])
        record["moneyness"] = cols[3].strip() if cols[3] != "\xa0" else None
        record["precio_subyacente"] = _to_float(cols[4])
        record["apertura_prima"] = _to_float(cols[5])
        record["min_prima"] = _to_float(cols[6])
        record["max_prima"] = _to_float(cols[7])
        record["ultimo_precio_prima"] = _to_float(cols[8])
        record["var_prima_pct"] = _to_float(cols[9])
        hora = cols[10].strip() if cols[10] != "\xa0" else None
        record["hora_ultimo_trade"] = hora
        record["operado"] = bool(hora and re.match(r"^\d{2}:\d{2}:\d{2}$", hora))
        vol = _to_float(cols[11])
        record["volumen_efectivo_ars"] = vol if vol is not None else 0.0
        record["cant_operaciones"] = _to_int(cols[12], default=0)
        oi = _to_float(cols[13])
        record["open_interest"] = oi if oi is not None else 0.0
        record["var_open_interest_pct"] = _to_float(cols[14])
        record["precio_teorico"] = _to_float(cols[15])
        record["desvio_vs_teorico"] = _to_float(cols[16])
        record["valor_temporal"] = _to_float(cols[17])
        record["vol_historica_40r"] = _to_float(cols[18])
        record["vol_implicita_pct"] = _to_float(cols[19])
        record["delta"] = _to_float(cols[20])
        record["gamma"] = _to_float(cols[21])
        record["theta"] = _to_float(cols[22])
        record["vega"] = _to_float(cols[23])
        record["rho"] = _to_float(cols[24])
        return record

    # Fallback for rare vertically-clipped rows at visual border
    tokens = line.split()
    if len(tokens) > 1:
        record["strike"] = _to_float(tokens[1])
    for idx, t in enumerate(tokens[2:6], start=2):
        if t in ["ITM", "OTM", "ATM"]:
            record["moneyness"] = t
            if idx > 2:
                record["distancia_itm_otm_pct"] = _to_float(tokens[idx - 1])
            if idx + 1 < len(tokens):
                record["precio_subyacente"] = _to_float(tokens[idx + 1])
            break
    for t in reversed(tokens):
        if "%" in t:
            record["vol_historica_40r"] = _to_float(t)
            break
    return record


def parse_pdf_file(pdf_path):
    fname = os.path.basename(pdf_path)
    if "anexo" in fname.lower() or "aneox" in fname.lower():
        return []

    date_match = re.search(r'(\d{2})(\d{2})(\d{4})', fname)
    if date_match:
        report_date = f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
    else:
        report_date = "2026-10-05"

    reader = PdfReader(pdf_path)
    if not reader.pages:
        return []
    first_page_text = reader.pages[0].extract_text() or ""
    if "Anexo Opciones" in first_page_text:
        return []

    current_type = "CALL"
    current_expiry_month = "Octubre"
    current_expiry_date = "16/10/2026"
    current_company = ""
    current_underlying = ""

    records = []

    for page_idx in range(3, len(reader.pages)):
        page = reader.pages[page_idx]
        text = page.extract_text()
        if not text:
            continue

        raw_lines = [l.strip(" \r\t") for l in text.split("\n") if l.strip(" \r\t")]

        # Pass 1: Detect page-level Option Type and Vencimiento (located in page footer)
        for line in raw_lines:
            cp_m = CALL_PUT_RE.search(line)
            if cp_m:
                current_type = cp_m.group(2).upper()
            exp_m = EXP_RE.search(line)
            if exp_m:
                current_expiry_month = exp_m.group(1)
                current_expiry_date = exp_m.group(2)

        # Pass 2: Reconstruct wrapped option lines and parse
        i = 0
        while i < len(raw_lines):
            line = raw_lines[i]
            c_m = COMP_RE.match(line)
            if c_m and not line.startswith("Power BI") and "Índice" not in line and "Glosario" not in line:
                current_company = c_m.group(1).strip()
                current_underlying = c_m.group(2).strip()
                i += 1
                continue

            first_tok = line.split(" ")[0]
            if TICKER_RE.match(first_tok):
                cur = line
                i += 1
                while len(cur.split(" ")) < 25 and i < len(raw_lines):
                    nxt = raw_lines[i]
                    nxt_first = nxt.split(" ")[0]
                    if TICKER_RE.match(nxt_first) or nxt.startswith(STOP_PREFIXES) or COMP_RE.match(nxt):
                        break
                    if nxt.startswith("%") or (re.match(r"^\d+$", nxt) and re.search(r"\.\d*$", cur)):
                        cur = cur + nxt
                    else:
                        cur = cur + " " + nxt
                    i += 1
                rec = parse_option_line(
                    cur, current_underlying, current_company,
                    current_type, current_expiry_month, current_expiry_date, report_date
                )
                if rec:
                    records.append(rec)
            else:
                i += 1

    return records


def main():
    pdf_files = sorted(
        glob.glob("informes_pdf/InformeDiarioOpciones*.pdf")
        + glob.glob("archive_research/informes_pdf/InformeDiarioOpciones*.pdf")
    )
    print(f"Encontrados {len(pdf_files)} archivos PDF de Informe Diario para procesar.")

    os.makedirs(os.path.join("data", "daily"), exist_ok=True)

    all_records = []
    latest_records = []
    latest_date = ""
    latest_file = None

    for pdf_path in pdf_files:
        print(f"Procesando {pdf_path}...")
        recs = parse_pdf_file(pdf_path)
        print(f"  -> {len(recs)} opciones extraídas.")
        if not recs:
            continue
        all_records.extend(recs)
        rep_date = recs[0]["fecha_reporte"]
        daily_csv = os.path.join("data", "daily", f"options_iamc_{rep_date}.csv")
        pd.DataFrame(recs).to_csv(daily_csv, index=False, encoding="utf-8-sig")
        if rep_date >= latest_date:
            latest_date = rep_date
            latest_records = recs
            latest_file = pdf_path

    # 1. Export Latest Report
    df_latest = pd.DataFrame(latest_records)
    print(f"\nGuardando dataset del día más reciente ({latest_file}): {len(df_latest)} filas")
    
    # CSV
    csv_latest_path = "opciones_argentina_20261005.csv"
    df_latest.to_csv(csv_latest_path, index=False, encoding="utf-8-sig")
    print(f"  [OK] Guardado {csv_latest_path}")
    
    # JSON
    json_latest_path = "opciones_argentina_20261005.json"
    df_latest.to_json(json_latest_path, orient="records", indent=2, force_ascii=False)
    print(f"  [OK] Guardado {json_latest_path}")
    
    # Excel con múltiples solapas
    excel_latest_path = "opciones_argentina_20261005.xlsx"
    with pd.ExcelWriter(excel_latest_path, engine="openpyxl") as writer:
        # Solapa 1: Resumen
        resumen_data = {
            "Métrica": [
                "Fecha de Reporte",
                "Total Contratos de Opciones",
                "Opciones con Operaciones",
                "Opciones CALL",
                "Opciones PUT",
                "Total Volumen Efectivo (ARS)",
                "Total Contratos Open Interest",
                "Activos Subyacentes Únicos"
            ],
            "Valor": [
                df_latest["fecha_reporte"].iloc[0] if not df_latest.empty else "N/A",
                len(df_latest),
                len(df_latest[df_latest["operado"]]),
                len(df_latest[df_latest["tipo_opcion"] == "CALL"]),
                len(df_latest[df_latest["tipo_opcion"] == "PUT"]),
                f"${df_latest['volumen_efectivo_ars'].sum():,.2f}",
                f"{df_latest['open_interest'].sum():,.0f}",
                df_latest["subyacente"].nunique()
            ]
        }
        pd.DataFrame(resumen_data).to_excel(writer, sheet_name="Resumen", index=False)
        
        # Solapa 2: Todas las opciones
        df_latest.to_excel(writer, sheet_name="Todas", index=False)
        
        # Solapa 3: CALLs
        df_latest[df_latest["tipo_opcion"] == "CALL"].to_excel(writer, sheet_name="Calls", index=False)
        
        # Solapa 4: PUTs
        df_latest[df_latest["tipo_opcion"] == "PUT"].to_excel(writer, sheet_name="Puts", index=False)
        
        # Solapa 5: Más Operadas (ordenadas por volumen efectivo)
        df_operadas = df_latest[df_latest["operado"]].sort_values(by="volumen_efectivo_ars", ascending=False)
        df_operadas.to_excel(writer, sheet_name="Mas_Operadas", index=False)
        
    print(f"  [OK] Guardado {excel_latest_path}")

    # 2. Export Multi-Day Historical Consolidated Table
    df_all = pd.DataFrame(all_records)
    print(f"\nConsolidando histórico multirrueda: {len(df_all)} filas en total.")
    df_all.sort_values(by=["fecha_reporte", "subyacente", "strike"], inplace=True)
    
    hist_csv_path = "opciones_argentina_historico_multi_dia.csv"
    df_all.to_csv(hist_csv_path, index=False, encoding="utf-8-sig")
    print(f"  [OK] Guardado {hist_csv_path}")

    master_daily_path = os.path.join("data", "daily", "options_iamc_master_history.csv")
    df_all.to_csv(master_daily_path, index=False, encoding="utf-8-sig")
    print(f"  [OK] Guardado {master_daily_path}")

    latest_json_daily = os.path.join("data", "daily", "options_iamc_latest.json")
    df_latest.to_json(latest_json_daily, orient="records", indent=2, force_ascii=False)
    print(f"  [OK] Guardado {latest_json_daily}")

    print("\n¡Procesamiento completo finalizado exitosamente!")

if __name__ == "__main__":
    main()
