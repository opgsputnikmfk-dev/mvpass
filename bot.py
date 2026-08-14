from google import genai
from google.genai import types
from flask import Flask
import urllib.request
import urllib.parse
import json
import ssl
import time
import math
import threading
import os

# --- КЛЮЧИ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
context = ssl._create_unverified_context()

app = Flask(__name__)
@app.route('/')
def home(): return "Quant Bot VSA Active"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- НАСТРОЙКИ СТРАТЕГИИ ---
INTERVAL = "15m"
HISTORY_LIMIT = 300
SCAN_INTERVAL = 900
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", 
           "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]

subscribed_users = set()

def get_market_data(symbol, interval, limit):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(url, context=context, timeout=10) as response:
            return json.loads(response.read())
    except: return []

def calculate_bollinger_bands(prices, period=20, num_std=2.5):
    sma = sum(prices[-period:]) / period
    variance = sum((x - sma) ** 2 for x in prices[-period:]) / period
    stdev = math.sqrt(variance)
    return sma + (num_std * stdev), sma, sma - (num_std * stdev)

# --- НОВАЯ СТРАТЕГИЯ VSA (Объемная кульминация) ---
def evaluate_quant_strategy(candles):
    if len(candles) < 50: return None
    prices = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    
    current_price = prices[-1]
    avg_vol = sum(volumes[-20:-1]) / 20
    
    upper, middle, lower = calculate_bollinger_bands(prices, 20, 2.5)
    
    # Вход: Аномалия + Всплеск объема (кульминация)
    if volumes[-1] > (avg_vol * 1.8):
        if current_price < lower: return "LONG"
        if current_price > upper: return "SHORT"
    return None

def get_quant_stats(symbol):
    candles = get_market_data(symbol, "15m", 500)
    stats = {"t": 0, "w": 0}
    if not candles: return stats
    
    for i in range(40, len(candles) - 5):
        sig = evaluate_quant_strategy(candles[:i+1])
        if sig:
            stats["t"] += 1
            # Логика: если цена за 4 свечи (60 мин) прошла в сторону сигнала - успех
            p_in = float(candles[i][4])
            p_out = float(candles[i+4][4])
            if (sig == "LONG" and p_out > p_in) or (sig == "SHORT" and p_out < p_in):
                stats["w"] += 1
    return stats

def send_telegram_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}"
        urllib.request.urlopen(url, context=context, timeout=10)
    except: pass

def auto_scanner():
    while True:
        for symbol in SYMBOLS:
            candles = get_market_data(symbol, INTERVAL, HISTORY_LIMIT)
            if not candles: continue
            sig = evaluate_quant_strategy(candles)
            if sig:
                msg = f"🎯 VSA СИГНАЛ: {symbol.replace('USDT', '')} | {sig} | Вход: {candles[-1][4]}"
                for chat_id in subscribed_users: send_telegram_message(chat_id, msg)
                time.sleep(60) # Пауза чтобы не спамить
        time.sleep(SCAN_INTERVAL)

def main():
    threading.Thread(target=auto_scanner, daemon=True).start()
    threading.Thread(target=run_web, daemon=True).start()
    # (Здесь должна быть логика опроса Telegram как в предыдущем коде)
    # Я для краткости сократил, вставь свою часть main из предыдущего кода
    print("🤖 Система VSA активна")

if __name__ == "__main__":
    main()
