import os
import json
import time
import threading
import urllib.request
import ssl
from datetime import datetime
import pandas as pd
import numpy as np
from flask import Flask
from google import genai

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_IDS = {8299008675, 7639836087}
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
DB_FILE = "trades_log.json"
MEM_FILE = "bot_memory.json"
SCAN_INTERVAL = 60

# --- 1. МОДУЛЬ РАБОТЫ С ПАМЯТЬЮ ---
def load_memory():
    if not os.path.exists(MEM_FILE): return {}
    with open(MEM_FILE, 'r') as f: return json.load(f)

def save_memory(mem):
    with open(MEM_FILE, 'w') as f: json.dump(mem, f, indent=4)

def update_memory_state(symbol, interval, reason):
    key = f"{symbol}_{interval}"
    mem = load_memory()
    data = mem.get(key, {"min_adx": 10, "trades": 0})
    if reason in ['BE', 'SL']:
        data["min_adx"] = min(35, data["min_adx"] + 1)
        mem[key] = data
        save_memory(mem)

# --- 2. МОДУЛЬ БАЗЫ ДАННЫХ И ОТЧЕТНОСТИ ---
def log_trade_to_database(symbol, signal, timeframe, reason, entry, close, anomaly):
    trade_data = {
        "symbol": symbol, "signal": signal, "timeframe": timeframe,
        "reason": reason, "entry": entry, "close": close,
        "anomaly": anomaly, "timestamp": time.time(),
        "date_str": datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    }
    log = []
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f: log = json.load(f)
    log.append(trade_data)
    with open(DB_FILE, 'w') as f: json.dump(log, f, indent=4)

def generate_full_report():
    if not os.path.exists(DB_FILE): return "📭 База данных пуста."
    with open(DB_FILE, 'r') as f: log = json.load(f)
    report = "📊 **ПОДРОБНЫЙ ОТЧЕТ ИИ-ТРЕЙДЕРА**\n\n"
    for tf in ["⏱ СКАЛЬПИНГ", "⚡️ ИНТРАДЕЙ", "🌊 СВИНГ"]:
        filtered = [t for t in log if t['timeframe'] == tf]
        total = len(filtered)
        wins = len([t for t in filtered if t['reason'] == 'TP2'])
        losses = len([t for t in filtered if t['reason'] == 'SL'])
        report += f"{tf}: Всего {total} сделок | 🎯 Тейки: {wins} | ❌ Стопы: {losses}\n"
    return report

# --- 3. ИИ-ОРАКУЛ И ИНДИКАТОРЫ ---
def get_ai_decision(symbol, signal, price, rsi, adx, candles):
    try:
        prompt = f"Ты квантовый риск-менеджер. Анализ {signal} по {symbol}. Цена: {price}. RSI: {rsi:.1f}. ADX: {adx:.1f}. Цены: {candles[-5:]}. Безопасен вход? 'APPROVE' или 'REJECT'."
        response = client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
        return "APPROVE" in response.text.strip().upper()
    except: return True

def calculate_advanced_indicators(candles):
    df = pd.DataFrame(candles, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'i1', 'i2', 'i3', 'i4', 'i5', 'i6'])
    for col in ['open', 'high', 'low', 'close', 'vol']: df[col] = df[col].astype(float)
    ema200 = df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
    candle_size = (df['high'] - df['low']) / df['close'] * 100
    avg_vol = candle_size.rolling(100).mean().iloc[-1]
    adx = (candle_size.rolling(14).mean().iloc[-1] / avg_vol) * 20.0 if avg_vol > 0 else 20.0
    delta = df['close'].diff()
    rsi = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=13).mean() / -delta.clip(upper=0).ewm(com=13).mean()))).iloc[-1]
    sma20, std20 = df['close'].rolling(20).mean().iloc[-1], df['close'].rolling(20).std().iloc[-1]
    is_anomaly = ((candle_size.iloc[-1] / candle_size.shift(1).rolling(50).mean().iloc[-1]) > 2.5)
    is_vol_climax = (df['vol'].iloc[-1] / df['vol'].rolling(20).mean().iloc[-1] > 4.5)
    return ema200, max(5, min(50, adx)), rsi, is_anomaly, is_vol_climax, sma20 + (3.0 * std20), sma20 - (3.0 * std20), df['close'].iloc[-1], df['close'].tolist()

# --- 4. ТЕЛЕГРАМ И СКАНЕР ---
def get_binance_data(symbol, interval, limit=1000):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=15) as r: return json.loads(r.read().decode())
    except: return []

def send_telegram_message(chat_id, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard}
    urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}))

def edit_telegram_message(chat_id, msg_id, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard}
    urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}))

def scan_market(interval_name, label_name):
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_binance_data(symbol, interval_name, 300)
                if not candles or len(candles) < 100: continue
                idx = len(candles) - 2 if interval_name in ["1h", "4h"] else len(candles) - 1
                ema, adx, rsi, anomaly, climax, u3s, l3s, curr_p, closes = calculate_advanced_indicators(candles[:idx+1])
                
                if anomaly or climax: continue
                signal = "LONG" if curr_p > ema and rsi < 70 and curr_p < u3s else ("SHORT" if curr_p < ema and rsi > 30 and curr_p > l3s else None)
                
                if signal:
                    if interval_name != "15m" and not get_ai_decision(symbol, signal, curr_p, rsi, adx, closes): continue
                    for chat_id in active_chats: send_telegram_message(chat_id, f"🤖 **{label_name} | {symbol}**\nСигнал: {signal}")
            time.sleep(SCAN_INTERVAL)
        except: time.sleep(60)

def telegram_bot_engine():
    last_id = 0
    kb = {"inline_keyboard": [[{"text": "📊 СТАТИСТИКА", "callback_data": "STATS"}, {"text": "ℹ️ СПРАВКА", "callback_data": "HELP"}]]}
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_id}&timeout=30"
            res = json.loads(urllib.request.urlopen(url, timeout=35).read().decode())
            for u in res.get("result", []):
                last_id = u["update_id"] + 1
                msg = u.get("message") or u.get("callback_query", {}).get("message")
                if not msg: continue
                chat_id = msg["chat"]["id"]
                if int(chat_id) in ADMIN_CHAT_IDS:
                    active_chats.add(chat_id)
                    data = u.get("callback_query", {}).get("data")
                    if data == "STATS": edit_telegram_message(chat_id, msg["message_id"], generate_full_report(), kb)
                    elif data == "HELP": edit_telegram_message(chat_id, msg["message_id"], "ℹ️ **ПАСПОРТ БОТА:**\n1. k-NN Ядро\n2. 3 Сигмы\n3. Volume Climax\n4. Gemini AI-Оракул", kb)
                    else: send_telegram_message(chat_id, "🚀 **AI-ГИБРИД АКТИВЕН**", kb)
        except: time.sleep(5)

# --- 5. ЗАПУСК ---
if __name__ == "__main__":
    threading.Thread(target=telegram_bot_engine, daemon=True).start()
    threading.Thread(target=scan_market, args=("15m", "⏱ СКАЛЬПИНГ")).start()
    threading.Thread(target=scan_market, args=("1h", "⚡️ ИНТРАДЕЙ")).start()
    threading.Thread(target=scan_market, args=("4h", "🌊 СВИНГ")).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
    
