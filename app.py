# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, send_from_directory, send_file
import json
import io
import os
from pathlib import Path
import tempfile
import traceback
import decimal
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import re
import uuid
import urllib3
import time
import threading
from werkzeug.exceptions import NotFound

# SSL Uyarılarını Kapat
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- AYARLAR ---
decimal.getcontext().prec = 28
TWOPLACES = decimal.Decimal('0.01')

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = APP_DIR / 'config_templates'
STATIC_DIR = APP_DIR / 'static'
TEMP_RESULTS_DIR = APP_DIR / 'temp_results'
JOBS_DIR = APP_DIR / 'jobs'

CONFIG_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
TEMP_RESULTS_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024
app.config['JSON_AS_ASCII'] = False

print("Stok Yonetim Servisi Baslatildi.", flush=True)

# --- YARDIMCI FONKSİYONLAR ---
def update_job_status(job_id, status, progress, message, result_file=None, error=None):
    job_file = JOBS_DIR / f"{job_id}.json"
    data = {
        "status": status,
        "progress": progress,
        "message": f"%{progress} - {message}" if progress > 0 and status == "running" else message,
        "result_file": result_file,
        "error": str(error) if error else None,
        "timestamp": time.time()
    }
    with open(job_file, 'w') as f:
        json.dump(data, f)

def clean_column_name(col_name):
    if col_name is None: return ""
    s = str(col_name)
    s = s.replace('\xa0', ' ').replace('\t', ' ').replace('\n', ' ')
    return re.sub(r'\s+', ' ', s).strip().lower()

# --- STOK AYRIŞTIRMA VE ANALİZ ---
def parse_stock_value(stock_str):
    if pd.isna(stock_str) or stock_str is None: return 0
    s = str(stock_str).strip().lower()
    if not s or s == 'nan' or s == 'none': return 0
    negative_keywords = ["yok", "tükendi", "mevcut değil", "kalmadı", "gelince", "temin", "sorunuz", "stokta yok"]
    if any(kw in s for kw in negative_keywords): return 0
    s = s.replace(',', '.').replace('\xa0', '')
    val = 0
    try: 
        val = int(float(s))
    except:
        m = re.search(r'(\d+)', s)
        if m: val = int(m.group(1))
        else: val = 0
    return max(0, val)

# --- FİYAT AYRIŞTIRMA (PARSING) ---
def parse_price_value(val):
    if pd.isna(val) or val is None: return decimal.Decimal(0)
    s = str(val).strip()
    s = s.replace('\xa0', '').replace(' ', '')
    if not s or s.lower() in ['nan', 'none', '', '0']: return decimal.Decimal(0)
    s = re.sub(r'[^\d.,]', '', s)
    try:
        if '.' in s and ',' in s:
            if s.rfind(',') > s.rfind('.'): s = s.replace('.', '').replace(',', '.')
            else: s = s.replace(',', '')
        elif ',' in s: 
            s = s.replace(',', '.')
        return decimal.Decimal(s)
    except:
        return decimal.Decimal(0)

# --- METİN VE BİRİM STANDARDİZASYONU ---
def normalize_units(text):
    if not text: return ""
    text = text.lower()
    text = re.sub(r'(\d+)\s+(mm|cm|mt|m|gr|kg|w|v|lt|ml|bar|adet|pcs|set)\b', r'\1\2', text)
    replacements = {
        "watt": "w", "volt": "v", "amper": "amp", "siyah": "", "beyaz": "", 
        "kirmizi": "", "mavi": "", "sari": "", "yesil": "", "turuncu": "",
        "takim": "set", "cift": "set"
    }
    for k, v in replacements.items():
        text = re.sub(r'\b' + k + r'\b', v, text)
    return text

def strict_normalize(text):
    if not text: return ""
    text = str(text).lower()
    text = text.replace('ı', 'i').replace('ğ', 'g').replace('ü', 'u').replace('ş', 's').replace('ö', 'o').replace('ç', 'c')
    text = normalize_units(text)
    return re.sub(r'[^a-z0-9]', '', text)

# --- MATCH CODE KÖPRÜSÜ ---
def generate_match_code(code):
    if not code or pd.isna(code): return "KOD_YOK"
    s = str(code).upper().strip()
    prefixes = ["CETA", "IZELTAS", "BOSCH", "MAKITA", "DEWALT", "KNIPEX", "CERPA", "ELTA", "RTR", "ATTLAS"]
    for pre in prefixes:
        if s.startswith(pre):
            s = re.sub(f'^{pre}[-\s\.]*', '', s)
            break
    return re.sub(r'[^A-Z0-9]', '', s)

def load_template(name):
    p = CONFIG_DIR / f"{name}.json"
    if not p.exists(): return {}
    with open(p, 'r', encoding='utf-8') as f:
        return {k: clean_column_name(v) for k, v in json.load(f).items()}

def read_and_normalize_file(path, filename):
    print(f"DEBUG: Okunuyor -> {filename}", flush=True)
    try:
        if filename.lower().endswith('.csv'):
            try: df = pd.read_csv(path, dtype=str, encoding='utf-8-sig')
            except UnicodeDecodeError: df = pd.read_csv(path, dtype=str, encoding='latin-1')
        elif filename.lower().endswith('.xls'):
            try: df = pd.read_excel(path, dtype=str, engine='xlrd')
            except: df = pd.read_excel(path, dtype=str, engine='openpyxl')
        else:
            df = pd.read_excel(path, dtype=str)
    except Exception as e:
        raise Exception(f"'{filename}' okunamadı: {str(e)}")
    df.columns = [clean_column_name(c) for c in df.columns]
    return df.where(pd.notnull(df), None)

# --- DÖVİZ VE API ---
BASE_CURRENCY = "TRY"
EXCHANGE_RATES = {BASE_CURRENCY: decimal.Decimal(1.0)}
RATE_LAST_UPDATE = "Henüz Güncellenmedi"

def fetch_exchange_rates():
    global EXCHANGE_RATES, RATE_LAST_UPDATE
    print("DEBUG: TCMB Kur servisine baglaniliyor...", flush=True)
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        r = requests.get("https://www.tcmb.gov.tr/kurlar/today.xml", timeout=20, headers=headers, verify=False)
        
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            nr = {BASE_CURRENCY: decimal.Decimal(1.0)}
            for c in ["USD", "EUR"]:
                n = root.find(f"./Currency[@CurrencyCode='{c}']/ForexSelling")
                if n is None or not n.text: n = root.find(f"./Currency[@CurrencyCode='{c}']/BanknoteSelling")
                if n is not None and n.text:
                    nr[c] = decimal.Decimal(n.text.replace(',', '.'))
            if "USD" in nr:
                EXCHANGE_RATES = nr
                RATE_LAST_UPDATE = datetime.now().strftime("%d-%m-%Y %H:%M")
                print(f"DEBUG: Kurlar Guncellendi -> USD: {nr.get('USD')} - EUR: {nr.get('EUR')}", flush=True)
                return True, f"Kurlar güncellendi. ({RATE_LAST_UPDATE})"
        else:
            print(f"DEBUG: TCMB Yanit Kodu Hatali: {r.status_code}", flush=True)
    except Exception as e:
        print(f"DEBUG: KUR CEKME HATASI: {str(e)}", flush=True)
        traceback.print_exc()
    return False, "Kur alınamadı."

try: fetch_exchange_rates()
except: pass

# --- NLP Fiyatlandırma ve Kural Motoru ---
def parse_natural_language_rules(text_input):
    rules = []
    if not text_input: return rules
    lines = text_input.split('\n')
    for line in lines:
        line = line.strip().upper()
        if not line: continue
        
        target = "TANIMSIZ"
        if any(x in line for x in ["TUM", "HEPSI", "GENEL", "HERKES", "BUTUN", "TÜM", "BÜTÜN"]):
            target = "ALL_PRODUCTS"
        else:
            parts = line.split()
            if parts:
                target = parts[0]
                if len(parts) > 1 and parts[1] in ["FORM", "EXTRA", "POWER", "PLUS", "DECKER", "LI"]:
                     target = f"{parts[0]} {parts[1]}"

        action_type = "multiplier" 
        val = decimal.Decimal(0)
        currency = None 
        old_rate = None
        
        old_rate_match = re.search(r'ESKI_KUR\s*=\s*(\d+[.,]?\d*)', line)
        if old_rate_match:
            try: old_rate = decimal.Decimal(old_rate_match.group(1).replace(',', '.'))
            except: old_rate = None

        clean_line = re.sub(r'ESKI_KUR\s*=\s*(\d+[.,]?\d*)', '', line)
        numbers = re.findall(r'(\d+[.,]?\d*)', clean_line)
        
        found_val = False
        for num_str in numbers:
            if num_str in target: continue
            try:
                val = decimal.Decimal(num_str.replace(',', '.'))
                found_val = True
                break
            except: pass
        
        if not found_val: val = decimal.Decimal(0)
            
        if "USD" in line or "DOLAR" in line: currency = "USD"
        elif "EUR" in line or "EURO" in line: currency = "EUR"
        elif "TRY" in line or "TL" in line: currency = "TRY"
        
        if old_rate and old_rate > 0:
            action_type = "fx_conversion"
            if not currency: currency = "USD"
            
        elif any(x in line for x in ["KURA", "KURU", "DOVIZ", "ENDEKS"]) and any(x in line for x in ["ESITLE", "CEVIR", "YAP", "GUNCELLE"]):
            action_type = "fx_index"
            if not currency: currency = "USD"
            
        elif any(x in line for x in ["ZAM", "ARTIS", "EKLE", "YUKSELT"]):
            action_type = "multiplier"
            if "%" in line or "YUZDE" in line: val = 1 + (val / 100)
            else: pass 
            
        elif any(x in line for x in ["INDIRIM", "ISKONTO", "DUS", "AZALT"]):
            action_type = "multiplier"
            if "%" in line or "YUZDE" in line: val = 1 - (val / 100)
            else: val = -val 
            
        elif any(x in line for x in ["OLSUN", "SABITLE", "YAP", "FIKSE", "AYARLA"]):
            action_type = "fix_price"
            
        rules.append({
            "target": target,
            "action": action_type,
            "value": val,
            "currency": currency,
            "old_rate": old_rate,
            "raw_text": line
        })
    return rules

@app.route('/')
def index(): return send_from_directory(str(STATIC_DIR), 'index.html')

@app.route('/documentation')
def serve_documentation(): return send_from_directory(str(STATIC_DIR), 'teknik_dokumantasyon.html')

@app.route('/api/v1/exchange-rates', methods=['GET'])
def get_rates():
    return jsonify({"rates": {k: str(v) for k,v in EXCHANGE_RATES.items()}, "last_update": RATE_LAST_UPDATE})

@app.route('/api/v1/exchange-rates/refresh', methods=['POST'])
def refresh_rates():
    ok, msg = fetch_exchange_rates()
    return jsonify({"mesaj": msg}) if ok else (jsonify({"hata": msg}), 503)

@app.route('/api/v1/templates', methods=['POST', 'GET'])
def handle_templates():
    if request.method == 'POST':
        data = request.json
        with open(CONFIG_DIR / f"{data['template_name']}.json", 'w', encoding='utf-8') as f:
            json.dump(data['config'], f, ensure_ascii=False, indent=4)
        return jsonify({"mesaj": "Kaydedildi"}), 201
    else:
        return jsonify({"templates": [f.stem for f in CONFIG_DIR.glob('*.json')]})

@app.route('/api/v1/templates/export_all', methods=['GET'])
def export_all_templates():
    try:
        all_data = []
        for p in CONFIG_DIR.glob('*.json'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    all_data.append({"template_name": p.stem, "config": json.load(f)})
            except: pass
        return jsonify(all_data)
    except Exception as e: return jsonify({"hata": str(e)}), 500

@app.route('/api/v1/templates/import_all', methods=['POST'])
def import_all_templates():
    try:
        data = request.json
        count = 0
        for item in data:
            name = item.get('template_name')
            config = item.get('config')
            if name and config:
                with open(CONFIG_DIR / f"{name}.json", 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=4)
                count += 1
        return jsonify({"mesaj": f"{count} şablon yüklendi."})
    except Exception as e: return jsonify({"hata": str(e)}), 500

@app.route('/api/v1/templates/reset', methods=['POST'])
def reset_templates():
    try:
        for f in CONFIG_DIR.glob('*.json'): os.remove(f)
        return jsonify({"mesaj": "Temizlendi."})
    except: return jsonify({"hata": "Hata"}), 500

@app.route('/api/v1/templates/<name>', methods=['DELETE', 'GET'])
def template_ops(name):
    p = CONFIG_DIR / f"{name}.json"
    if request.method == 'DELETE':
        if p.exists(): os.remove(p)
        return jsonify({"mesaj": "Silindi"})
    else:
        if not p.exists(): return jsonify({}), 404
        with open(p, 'r', encoding='utf-8') as f: return jsonify({"config": json.load(f)})

# --- SİMÜLASYON ENDPOINT ---
@app.route('/api/v1/simulate_nlp', methods=['POST'])
def simulate_nlp():
    try:
        if 'file' not in request.files: return jsonify({"error": "Dosya yok"}), 400
        f = request.files['file']
        rules_text = request.form.get('rules', '')
        tpl_name = request.form.get('template_name', '')
        
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=Path(f.filename).suffix)
        f.save(tf.name)
        df = read_and_normalize_file(tf.name, f.filename)
        os.remove(tf.name)
        
        tpl = load_template(tpl_name)
        rules = parse_natural_language_rules(rules_text)
        
        c_price = tpl.get('current_price')
        c_name = tpl.get('product_name')
        c_brand = tpl.get('brand')
        c_sku = tpl.get('sku')
        
        preview_data = []
        scan_limit = min(len(df), 200) 
        scan_df = df.head(scan_limit)
        
        for idx, row in scan_df.iterrows():
            curr_p = parse_price_value(row.get(c_price, 0)) if c_price in row else decimal.Decimal(0)
            prod_name = str(row.get(c_name, '')).upper()
            brand = str(row.get(c_brand, '')).upper()
            sku = str(row.get(c_sku, '')).upper()
            
            matched_rules = []
            final_p = curr_p
            rule_descriptions = []
            
            for rule in rules:
                is_match = False
                if rule['target'] == "ALL_PRODUCTS": is_match = True
                elif rule['target'] in brand: is_match = True
                elif rule['target'] in prod_name: is_match = True
                elif rule['target'] in sku: is_match = True 
                
                if is_match:
                    matched_rules.append(rule['raw_text'])
                    
                    desc = ""
                    if final_p > 0 or rule['action'] == 'fix_price':
                        if rule['action'] == 'multiplier':
                            if rule['value'] > 1 or rule['value'] < 1: 
                                final_p = final_p * rule['value']
                                pct = int((rule['value'] - 1) * 100)
                                desc = f"Yüzde {'Zam' if pct > 0 else 'İndirim'} (%{abs(pct)})"
                            else: 
                                final_p = final_p + rule['value']
                                desc = f"Tutar {'Ekleme' if rule['value'] > 0 else 'Düşme'} ({rule['value']})"
                        elif rule['action'] == 'fix_price':
                             p_val = rule['value']
                             curr_symbol = rule['currency'] if rule['currency'] else 'TRY'
                             if rule['currency'] and rule['currency'] != 'TRY':
                                 rate = EXCHANGE_RATES.get(rule['currency'], 1)
                                 p_val = p_val * rate
                             final_p = p_val
                             desc = f"Sabit Fiyat: {rule['value']} {curr_symbol}"
                        elif rule['action'] == 'fx_index':
                            rate = EXCHANGE_RATES.get(rule['currency'] or 'USD', 1)
                            final_p = final_p * rate 
                            desc = f"Döviz Endeksleme ({rule['currency'] or 'USD'})"
                        elif rule['action'] == 'fx_conversion':
                            if rule['old_rate'] and rule['old_rate'] > 0:
                                rate_curr = rule['currency'] if rule['currency'] else 'USD'
                                new_rate = EXCHANGE_RATES.get(rate_curr, 1)
                                final_p = (final_p / rule['old_rate']) * new_rate
                                desc = f"Kur Farkı Uygulaması (Eski: {rule['old_rate']} -> Yeni: {new_rate})"
                    
                    if desc: rule_descriptions.append(desc)
            
            if matched_rules:
                preview_data.append({
                    "urun": (sku + " - " + prod_name)[:50] + "...",
                    "eski": str(curr_p),
                    "yeni": str(final_p.quantize(TWOPLACES)),
                    "kurallar": ", ".join(rule_descriptions) 
                })
                
        return jsonify({
            "preview": preview_data[:10] 
        })
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# --- STOK HESAPLAMA ---
def calculate_internal_stock(files, thr, amt):
    all_df = pd.DataFrame()
    meta_info = {}
    for f in files:
        df=f['dataframe']; tpl=f['template']; lbl=f['label']; fname=f['filename']
        meta_info[fname] = len(df)
        t_sku = tpl.get('sku'); t_stock = tpl.get('stock')
        sub = pd.DataFrame()
        sub['Anahtar_Kod'] = df[t_sku].fillna('KOD_YOK').astype(str) if t_sku and t_sku in df else 'KOD_YOK'
        sub['match_code'] = sub['Anahtar_Kod'].apply(generate_match_code)
        sub['Miktar'] = df[t_stock].apply(parse_stock_value) if t_stock and t_stock in df else 0
        sub['Barkod'] = df[tpl['barcode']].fillna('_barkod_yok_').astype(str) if tpl.get('barcode') in df else '_barkod_yok_'
        sub['Marka'] = df[tpl['brand']].fillna('TANIMSIZ').astype(str).str.upper() if tpl.get('brand') in df else 'TANIMSIZ'
        t_price = tpl.get('selling_price')
        sub['Ic_Hazir_Fiyat'] = df[t_price].apply(parse_price_value) if t_price and t_price in df else 0
        t_name = tpl.get('product_name')
        if t_name and t_name in df: sub['Ic_Urun_Adi'] = df[t_name].fillna('').astype(str)
        else: sub['Ic_Urun_Adi'] = ''
        if lbl == '-': sub['Miktar'] = sub['Miktar'].abs() * -1
        all_df = pd.concat([all_df, sub], ignore_index=True)

    if all_df.empty: return pd.DataFrame(), meta_info
    
    net = all_df.groupby(['Anahtar_Kod', 'Barkod', 'match_code'], as_index=False).agg(
        Hesaplanan_Stok=('Miktar','sum'), 
        Marka=('Marka','first'), 
        Ic_Urun_Adi=('Ic_Urun_Adi', 'first'),
        Ic_Hazir_Fiyat=('Ic_Hazir_Fiyat', 'max')
    )
    sec = amt if amt else decimal.Decimal(0)
    def apply_sec(r):
        v = decimal.Decimal(str(r['Hesaplanan_Stok']))
        return v - sec if thr is None or v > thr else v
    
    net['Nihai_Stok'] = net.apply(apply_sec, axis=1) if thr is not None else net['Hesaplanan_Stok']
    net['Hesaplanan_Stok'] = net['Hesaplanan_Stok'].astype(int)
    net['Nihai_Stok'] = net['Nihai_Stok'].astype(int)
    net['Barkod'] = net['Barkod'].replace('_barkod_yok_', 'YOK')
    return net, meta_info

# --- TEDARİKÇİ KONSOLİDE ---
def consolidate_suppliers(files):
    all_df = pd.DataFrame()
    meta_info = {}
    for f in files:
        df=f['dataframe']; tpl=f['template']; fname=f['filename']
        meta_info[fname] = len(df)
        t_sku = tpl.get('sku'); t_stock = tpl.get('stock')
        sub = pd.DataFrame()
        sub['Anahtar_Kod'] = df[t_sku].fillna('KOD_YOK').astype(str) if t_sku and t_sku in df else 'KOD_YOK'
        sub['match_code'] = sub['Anahtar_Kod'].apply(generate_match_code)
        sub['Miktar'] = df[t_stock].apply(parse_stock_value).clip(lower=0) if t_stock and t_stock in df else 0
        sub['Barkod'] = df[tpl['barcode']].fillna('_barkod_yok_').astype(str) if tpl.get('barcode') in df else '_barkod_yok_'
        sub['Maliyet'] = df[tpl['cost']].apply(parse_price_value) if tpl.get('cost') in df else 0
        t_price = tpl.get('selling_price')
        sub['Ted_Hazir_Fiyat'] = df[t_price].apply(parse_price_value) if t_price and t_price in df else 0
        sub['Marka'] = df[tpl['brand']].fillna('TANIMSIZ').astype(str).str.upper() if tpl.get('brand') in df else 'TANIMSIZ'
        t_name = tpl.get('product_name')
        if t_name and t_name in df: sub['Ted_Urun_Adi'] = df[t_name].fillna('').astype(str)
        else: sub['Ted_Urun_Adi'] = ''
        p_col = tpl.get('currency_column')
        sub['Para_Birimi'] = df[p_col] if p_col and p_col in df else tpl.get('currency', 'TRY')
        def convert(r):
            try:
                c = r['Maliyet']
                cur = str(r['Para_Birimi']).strip().upper()
                if cur == BASE_CURRENCY: return c
                rate = EXCHANGE_RATES.get(cur)
                return c * rate if rate else decimal.Decimal(0)
            except: return decimal.Decimal(0)
        sub['Maliyet_TRY'] = sub.apply(convert, axis=1)
        all_df = pd.concat([all_df, sub], ignore_index=True)
    if all_df.empty: return pd.DataFrame(), meta_info
    
    v_bc = all_df[all_df['Barkod'] != '_barkod_yok_']
    g_bc = v_bc.groupby(['Barkod'], as_index=False).agg(
        Anahtar_Kod=('Anahtar_Kod','first'),
        match_code=('match_code', 'first'), 
        Toplam_Tedarikci_Stok=('Miktar','sum'), 
        Maliyet=('Maliyet_TRY','min'), 
        Ted_Hazir_Fiyat=('Ted_Hazir_Fiyat', 'max'),
        Marka=('Marka','first'), 
        Ted_Urun_Adi=('Ted_Urun_Adi', 'first')
    ) if not v_bc.empty else pd.DataFrame()
    
    v_sku = all_df[all_df['Barkod'] == '_barkod_yok_']
    g_sku = v_sku.groupby('match_code', as_index=False).agg(
        Anahtar_Kod=('Anahtar_Kod','first'),
        Toplam_Tedarikci_Stok=('Miktar','sum'), 
        Maliyet=('Maliyet_TRY','min'), 
        Ted_Hazir_Fiyat=('Ted_Hazir_Fiyat', 'max'),
        Marka=('Marka','first'), 
        Ted_Urun_Adi=('Ted_Urun_Adi', 'first')
    ) if not v_sku.empty else pd.DataFrame()
    g_sku['Barkod'] = 'YOK'
    
    final = pd.concat([g_bc, g_sku], ignore_index=True)
    final['Maliyet'] = final['Maliyet'].fillna(0)
    final['Toplam_Tedarikci_Stok'] = final['Toplam_Tedarikci_Stok'].fillna(0).astype(int)
    final['Ted_Hazir_Fiyat'] = final['Ted_Hazir_Fiyat'].fillna(0)
    final['Barkod'] = final['Barkod'].replace('_barkod_yok_', 'YOK')
    return final, meta_info

# --- UNIVERSAL SMART MATCHING ENGINE (Enhanced) ---
class UniversalSmartMatcher:
    def __init__(self, internal_df, marketplace_df):
        self.int_df = internal_df.copy()
        self.mp_df = marketplace_df.copy()
        self.THRESHOLD_TRUSTED = 0.35 
        self.THRESHOLD_HIGH = 0.75    
        self.THRESHOLD_NUMERIC = 0.50 
        
        self.BANNED_CODES = { "SET", "ADET", "PARCA", "TAKIM", "CANTALI", "KUTULU", "PRO", "PLUS", "MAX" }
        self.KNOWN_BRANDS = { "BOSCH", "MAKITA", "DEWALT", "MILWAUKEE", "STANLEY", "BLACK&DECKER", "CETA FORM", "IZELTAS", "KNIPEX", "PROXXON", "WERA", "WIHA", "ATTLAS", "RTRMAX", "CATPOWER", "EINHELL", "KARCHER", "LOCTITE", "DBK", "KLPRO", "MAX EXTRA", "ROTA", "GLOBE", "YKAR", "CERMAX", "INGCO", "TOTAL", "RODEX", "CRAFT", "MAGMAWELD", "ASKAYNAK", "CERPA", "ALTAS", "ALTAŞ", "WOLFCRAFT", "UNI-T", "UNIT", "AEG", "ELTA", "MASTECH", "LUTION", "LUTIAN", "MYTOL", "CORAH", "HITACHI", "HIKOKI", "PIECESS", "ZOBO", "DURACELL", "GP", "VARTA", "OSRAM", "PHILIPS", "RAPID", "CHATTEL", "TODRILL", "RUBI", "KRISTAL", "RODEX", "MIKASSO", "KLEIN", "DREMEL", "RYOBI", "METABO", "HILTI", "STAYER", "VIRAX", "ROTHENBERGER", "RIDGID", "REMS" }
        self.BRAND_CONFLICTS = { "CETA FORM": ["IZELTAS", "CERPA", "ALTAS", "KNIPEX", "ELTA"], "IZELTAS": ["CETA FORM", "CERPA", "ALTAS", "KNIPEX"], "CERPA": ["CETA FORM", "IZELTAS", "KNIPEX", "ALTAS"], "BOSCH": ["MAKITA", "DEWALT", "MILWAUKEE", "EINHELL", "RTRMAX", "DBK", "AEG", "HITACHI"], "MAKITA": ["BOSCH", "DEWALT", "MILWAUKEE", "EINHELL", "RTRMAX", "DBK", "AEG", "HITACHI"], "RTRMAX": ["BOSCH", "MAKITA", "DEWALT", "EINHELL", "CATPOWER", "AEG", "HITACHI", "ATTLAS", "CHATTEL", "INGCO"], "INGCO": ["TOTAL", "RTRMAX", "ATTLAS", "CATPOWER"], "KNIPEX": ["IZELTAS", "CETA FORM", "CERPA"], "MILWAUKEE": ["DEWALT", "MAKITA", "BOSCH"], "HITACHI": ["MAKITA", "BOSCH", "DEWALT", "RTRMAX"] }
        
    def normalize_text(self, text):
        if not isinstance(text, str): return ""
        text = text.lower()
        text = re.sub(r'\b(rm_|tyc_|hbv|akn_|frkn)\w*', '', text) 
        text = text.replace("frkn", "")
        tr_map = str.maketrans("ğüşıöçâêîôû", "gusiocaeiou")
        text = text.translate(tr_map)
        text = normalize_units(text)
        noise = ["orijinal", "ithal", "yerli", "uretim", "yeni", "kampanya", "kargo", "bedava", "firsati", "garantili"]
        for w in noise: text = text.replace(w, "")
        text = re.sub(r'[^a-z0-9\s]', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    def normalize_brand(self, brand_text):
        if not brand_text or str(brand_text).upper() in ['TANIMSIZ', 'NAN', 'NONE', 'YOK', 'DIĞER', 'DIGER', 'NULL']: return "TANIMSIZ"
        b = str(brand_text).upper().replace('İ', 'I').replace('Ğ', 'G').replace('Ü', 'U').replace('Ş', 'S').replace('Ö', 'O').replace('Ç', 'C')
        if "CETA" in b: return "CETA FORM"
        if "IZEL" in b or b == "IZ": return "IZELTAS"
        if "CER" in b and "PA" in b: return "CERPA"
        if "UNI" in b and "T" in b: return "UNIT"
        if "BLACK" in b and "DECKER" in b: return "BLACK&DECKER"
        return b.strip()

    def extract_brand_from_title(self, title):
        title_upper = str(title).upper().replace('İ', 'I')
        sorted_brands = sorted(self.KNOWN_BRANDS, key=len, reverse=True)
        for b in sorted_brands:
            if re.search(r'\b' + re.escape(b) + r'\b', title_upper): return b
        if "IZEL" in title_upper: return "IZELTAS"
        return "TANIMSIZ"

    def detect_brand_smart(self, row, source_type):
        col_brand = self.normalize_brand(row.get('MP_Marka' if source_type == 'mp' else 'marka', 'TANIMSIZ'))
        if col_brand != "TANIMSIZ" and len(col_brand) > 2: return col_brand
        text = str(row.get('MP_Urun_Adi' if source_type == 'mp' else 'ic_urun_adi', ''))
        return self.extract_brand_from_title(text)

    def is_brand_conflict(self, b1, b2):
        if b1 == "TANIMSIZ" or b2 == "TANIMSIZ": return False
        if b1 == b2: return False
        if b1 in b2 or b2 in b1: return False
        if b1 in self.BRAND_CONFLICTS and b2 in self.BRAND_CONFLICTS[b1]: return True
        if b2 in self.BRAND_CONFLICTS and b1 in self.BRAND_CONFLICTS[b2]: return True
        if b1 in self.KNOWN_BRANDS and b2 in self.KNOWN_BRANDS: return True
        return False

    def get_numbers(self, text):
        return set(re.findall(r'\b\d+[a-z]*\b', text))

    def extract_identity_codes(self, text):
        text_clean = self.normalize_text(text).upper()
        tokens = text_clean.split()
        codes = set()
        for t in tokens:
            if len(t) < 3: continue 
            if t in self.BANNED_CODES: continue
            if any(c.isdigit() for c in t) and any(c.isalpha() for c in t):
                codes.add(t)
            elif t.isalpha() and len(t) >= 4 and t not in self.KNOWN_BRANDS:
                codes.add(t)
        return codes

    def check_set_count_conflict(self, t1, t2):
        p1 = re.search(r'(\d+)\s*(parca|prc|set|li)', t1.lower())
        p2 = re.search(r'(\d+)\s*(parca|prc|set|li)', t2.lower())
        if p1 and p2:
            if p1.group(1) != p2.group(1): return True
        return False

    def calculate_hybrid_score(self, vector_score, row_text, cand_text):
        norm1 = self.normalize_text(row_text)
        norm2 = self.normalize_text(cand_text)
        
        tokens1 = set(norm1.split())
        tokens2 = set(norm2.split())
        
        if not tokens1 or not tokens2: return 0.0
        
        intersection = len(tokens1.intersection(tokens2))
        union = len(tokens1.union(tokens2))
        jaccard = intersection / union if union > 0 else 0
        
        final_score = (vector_score * 0.6) + (jaccard * 0.4)
        return min(final_score, 1.0)

    def run_engine(self):
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
        except ImportError: return pd.DataFrame()
        
        self.int_df['norm_name'] = self.int_df['ic_urun_adi'].astype(str).apply(self.normalize_text)
        self.mp_df['norm_name'] = self.mp_df['MP_Urun_Adi'].astype(str).apply(self.normalize_text)
        
        valid_int = self.int_df[self.int_df['norm_name'].str.len() > 3].reset_index(drop=True)
        valid_mp = self.mp_df[self.mp_df['norm_name'].str.len() > 3].reset_index(drop=True)
        
        if valid_int.empty or valid_mp.empty: return pd.DataFrame()
        
        vectorizer = TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 4), min_df=1, dtype=np.float32)
        
        try:
            vectorizer.fit(pd.concat([valid_int['norm_name'], valid_mp['norm_name']]))
            int_matrix = vectorizer.transform(valid_int['norm_name'])
            mp_matrix = vectorizer.transform(valid_mp['norm_name'])
            similarity_scores = cosine_similarity(mp_matrix, int_matrix)
        except: return pd.DataFrame()
        
        results = []
        
        for i, (idx, row) in enumerate(valid_mp.iterrows()):
            best_idx = similarity_scores[i].argmax()
            vector_score = similarity_scores[i][best_idx]
            
            if vector_score < 0.15:
                match_data = row.to_dict(); match_data['Eslestirme'] = 'Eşleşmedi'; match_data['anahtar_kod']='YOK'; results.append(match_data); continue
            
            candidate = valid_int.iloc[best_idx]
            
            match_data = row.to_dict()
            mp_title = str(row['MP_Urun_Adi'])
            int_title = str(candidate['ic_urun_adi'])
            
            mp_brand = self.detect_brand_smart(row, 'mp')
            int_brand = self.detect_brand_smart(candidate, 'int')
            brand_conflict = self.is_brand_conflict(mp_brand, int_brand)
            brands_match = (mp_brand == int_brand) and (mp_brand != "TANIMSIZ")
            
            nums_mp = self.get_numbers(self.normalize_text(mp_title))
            nums_int = self.get_numbers(self.normalize_text(int_title))
            
            numeric_match = False
            if nums_mp and nums_int:
                if nums_mp.issubset(nums_int) or nums_int.issubset(nums_mp):
                    numeric_match = True
            
            codes1 = self.extract_identity_codes(mp_title)
            codes2 = self.extract_identity_codes(int_title)
            common_codes = codes1.intersection(codes2)
            
            has_strong_code_match = False
            if common_codes:
                longest = max(common_codes, key=len)
                if len(longest) >= 3: has_strong_code_match = True
            
            if not has_strong_code_match:
                n1 = self.normalize_text(mp_title).replace(" ", "")
                n2 = self.normalize_text(int_title).replace(" ", "")
                for code in codes2:
                    if len(code) > 3 and code.lower() in n1:
                        has_strong_code_match = True
                        break
            
            set_conflict = self.check_set_count_conflict(mp_title, int_title)
            hybrid_score = self.calculate_hybrid_score(vector_score, mp_title, int_title)
            
            match_data['Algoritma_Skoru'] = round(hybrid_score * 100, 2)
            
            final_decision = "Eşleşmedi"
            
            if brand_conflict:
                if has_strong_code_match and not set_conflict and numeric_match:
                    final_decision = "Füzyon (Marka Farklı ama Kod ve Sayılar Aynı)"
                else:
                    final_decision = "Eşleşmedi (Marka Çatışması)"
                    
            elif set_conflict:
                final_decision = "Eşleşmedi (Set Sayısı Farkı)"
                
            elif has_strong_code_match:
                final_decision = "Füzyon (Altın Kod)"
                
            elif brands_match:
                if hybrid_score > self.THRESHOLD_TRUSTED: 
                    final_decision = "Füzyon (Güvenli Marka)"
                elif numeric_match and hybrid_score > 0.25:
                    final_decision = "Füzyon (Marka + Sayısal Eşleşme)"
                    
            else: 
                if numeric_match and hybrid_score > self.THRESHOLD_NUMERIC:
                    final_decision = "Füzyon (Güçlü Sayısal Benzerlik)"
                elif hybrid_score > self.THRESHOLD_HIGH:
                    final_decision = "Füzyon (Yüksek Metin Benzerliği)"
            
            if "Eşleşmedi" not in final_decision:
                match_data.update(candidate.to_dict())
                match_data['Eslestirme'] = final_decision
            else:
                match_data['Eslestirme'] = final_decision
                match_data['anahtar_kod'] = 'YOK'
            
            results.append(match_data)
            
        return pd.DataFrame(results)

def run_matching_job(job_id, ikey, skey, mp_path, mp_filename, tpl_n, stock_strat, price_strat, orphan_strat, smart_freeze, freeze_conf, brand_strat, include_orig):
    try:
        update_job_status(job_id, "running", 5, "Adım 1/5: Veri Setleri Yükleniyor...")
        
        internal_df = pd.read_json(TEMP_RESULTS_DIR/f"internal_{ikey}.json")
        internal_df.columns=[c.lower() for c in internal_df.columns]
        
        supplier_df = pd.read_json(TEMP_RESULTS_DIR/f"supplier_{skey}.json") if skey else pd.DataFrame()
        if not supplier_df.empty: supplier_df.columns=[c.lower() for c in supplier_df.columns]
        
        with open(TEMP_RESULTS_DIR/f"meta_internal_{ikey}.json") as f: meta_int = json.load(f)
        
        mp_df = read_and_normalize_file(mp_path, mp_filename)
        os.remove(mp_path)
        mp_tpl = load_template(tpl_n)
        
        s_bc=mp_tpl.get('barcode'); s_sku=mp_tpl.get('sku'); s_stk=mp_tpl.get('stock_to_update'); s_prc=mp_tpl.get('current_price'); s_nam=mp_tpl.get('product_name'); s_brn=mp_tpl.get('brand')
        
        mp = pd.DataFrame()
        if s_bc and s_bc in mp_df.columns: mp['MP_Barkod'] = mp_df[s_bc].astype(str).replace('nan','YOK').fillna('YOK').str.strip()
        else: mp['MP_Barkod'] = 'YOK'
        if s_sku and s_sku in mp_df.columns: mp['MP_SKU'] = mp_df[s_sku].astype(str).replace('nan','YOK').fillna('YOK').str.strip()
        else: mp['MP_SKU'] = 'YOK'
        if s_nam and s_nam in mp_df.columns: mp['MP_Urun_Adi'] = mp_df[s_nam].astype(str).fillna('')
        else: mp['MP_Urun_Adi'] = ''
        if s_stk and s_stk in mp_df.columns: mp['MP_Eski_Stok'] = mp_df[s_stk].apply(parse_stock_value)
        else: mp['MP_Eski_Stok'] = 0
        if s_prc and s_prc in mp_df.columns: mp['MP_Fiyat'] = mp_df[s_prc].apply(lambda x: decimal.Decimal(str(x).replace(',','.')) if pd.notna(x) and str(x).strip()!="" else decimal.Decimal(0))
        else: mp['MP_Fiyat'] = decimal.Decimal(0)
        if s_brn and s_brn in mp_df.columns: mp['MP_Marka'] = mp_df[s_brn].astype(str).fillna('TANIMSIZ').str.upper()
        else: mp['MP_Marka'] = 'TANIMSIZ'
        mp['idx'] = mp.index
        mp['bk_norm'] = mp['MP_Barkod'].apply(strict_normalize)
        mp['sku_norm'] = mp['MP_SKU'].apply(strict_normalize)
        internal_df['bk_norm'] = internal_df['barkod'].apply(strict_normalize)
        internal_df['sku_norm'] = internal_df['anahtar_kod'].apply(strict_normalize)
        
        results = []
        processed = set()
        
        update_job_status(job_id, "running", 15, "Adım 2/5: Barkod ve SKU Taraması Yapılıyor...")
        mp_valid = mp[mp['bk_norm'].str.len() > 4]
        int_valid = internal_df[internal_df['bk_norm'].str.len() > 4]
        if not mp_valid.empty and not int_valid.empty:
            m1 = pd.merge(mp_valid, int_valid, on='bk_norm', how='inner', suffixes=('', '_ic'))
            for _, r in m1.iterrows():
                if r['idx'] not in processed:
                    d = r.to_dict(); d['Eslestirme'] = 'Barkod'; results.append(d); processed.add(r['idx'])

        rem = mp[~mp['idx'].isin(processed)]
        rem_valid = rem[rem['sku_norm'].str.len() > 2]
        int_valid_sku = internal_df[internal_df['sku_norm'].str.len() > 2]
        if not rem_valid.empty and not int_valid_sku.empty:
            m2 = pd.merge(rem_valid, int_valid_sku, on='sku_norm', how='inner', suffixes=('', '_ic'))
            for _, r in m2.iterrows():
                if r['idx'] not in processed:
                    d = r.to_dict(); d['Eslestirme'] = 'SKU'; results.append(d); processed.add(r['idx'])

        update_job_status(job_id, "running", 40, "Adım 3/5: Akıllı Eşleştirme Motoru (İsim Analizi)...")
        remaining_mp = mp[~mp['idx'].isin(processed)].copy()
        if not remaining_mp.empty and not internal_df.empty:
            matcher = UniversalSmartMatcher(internal_df, remaining_mp)
            ai_results_df = matcher.run_engine()
            if not ai_results_df.empty:
                for _, row in ai_results_df.iterrows():
                    if row['idx'] not in processed and row['Eslestirme'] != 'Eşleşmedi':
                        d = row.to_dict(); results.append(d); processed.add(row['idx'])

        unmatched = mp[~mp['idx'].isin(processed)]
        for _, r in unmatched.iterrows():
            d = r.to_dict(); d.update({'Eslestirme':'Eşleşmedi', 'nihai_stok':0, 'hesaplanan_stok':0, 'toplam_tedarikci_stok':0, 'maliyet':0, 'anahtar_kod':'YOK', 'marka':'YOK', 'ic_hazir_fiyat':0})
            results.append(d)
            
        final = pd.DataFrame(results)
        
        if not supplier_df.empty:
            if 'match_code' not in final.columns: 
                final['match_code'] = final['anahtar_kod'].apply(generate_match_code)
            if 'match_code' not in supplier_df.columns:
                supplier_df['match_code'] = supplier_df['anahtar_kod'].apply(generate_match_code)

            sup_lookup = supplier_df[['match_code', 'toplam_tedarikci_stok', 'maliyet', 'marka', 'ted_hazir_fiyat']].rename(columns={'toplam_tedarikci_stok': 'sup_stok', 'maliyet': 'sup_maliyet', 'marka': 'sup_marka', 'ted_hazir_fiyat':'sup_hazir_fiyat'}).drop_duplicates(subset=['match_code'])
            
            final = pd.merge(final, sup_lookup, on='match_code', how='left')
            final['toplam_tedarikci_stok'] = final['sup_stok'].fillna(0).astype(int)
            final['maliyet'] = final['sup_maliyet'].fillna(0)
            final['marka_ted'] = final['sup_marka'].fillna('TANIMSIZ')
            final['Ted_Hazir_Fiyat'] = final['sup_hazir_fiyat'].fillna(0)
            final.drop(columns=['sup_stok', 'sup_maliyet', 'sup_marka', 'sup_hazir_fiyat'], inplace=True)
        else: 
            final['toplam_tedarikci_stok'] = 0; final['maliyet'] = 0; final['marka_ted'] = 'TANIMSIZ'; final['Ted_Hazir_Fiyat'] = 0
        
        final['marka'] = final.get('marka', pd.Series()).fillna('TANIMSIZ')
        final['marka_ted'] = final.get('marka_ted', pd.Series()).fillna('TANIMSIZ')
        final['MP_Marka'] = final.get('MP_Marka', pd.Series()).fillna('TANIMSIZ')
        final['MP_Eski_Stok'] = final.get('MP_Eski_Stok', pd.Series()).fillna(0).astype(int)
        final['Ic_Hazir_Fiyat'] = final.get('ic_hazir_fiyat', pd.Series()).fillna(0).astype(float)
        final['Ted_Hazir_Fiyat'] = final.get('Ted_Hazir_Fiyat', pd.Series()).fillna(0).astype(float)

        def get_brand(r):
            if r['marka'] not in ['TANIMSIZ', 'YOK']: return r['marka']
            if r['marka_ted'] not in ['TANIMSIZ', 'YOK']: return r['marka_ted']
            return r['MP_Marka']
        final['Nihai_Marka'] = final.apply(get_brand, axis=1)
        
        # --- NLP KURALLARINI PARSE ET ---
        update_job_status(job_id, "running", 60, "Adım 4/5: Akıllı Fiyat Hesaplama ve Kur Analizi...")
        
        text_rules = price_strat.get('natural_language_text', '')
        nlp_rules = parse_natural_language_rules(text_rules)
        
        def apply_vat(price, strategy):
            if not strategy or not strategy.get('add_vat'): return price
            try:
                rate = decimal.Decimal(str(strategy.get('vat_rate', 20)))
                return price * (1 + (rate / 100))
            except: return price

        def calc_p(r):
            curr = decimal.Decimal(str(r.get('MP_Fiyat', 0)))
            br = str(r.get('Nihai_Marka','')).upper()
            prod_name = str(r.get('Urun_Adi','')).upper()
            sku = str(r.get('MP_SKU')); bk = str(r.get('MP_Barkod'))
            
            if freeze_conf and (sku in freeze_conf.get('skus',[]) or bk in freeze_conf.get('barcodes',[])): 
                return curr, "Manuel Dondurma"
            
            method = price_strat.get('method', 'calculated')
            base_price = decimal.Decimal(0)
            note = ""
            
            if method == 'stock_only':
                base_price = curr
                note = "Pazaryeri Fiyatı"
            else:
                source = price_strat.get('source', 'cost')
                if source == 'internal': 
                    base_price = decimal.Decimal(str(r.get('Ic_Hazir_Fiyat', 0)))
                    note = "İç Liste"
                elif source == 'supplier': 
                    base_price = decimal.Decimal(str(r.get('Ted_Hazir_Fiyat', 0)))
                    note = "Ted. Liste"
                elif source == 'cost': 
                    base_price = decimal.Decimal(str(r.get('maliyet', 0)))
                    note = "Maliyet"
            
            if base_price <= 0 and method != 'stock_only' and source != 'cost':
                   return (curr, "Kaynak Fiyat Yok") if curr > 0 else (decimal.Decimal(0), "Fiyat Yok")

            candidate_price = decimal.Decimal(0)
            
            if method == 'stock_only':
                candidate_price = base_price
            elif method == 'ready_list':
                candidate_price = base_price
            else: # calculated
                if base_price > 0:
                    candidate_price = base_price * decimal.Decimal(str(price_strat.get('default_multiplier',1.5))) + decimal.Decimal(str(price_strat.get('default_addition',0)))
                else:
                    note = "Maliyet Yok"

            if candidate_price > 0 or any(rule['action'] == 'fix_price' for rule in nlp_rules):
                for rule in nlp_rules:
                    is_match = False
                    if rule['target'] == "ALL_PRODUCTS": is_match = True
                    elif rule['target'] in br: is_match = True
                    elif rule['target'] in prod_name: is_match = True
                    elif rule['target'] in sku: is_match = True 
                    
                    if not is_match: continue
                    
                    if rule['action'] == 'fx_conversion':
                        if rule['old_rate'] and rule['old_rate'] > 0:
                            rate_curr = rule['currency'] if rule['currency'] else 'USD'
                            new_rate = EXCHANGE_RATES.get(rate_curr, 1)
                            candidate_price = (candidate_price / rule['old_rate']) * new_rate
                            note += f" + Kur Farkı ({rate_curr})"

                    elif rule['action'] == 'fx_index':
                        rate_curr = rule['currency'] if rule['currency'] else 'USD'
                        rate = EXCHANGE_RATES.get(rate_curr, 1)
                        candidate_price = base_price * rate
                        note = f"Döviz Endeksli ({rate_curr})"
                        
                    elif rule['action'] == 'multiplier':
                        if rule['value'] > 1 or rule['value'] < 1: 
                            candidate_price = candidate_price * rule['value']
                        else: 
                            candidate_price = candidate_price + rule['value']
                        note += f" + NLP ({rule['target']})"

                    elif rule['action'] == 'fix_price':
                        p_val = rule['value']
                        if rule['currency'] and rule['currency'] != 'TRY':
                            rate = EXCHANGE_RATES.get(rule['currency'], 1)
                            p_val = p_val * rate
                        candidate_price = p_val
                        note = f"Sabit Fiyat ({rule['target']})"

            if candidate_price > 0:
                candidate_price = apply_vat(candidate_price, price_strat)

            if candidate_price <= 0:
                return (curr, "Fiyat Korundu") if curr > 0 else (decimal.Decimal(0), note)

            final_p = candidate_price.quantize(TWOPLACES)

            if smart_freeze and curr > 0:
                if final_p < curr:
                    return curr, "Donduruldu (Düşüş Engellendi)"
            
            if final_p == curr: return curr, "Değişim Yok"
            return final_p, note
        
        pres = final.apply(calc_p, axis=1, result_type='expand')
        final['Satis_Fiyati'] = pres[0]; final['Fiyat_Durumu'] = pres[1]
        
        def calc_s(r):
            try:
                i = int(float(str(r.get('nihai_stok',0) or 0)))
                s = int(float(str(r.get('toplam_tedarikci_stok',0) or 0)))
            except: i,s=0,0
            res = 0
            if stock_strat == 'internal': res = i
            elif stock_strat == 'supplier': res = s
            else: res = min(i,s)
            if orphan_strat == 'zero' and r['Eslestirme'] == 'Eşleşmedi': return 0
            return max(0, res)
            
        final['Gonderilecek_Stok'] = final.apply(calc_s, axis=1)
        
        def get_stat(r):
            if "Yeni" in r['Eslestirme']: return r['Eslestirme']
            if r['Eslestirme'] == 'Eşleşmedi': return 'Eşleşmedi'
            return r['Fiyat_Durumu']
        final['Durum'] = final.apply(get_stat, axis=1)
        
        orig_out = None
        if include_orig:
            orig_out = mp_df.copy()
            lookup = final.drop_duplicates(subset=['MP_SKU']).set_index('MP_SKU')[['Satis_Fiyati', 'Gonderilecek_Stok']]
            orig_out['__sku'] = orig_out[s_sku].astype(str).str.strip()
            orig_out[s_prc] = orig_out['__sku'].map(lookup['Satis_Fiyati']).fillna(orig_out[s_prc]).apply(lambda x: float(x) if isinstance(x, decimal.Decimal) else x)
            orig_out[s_stk] = orig_out['__sku'].map(lookup['Gonderilecek_Stok']).fillna(orig_out[s_stk])
            orig_out.drop(columns=['__sku'], inplace=True)
        
        final.rename(columns={'MP_Barkod':'Barkod', 'MP_SKU':'SKU', 'MP_Urun_Adi':'Urun_Adi', 'MP_Fiyat':'Eski_Fiyat', 'MP_Eski_Stok': 'Eski_Stok', 'anahtar_kod':'Kaynak_Kod', 'nihai_stok':'Ic_Stok', 'toplam_tedarikci_stok':'Ted_Stok', 'maliyet':'Maliyet'}, inplace=True)
        
        cols_to_drop = ['tokens', 'idx', 'name_len', 'bk_norm', 'sku_norm', 'sup_stok', 'sup_maliyet', 'sup_marka', 'norm_name', 'ic_hazir_fiyat', 'Ted_Hazir_Fiyat', 'match_code']
        final_clean = final.drop(columns=[c for c in cols_to_drop if c in final.columns], errors='ignore')

        for col in ['Satis_Fiyati', 'Eski_Fiyat', 'Maliyet']:
            if col in final_clean.columns:
                final_clean[col] = final_clean[col].apply(lambda x: float(x) if isinstance(x, decimal.Decimal) else x)

        matched_mp_only = final_clean[final_clean['Kaynak_Kod'] != 'YOK']
        unmatched_mp_only = final_clean[final_clean['Kaynak_Kod'] == 'YOK']
        processed_skus = set(matched_mp_only['Kaynak_Kod'])
        missing_in_mp = internal_df[~internal_df['anahtar_kod'].isin(processed_skus)].copy()
        missing_in_mp.rename(columns={'anahtar_kod':'SKU', 'ic_urun_adi':'Urun_Adi', 'marka':'Marka', 'hesaplanan_stok':'Stok'}, inplace=True)

        def sort_key(row):
            match_type = str(row.get('Eslestirme', ''))
            score = row.get('Algoritma_Skoru', 0)
            if 'Barkod' in match_type: return (0, 100)
            if 'SKU' in match_type: return (1, 100)
            return (2, -score)

        matched_mp_only['SortKey'] = matched_mp_only.apply(sort_key, axis=1)
        matched_mp_only = matched_mp_only.sort_values(by=['SortKey']).drop(columns=['SortKey'])
        
        update_job_status(job_id, "running", 95, "Adım 5/5: Excel Raporu Yazılıyor...")
        
        out_file = TEMP_RESULTS_DIR / f"{job_id}.xlsx"
        with pd.ExcelWriter(out_file, engine='openpyxl') as writer:
            summary_data = []
            
            summary_data.append({'Kategori': '!!! YASAL UYARI !!!', 'Açıklama': 'SORUMLULUK REDDİ', 'Değer': 'Bu yazılım karar destek amaçlıdır. Stokçu, fiyat ve stok güncellemelerinde %100 doğruluk garantisi vermez. Lütfen yükleme yapmadan önce verileri kontrol ediniz.'})
            summary_data.append({'Kategori': 'BİLGİLENDİRME', 'Açıklama': 'Doğruluk Payı', 'Değer': 'Rapordaki "Algoritma Skoru" (0-100) eşleşme güvenini temsil eder. Düşük puanlı ürünleri manuel kontrol ediniz.'})
            summary_data.append({'Kategori': ' ', 'Açıklama': ' ', 'Değer': ' '})

            summary_data.append({'Kategori': 'İSTATİSTİK', 'Açıklama': 'Yüklenen Pazaryeri Listesi (Adet)', 'Değer': len(mp_df)})
            summary_data.append({'Kategori': 'İSTATİSTİK', 'Açıklama': 'BAŞARILI EŞLEŞME (Yeşil Sayfa)', 'Değer': len(matched_mp_only)})
            summary_data.append({'Kategori': 'İSTATİSTİK', 'Açıklama': 'EŞLEŞMEYEN (Kırmızı Sayfa)', 'Değer': len(unmatched_mp_only)})
            summary_data.append({'Kategori': 'İSTATİSTİK', 'Açıklama': 'Bizde Olup MP\'de Olmayanlar', 'Değer': len(missing_in_mp)})
            
            summary_data.append({'Kategori': ' ', 'Açıklama': ' ', 'Değer': ' '}) 
            summary_data.append({'Kategori': 'SÖZLÜK', 'Açıklama': 'MP_ (Prefix)', 'Değer': 'Pazaryeri (Marketplace) dosyasından gelen orijinal veriler.'})
            summary_data.append({'Kategori': 'SÖZLÜK', 'Açıklama': 'Ic_ (Prefix)', 'Değer': 'Sizin yüklediğiniz İç Stok (Depo) verileri.'})
            summary_data.append({'Kategori': 'SÖZLÜK', 'Açıklama': 'Ted_ (Prefix)', 'Değer': 'Tedarikçi listelerinden gelen veriler.'})
            summary_data.append({'Kategori': 'SÖZLÜK', 'Açıklama': 'Satis_Fiyati', 'Değer': 'Hesaplanan yeni satış fiyatı.'})
            summary_data.append({'Kategori': 'SÖZLÜK', 'Açıklama': 'Gonderilecek_Stok', 'Değer': 'Pazaryerine gönderilecek nihai stok miktarı.'})
            summary_data.append({'Kategori': 'SÖZLÜK', 'Açıklama': 'Algoritma Skoru', 'Değer': 'Ürün isim ve özellik benzerlik oranı (100 = Tam Eşleşme).'})

            pd.DataFrame(summary_data).to_excel(writer, sheet_name='1. Genel Özet', index=False)
            matched_mp_only.to_excel(writer, sheet_name='2. Eşleşenler (Yeşil)', index=False)
            unmatched_mp_only.to_excel(writer, sheet_name='3. Eşleşmeyenler (Kırmızı)', index=False)
            missing_in_mp.to_excel(writer, sheet_name='4. Bizde Var MP Yok', index=False)
            mp_df.to_excel(writer, sheet_name='5. Pazaryeri Ham', index=False)
            internal_df.drop(columns=['bk_norm', 'sku_norm', 'norm_name', 'match_code'], errors='ignore').to_excel(writer, sheet_name='6. İç Stok Ham', index=False)
            if not supplier_df.empty: supplier_df.drop(columns=['bk_norm', 'sku_norm', 'match_code'], errors='ignore').to_excel(writer, sheet_name='7. Tedarikçi Ham', index=False)
            if orig_out is not None: orig_out.to_excel(writer, sheet_name='OPSİYONEL - Yükleme Formatı', index=False)
        
        update_job_status(job_id, "completed", 100, "Tamamlandı.", result_file=f"{job_id}.xlsx")
        
    except Exception as e:
        traceback.print_exc()
        update_job_status(job_id, "error", 0, "Hata oluştu", error=str(e))

@app.route('/api/v1/calculate_stock', methods=['POST'])
def api_calculate_stock():
    try:
        uploaded_files = request.files.getlist('files')
        template_names = request.form.get('template_names', '').split(',')
        labels = request.form.get('labels', '').split(',')
        
        thr = None
        amt = decimal.Decimal(0)
        if request.form.get('security_threshold'):
            thr = int(request.form.get('security_threshold'))
            amt = decimal.Decimal(request.form.get('security_amount', 0))

        processed_files = []
        for i, f in enumerate(uploaded_files):
            t_path = tempfile.NamedTemporaryFile(delete=False, suffix=Path(f.filename).suffix).name
            f.save(t_path)
            
            tpl_name = template_names[i] if i < len(template_names) else ""
            tpl = load_template(tpl_name)
            
            df = read_and_normalize_file(t_path, f.filename)
            os.remove(t_path)
            
            label = labels[i] if i < len(labels) else "+"
            processed_files.append({
                'dataframe': df,
                'template': tpl,
                'label': label,
                'filename': f.filename
            })

        result_df, meta = calculate_internal_stock(processed_files, thr, amt)
        
        key = str(uuid.uuid4())
        result_df.to_json(TEMP_RESULTS_DIR / f"internal_{key}.json")
        with open(TEMP_RESULTS_DIR / f"meta_internal_{key}.json", 'w') as f:
            json.dump(meta, f)
            
        return jsonify({"result_key": key})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"hata": str(e)}), 500

@app.route('/api/v1/consolidate_suppliers', methods=['POST'])
def api_consolidate_suppliers():
    try:
        uploaded_files = request.files.getlist('files')
        template_names = request.form.get('template_names', '').split(',')
        
        processed_files = []
        for i, f in enumerate(uploaded_files):
            t_path = tempfile.NamedTemporaryFile(delete=False, suffix=Path(f.filename).suffix).name
            f.save(t_path)
            
            tpl_name = template_names[i] if i < len(template_names) else ""
            tpl = load_template(tpl_name)
            
            df = read_and_normalize_file(t_path, f.filename)
            os.remove(t_path)
            
            processed_files.append({
                'dataframe': df,
                'template': tpl,
                'filename': f.filename
            })
            
        result_df, meta = consolidate_suppliers(processed_files)
        
        key = str(uuid.uuid4())
        result_df.to_json(TEMP_RESULTS_DIR / f"supplier_{key}.json")
        
        return jsonify({"result_key": key})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"hata": str(e)}), 500

@app.route('/api/v1/process_marketplace', methods=['POST'])
def step3_async():
    try:
        job_id = str(uuid.uuid4())
        price_strat_raw = request.form.get('price_strategy_json', '{}')
        price_strat = json.loads(price_strat_raw)
        if price_strat is None: price_strat = {}
        
        nlp_text = request.form.get('price_rules_text', '')
        if nlp_text: price_strat['natural_language_text'] = nlp_text
        
        source = request.form.get('price_source_selection')
        if source == 'stock_only':
            price_strat['method'] = 'stock_only'
            price_strat['source'] = 'none'
        elif source == 'calculated':
            price_strat['method'] = 'calculated'
            price_strat['source'] = 'cost'
        elif source in ['supplier', 'internal', 'cost']:
            price_strat['method'] = 'ready_list'
            price_strat['source'] = source
        
        add_vat_param = request.form.get('add_vat')
        if add_vat_param == 'true':
            price_strat['add_vat'] = True
            price_strat['vat_rate'] = request.form.get('vat_rate', 20)
        else:
            price_strat['add_vat'] = False

        args = (
            job_id,
            request.form.get('internal_stock_key'),
            request.form.get('supplier_stock_key'),
            "", 
            request.files.get('marketplace_file').filename,
            request.form.get('template_name'),
            request.form.get('stock_strategy'),
            price_strat,
            request.form.get('orphan_strategy'),
            request.form.get('smart_freeze') == 'true',
            json.loads(request.form.get('freeze_config_json', '{}')),
            request.form.get('brand_extraction_strategy'),
            request.form.get('include_original_format') == 'true'
        )
        mp = request.files.get('marketplace_file')
        t_path = tempfile.NamedTemporaryFile(delete=False, suffix=Path(mp.filename).suffix).name
        mp.save(t_path)
        args_list = list(args)
        args_list[3] = t_path
        thread = threading.Thread(target=run_matching_job, args=tuple(args_list))
        thread.start()
        return jsonify({"job_id": job_id})
    except Exception as e:
        return jsonify({"hata": str(e)}), 500

@app.route('/api/v1/jobs/<job_id>', methods=['GET'])
def get_job_status(job_id):
    try:
        p = JOBS_DIR / f"{job_id}.json"
        if not p.exists(): return jsonify({"status": "not_found"}), 404
        with open(p, 'r') as f: return jsonify(json.load(f))
    except: return jsonify({"status": "error"}), 500

@app.route('/api/v1/download/<job_id>', methods=['GET'])
def download_result(job_id):
    p = TEMP_RESULTS_DIR / f"{job_id}.xlsx"
    if p.exists():
        return send_file(p, download_name=f"Stokcu_Raporu_{datetime.now().strftime('%H%M')}.xlsx", as_attachment=True)
    return "Dosya yok", 404

@app.route('/api/v1/download_template/freeze', methods=['GET'])
def get_freeze_template():
    try:
        df = pd.DataFrame({'Barkod': ['8690000000000'], 'SKU': ['ORNEK-KOD-123']})
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
        output.seek(0)
        return send_file(output, download_name='ornek_fiyat_dondurma_sablonu.xlsx', as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        return jsonify({"hata": str(e)}), 500

@app.route('/<path:path>')
def static_proxy(path):
    if not (STATIC_DIR / path).exists(): return send_from_directory(STATIC_DIR, 'index.html')
    return send_from_directory(STATIC_DIR, path)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
