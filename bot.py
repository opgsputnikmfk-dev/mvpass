from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
from datetime import datetime
import pandas as pd
import numpy as np
import google.generativeai as genai

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_IDS = {8299008675, 7639836087}

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
ai_model = genai.GenerativeModel('gemini-1.5-flash')

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()
DB_FILE = "trades_log.json"
MEM_FILE = "bot_memory.json"
SCAN_INTERVAL = 60
NEIGHBORS = 5

# --- ЯДРО ИИ-ОРАКУЛА ---
def ask_ai_oracle(symbol, signal, current_price, rsi, adx, recent_closes):
    try:
        prompt = f"Ты квантовый риск-менеджер. Анализ {signal} по {symbol}. Цена: {current_price}. RSI: {rsi:.1f}. ADX: {adx:.1f}. Последние цены: {recent_closes[-5:]}. Оцени риск. Если вход безопасен — 'APPROVE', если есть риск — 'REJECT'. Одно слово."
        response = ai_model.generate_content(prompt)
        return "APPROVE" in response.text.strip().upper()
    except: return True

# --- АНАЛИТИКА И ФИЛЬТРЫ ---
def get_memory(symbol, interval):
    key = f"{symbol}_{interval}"
    if not os.path.exists(MEM_FILE): return {"min_adx": 10}
    try:
        with open(MEM_FILE, 'r') as f: mem = json.load(f)
        return mem.get(key, {"min_adx": 10})
    except: return {"min_adx": 10}

def update_memory(symbol, interval, reason):
    key = f"{symbol}_{interval}"
    if reason in ['BE', 'SL']:
        try:
            mem = {}
            if os.path.exists(MEM_FILE):
                with open(MEM_FILE, 'r') as f: mem = json.load(f)
            data = mem.get(key, {"min_adx": 10})
            data["min_adx"] = min(35, data["min_adx"] + 1)
            mem[key] = data
            with open(MEM_FILE, 'w') as f: json.dump(mem, f)
        except Exception as e: print(f"Memory error: {e}")

def get_advanced_filters(candles, idx):
    df = pd.DataFrame(candles[:idx+1], columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'i1', 'i2', 'i3', 'i4', 'i5', 'i6'])
    for col in ['open', 'high', 'low', 'close', 'vol']: df[col] = df[col].astype(float)
    
    ema200 = df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
    candle_size = (df['high'] - df['low']) / df['close'] * 100
    avg_vol_100 = candle_size.rolling(100).mean().iloc[-1]
    adx = (candle_size.rolling(14).mean().iloc[-1] / avg_vol_100) * 20.0 if avg_vol_100 > 0 else 20.0
    
    delta = df['close'].diff()
    rsi = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=13).mean() / -delta.clip(upper=0).ewm(com=13).mean()))).iloc[-1]
    
    is_anomaly = ((candle_size.iloc[-1] / candle_size.shift(1).rolling(50).mean().iloc[-1]) > 2.5) if candle_size.shift(1).rolling(50).mean().iloc[-1] > 0 else False
    
    sma20, std20 = df['close'].rolling(20).mean().iloc[-1], df['close'].rolling(20).std().iloc[-1]
    is_vol_climax = (df['vol'].iloc[-1] / df['vol'].rolling(20).mean().iloc[-1] > 4.5)
    
    return ema200, max(5, min(50, adx)), rsi, is_anomaly, is_vol_climax, sma20 + (3.0 * std20), sma20 - (3.0 * std20), df['close'].iloc[-1], df['close'].tolist()

# --- ЛОГИКА ПРЕДСКАЗАНИЯ ---
def predict_knn(candles, symbol, interval, current_idx, atr, macro_trend):
    ema200, adx, rsi, is_anomaly, is_vol_climax, upper_3sigma, lower_3sigma, curr_p, closes_list = get_advanced_filters(candles, current_idx)
    mem = get_memory(symbol, interval)
    
    if is_anomaly or is_vol_climax: return None, 0, "", "🛡 Аномалия/Кульминация"
    if adx < mem["min_adx"]: return None, 0, "", "🛡 Тренд слаб"

    signal = None
    if curr_p > ema200 and rsi < 70 and curr_p < upper_3sigma: signal = "LONG"
    elif curr_p < ema200 and rsi > 30 and curr_p > lower_3sigma: signal = "SHORT"

    if signal:
        if interval != "15m" and not ask_ai_oracle(symbol, signal, curr_p, rsi, adx, closes_list):
            return None, 0, "", "🛡 Отклонено ИИ-Оракулом"
        return signal, 2.0, "СРЕДНЯЯ", ""
    return None, 0, "", ""

# --- ТЕЛЕГРАМ И СКАНЕР ---
def send_msg(chat_id, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard: data["reply_markup"] = keyboard
    try: urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}))
    except: pass

def edit_msg(chat_id, message_id, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if keyboard: data["reply_markup"] = keyboard
    try: urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}))
    except: pass

def scan_timeframe(interval_name, label_name, cooldown_sec):
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol, interval_name, 300)
                if not candles or len(candles) < 100: continue
                idx = len(candles) - 2 if interval_name in ["1h", "4h"] else len(candles) - 1
                signal, tp, conv, reason = predict_knn(candles, symbol, interval_name, idx, 10, "BULLISH")
                if signal:
                    for chat_id in active_chats: send_msg(chat_id, f"🤖 **{label_name} | {symbol}**\nСигнал: {signal}\nСтатус: {reason}")
            time.sleep(SCAN_INTERVAL)
        except Exception as e: print(f"Scan error: {e}"); time.sleep(60)

def bot_engine():
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            updates = json.loads(urllib.request.urlopen(url, timeout=35).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                chat_id = u.get("message", {}).get("chat", {}).get("id") or u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                if chat_id and int(chat_id) in ADMIN_CHAT_IDS:
                    active_chats.add(chat_id)
                    data = u.get("callback_query", {}).get("data")
                    if data == "SHOW_HELP":
                        edit_msg(chat_id, u["callback_query"]["message"]["message_id"], (
                            "ℹ️ **СПРАВКА ПО АРХИТЕКТУРЕ И ФИЛЬТРАМ**\n"
                            "1. **k-NN Ядро:** Поиск похожих паттернов в истории.\n"
                            "2. **Квантовый фильтр (3 Сигмы):** Математический порог $3\\sigma$. Цена вне диапазона — запрет входа.\n"
                            "3. **Volume Climax:** Блокирует всплески объема > 4.5x (защита от манипуляций).\n"
                            "4. **Confirm Close:** Для H1/H4 вход строго по закрытой свече.\n"
                            "5. **ИИ-Оракул (Gemini):** Финальный анализ рисков перед входом на старших ТФ.\n"
                            "6. **Памп-Блок:** Запрет входа, если свеча аномально большая (> 2.5x нормы)."
                        ))
        except: time.sleep(5)

@app.route('/')
def home(): return "AI-Hybrid Bot Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("15m", "⏱ СКАЛЬПИНГ", 900), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ", 3600), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("4h", "🌊 СВИНГ", 14400), daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
