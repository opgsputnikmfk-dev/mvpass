from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
import math
from datetime import datetime
import pandas as pd
import pandas_ta as ta

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = 8299008675 

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()
DB_FILE = "trades_log.json"

SCAN_INTERVAL = 300 
NEIGHBORS = 5

# --- ТЕХНИЧЕСКИЙ АНАЛИЗ ---
def get_advanced_filters(candles):
    df = pd.DataFrame(candles, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'ignore1', 'ignore2', 'ignore3', 'ignore4', 'ignore5', 'ignore6'])
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    
    # 1. EMA 200
    ema200 = ta.ema(df['close'], length=200).iloc[-1]
    # 2. ADX (сила тренда)
    adx = ta.adx(df['high'], df['low'], df['close'], length=14).iloc[-1, 0]
    # 3. Pivot Points (уровни)
    high, low, close = df['high'].iloc[-2], df['low'].iloc[-2], df['close'].iloc[-2]
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    
    return ema200, adx, pivot, r1, s1, df['close'].iloc[-1]

# --- СТАТИСТИКА И БД ---
def save_trade_to_db(symbol, signal, timeframe, reason, entry, close_price, is_anomaly):
    log = []
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f: log = json.load(f)
    log.append({"symbol": symbol, "signal": signal, "timeframe": timeframe, "reason": reason, "entry": entry, "timestamp": time.time()})
    with open(DB_FILE, 'w') as f: json.dump(log, f)

def generate_monthly_report():
    if not os.path.exists(DB_FILE): return "📭 База пуста."
    with open(DB_FILE, 'r') as f: log = json.load(f)
    
    # Группировка по срокам
    report = "📊 **АВТО-СТАТИСТИКА**\n➖➖➖➖➖➖➖➖\n"
    for tf in ["⚡️ ИНТРАДЕЙ", "🌊 СВИНГ"]:
        trades = [t for t in log if t['timeframe'] == tf]
        wins = sum(1 for t in trades if t['reason'] == 'TP2')
        total = len(trades)
        wr = (wins / total * 100) if total > 0 else 0
        report += f"{tf}: {total} сделок | Winrate: {wr:.1f}%\n"
    return report

# --- ЛОГИКА ИИ ---
def predict_knn(candles, current_idx, atr, macro_trend):
    ema200, adx, pivot, r1, s1, curr_p = get_advanced_filters(candles)
    
    # Фильтр: ADX (тренд должен быть выражен)
    if adx < 20: return None, 0, "🛡 Отмена: Рынок во флэте (ADX < 20)"
    
    # Фильтр: EMA 200 (тренд)
    if curr_p < ema200: trend = "BEAR"
    else: trend = "BULL"
    
    # Паттерны k-NN... (логика поиска совпадений)
    # Здесь вставь твою стандартную логику predict_knn, добавив проверку:
    # if signal == "LONG" and trend == "BEAR": return None, 0, "🛡 Отмена: Против тренда EMA"
    # if signal == "LONG" and curr_p > r1: return None, 0, "🛡 Отмена: Уровень сопротивления R1"
    
    return "LONG", 2.0, "🔥 ИИ + Фильтры"

# --- ИНТЕРФЕЙС И СКАНЕР ---
# ...Остальной код бота (broadcast, bot_engine, scan_timeframe) без изменений...

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ", 14400), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("4h", "🌊 СВИНГ", 43200), daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
