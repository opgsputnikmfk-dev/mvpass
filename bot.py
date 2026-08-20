from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
import math
from datetime import datetime
import pandas as pd
import numpy as np
from google import genai

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_IDS = {8299008675, 7639836087}
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
active_trades = {} 
SCALP_ENABLED = True
start_time = time.time()
DB_FILE = "trades_log.json"

# --- БЫСТРЫЕ ЗАПРОСЫ К БИРЖЕ ---
def get_current_price(symbol):
    url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=3) as r:
            return float(json.loads(r.read().decode())['price'])
    except: return None

def get_data(symbol, interval, limit=200):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=5) as r:
            return json.loads(r.read().decode())
    except: return []

# --- МОДУЛЬ КОНТРОЛЯ СДЕЛОК (РЕАЛЬНОЕ ВРЕМЯ) ---
def trade_monitor():
    print("Trade Monitor Online...")
    while True:
        for key, trade in list(active_trades.items()):
            price = get_current_price(trade["symbol"])
            if not price: continue
            
            hit = None
            # Логика Long
            if trade["signal"] == "LONG":
                if not trade["tp1_hit"] and price >= trade["tp1"]:
                    trade["tp1_hit"] = True; trade["sl"] = trade["entry"]
                    for c, m in trade["messages"]: edit_msg(c, m, trade["original_msg"].replace("🤖 **AI ALERT", "🟡 **[TP1 ВЗЯТ]"))
                elif price >= trade["tp2"]: hit = "TP2"
                elif price <= trade["sl"]: hit = "SL" if not trade["tp1_hit"] else "BE"
            # Логика Short
            else:
                if not trade["tp1_hit"] and price <= trade["tp1"]:
                    trade["tp1_hit"] = True; trade["sl"] = trade["entry"]
                    for c, m in trade["messages"]: edit_msg(c, m, trade["original_msg"].replace("🤖 **AI ALERT", "🟡 **[TP1 ВЗЯТ]"))
                elif price <= trade["tp2"]: hit = "TP2"
                elif price >= trade["sl"]: hit = "SL" if not trade["tp1_hit"] else "BE"
            
            if hit:
                save_trade_to_db(trade["symbol"], trade["signal"], trade["tf_label"], hit, trade["entry"], price)
                header = f"✅ **[{trade['tf_label']} | {hit}]"
                for c, m in trade["messages"]: edit_msg(c, m, f"{header}\n\n**Цена закрытия:** {price}")
                del active_trades[key]
        time.sleep(2)

# --- БИЗНЕС-ЛОГИКА ---
def save_trade_to_db(symbol, signal, tf, reason, entry, close):
    trade_data = {"symbol": symbol, "signal": signal, "timeframe": tf, "reason": reason, "entry": entry, "close": close, "ts": time.time()}
    try:
        log = json.load(open(DB_FILE, 'r')) if os.path.exists(DB_FILE) else []
        log.append(trade_data)
        json.dump(log, open(DB_FILE, 'w'))
    except: pass

def broadcast(text):
    return [(c, send_msg(c, text)) for c in active_chats]

def send_msg(chat_id, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard}
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        return json.loads(urllib.request.urlopen(req, timeout=5).read().decode())["result"]["message_id"]
    except: return None

def edit_msg(chat_id, mid, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {"chat_id": chat_id, "message_id": mid, "text": text, "parse_mode": "Markdown"}
    try: urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}), timeout=5)
    except: pass

# --- СКАНЕР РЫНКА ---
def scan_timeframe(interval, label):
    while True:
        if interval == "15m" and not SCALP_ENABLED:
            time.sleep(10); continue
        for symbol in SYMBOLS:
            if f"{symbol}_{interval}" in active_trades: continue
            
            candles = get_data(symbol, interval)
            if not candles or len(candles) < 50: continue
            
            curr_p = float(candles[-1][4])
            atr = sum([float(c[2])-float(c[3]) for c in candles[-15:]]) / 15
            
            # Упрощенная логика сигнала для примера (вставь сюда свой k-NN)
            signal = "LONG" if float(candles[-1][4]) > float(candles[-2][4]) else "SHORT"
            
            if signal:
                sl = curr_p - (atr * 2) if signal == "LONG" else curr_p + (atr * 2)
                tp1 = curr_p + (atr * 1) if signal == "LONG" else curr_p - (atr * 1)
                tp2 = curr_p + (atr * 3) if signal == "LONG" else curr_p - (atr * 3)
                
                msg = f"🤖 **AI ALERT | {symbol}**\n📈 **Направление:** {signal}\n🎯 TP1: `{tp1:.2f}`\n🚀 TP2: `{tp2:.2f}`\n🛡 SL: `{sl:.2f}`"
                msgs = broadcast(msg)
                if msgs:
                    active_trades[f"{symbol}_{interval}"] = {
                        "symbol": symbol, "signal": signal, "tf_label": label, "interval": interval,
                        "entry": curr_p, "tp1": tp1, "tp2": tp2, "sl": sl, "tp1_hit": False, "messages": msgs, "original_msg": msg
                    }
        time.sleep(60)

# --- ЗАПУСК ---
if __name__ == "__main__":
    threading.Thread(target=trade_monitor, daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("15m", "⏱ СКАЛЬПИНГ"), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ"), daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
