from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
from datetime import datetime
import pandas as pd
import numpy as np
from google import genai

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_IDS = {8299008675, 7639836087}

# Инициализация Gemini по новому стандарту
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()
SCAN_INTERVAL = 60

# --- ПОЛУЧЕНИЕ ДАННЫХ ---
def get_data(symbol, interval, limit=1000):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=15) as r: 
            return json.loads(r.read().decode())
    except: return []

# --- ИИ-ОРАКУЛ (Обновленный API) ---
def ask_ai_oracle(symbol, signal, current_price, rsi, adx, recent_closes):
    try:
        prompt = f"Риск-менеджер. Анализ {signal} по {symbol}. Цена: {current_price}. RSI: {rsi:.1f}. ADX: {adx:.1f}. Цены: {recent_closes[-5:]}. Безопасен вход? 'APPROVE' или 'REJECT'. Одно слово."
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt,
        )
        return "APPROVE" in response.text.strip().upper()
    except Exception as e:
        print(f"AI Oracle Error: {e}")
        return True

# --- АНАЛИТИКА ---
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

# --- ЛОГИКА ---
def predict_knn(candles, symbol, interval, current_idx, atr, macro_trend):
    ema200, adx, rsi, is_anomaly, is_vol_climax, upper_3sigma, lower_3sigma, curr_p, closes_list = get_advanced_filters(candles, current_idx)
    
    if is_anomaly or is_vol_climax: return None, 0, "", "🛡 Аномалия/Кульминация"
    
    signal = None
    if curr_p > ema200 and rsi < 70 and curr_p < upper_3sigma: signal = "LONG"
    elif curr_p < ema200 and rsi > 30 and curr_p > lower_3sigma: signal = "SHORT"

    if signal:
        if interval != "15m" and not ask_ai_oracle(symbol, signal, curr_p, rsi, adx, closes_list):
            return None, 0, "", "🛡 Отклонено ИИ-Оракулом"
        return signal, 2.0, "СРЕДНЯЯ", "✅ ОК"
    return None, 0, "", ""

# --- TELEGRAM СКАНЕР ---
def send_msg(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try: urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}))
    except: pass

def scan_timeframe(interval_name, label_name):
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

@app.route('/')
def home(): return "AI-Hybrid Bot Active (New GenAI API)"

if __name__ == "__main__":
    threading.Thread(target=scan_timeframe, args=("15m", "⏱ СКАЛЬПИНГ")).start()
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ")).start()
    threading.Thread(target=scan_timeframe, args=("4h", "🌊 СВИНГ")).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
