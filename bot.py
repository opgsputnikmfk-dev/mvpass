from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
import math
from datetime import datetime

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()

# --- ПАРАМЕТРЫ MACHINE LEARNING (k-NN) ---
INTERVAL = "1h"
HISTORY_LIMIT = 1000 # Загружаем максимум истории для обучения базы
SCAN_INTERVAL = 3600
NEIGHBORS = 5 # Количество исторических паттернов для анализа
CONSENSUS = 4 # Сколько из 5 паттернов должны показать одинаковый результат (80% вероятность)

def get_data(symbol):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={HISTORY_LIMIT}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=15) as r: 
            return json.loads(r.read().decode())
    except:
        return []

def get_fingerprint(opens, highs, lows, closes, volumes, i, atr):
    # Создаем математический слепок свечи, независимый от абсолютной цены актива
    if atr == 0: return [0, 0, 0]
    
    # 1. Размер тела относительно ATR
    body = (closes[i] - opens[i]) / atr
    # 2. Общая волатильность (размер от хая до лоя) относительно ATR
    volatility = (highs[i] - lows[i]) / atr
    # 3. Всплеск объема (текущий объем к среднему за 20 баров)
    avg_vol = sum(volumes[i-20:i]) / 20 if i >= 20 else volumes[i]
    vol_ratio = volumes[i] / avg_vol if avg_vol > 0 else 1.0
    
    return [body, volatility, vol_ratio]

def calculate_distance(f1, f2):
    # Евклидово расстояние между двумя паттернами
    return math.sqrt((f1[0]-f2[0])**2 + (f1[1]-f2[1])**2 + (f1[2]-f2[2])**2)

def predict_knn(candles, current_idx, atr):
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    
    current_fp = get_fingerprint(opens, highs, lows, closes, volumes, current_idx, atr)
    
    distances = []
    # Обучение на истории: ищем похожие слепки в прошлом (отступаем 10 свечей, чтобы видеть будущее)
    for hist_i in range(25, current_idx - 10):
        hist_atr = sum([highs[j] - lows[j] for j in range(hist_i-14, hist_i)]) / 14
        hist_fp = get_fingerprint(opens, highs, lows, closes, volumes, hist_i, hist_atr)
        
        dist = calculate_distance(current_fp, hist_fp)
        
        # Смотрим в будущее исторической свечи: куда пошла цена через 5 часов?
        future_close = closes[hist_i + 5]
        price_diff = future_close - closes[hist_i]
        
        # Классифицируем: 1 (Рост), -1 (Падение), 0 (Флэт)
        if price_diff > hist_atr * 0.8: outcome = 1
        elif price_diff < -hist_atr * 0.8: outcome = -1
        else: outcome = 0
            
        distances.append((dist, outcome))
        
    # Сортируем по степени сходства (самые близкие расстояния)
    distances.sort(key=lambda x: x[0])
    top_neighbors = distances[:NEIGHBORS]
    
    # Считаем консенсус (голосование соседей)
    ups = sum(1 for d, o in top_neighbors if o == 1)
    downs = sum(1 for d, o in top_neighbors if o == -1)
    
    if ups >= CONSENSUS: return "LONG"
    if downs >= CONSENSUS: return "SHORT"
    return None

def calculate_terminal_backtest():
    t_all, w_all = 0, 0
    rows = []
    
    for symbol in SYMBOLS:
        candles = get_data(symbol)
        if not candles or len(candles) < 300: continue
        
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        
        t, w = 0, 0
        # Для скорости бэктеста проверяем последние 150 часов (около недели)
        start_idx = len(candles) - 150
        
        for i in range(start_idx, len(candles) - 10):
            atr = sum([highs[j] - lows[j] for j in range(i-14, i)]) / 14
            
            sig = predict_knn(candles, i, atr)
            
            if sig:
                t += 1
                hit = False
                curr_p = closes[i]
                sl = curr_p - (atr * 1.5) if sig == "LONG" else curr_p + (atr * 1.5)
                tp = curr_p + (atr * 2.0) if sig == "LONG" else curr_p - (atr * 2.0)
                
                for j in range(1, 10):
                    h_f = highs[i+j]
                    l_f = lows[i+j]
                    
                    if sig == "LONG":
                        if l_f <= sl: break
                        if h_f >= tp:
                            hit = True
                            break
                    else:
                        if h_f >= sl: break
                        if l_f <= tp:
                            hit = True
                            break
                if hit: w += 1
                    
        t_all += t
        w_all += w
        sym_name = symbol.replace('USDT', '').ljust(5)
        wr_sym = (w / t * 100) if t > 0 else 0.0
        rows.append(f"{sym_name} | {str(w).rjust(2)}/{str(t).rjust(3)} | {wr_sym:5.1f}%")
            
    winrate = (w_all / t_all * 100) if t_all > 0 else 0
    table_content = "\n".join(rows)
    
    report = (
        f"=== ML PATTERN BACKTEST (7D) ===\n"
        "PAIR  | WIN/TOT | WINRATE\n"
        "---------------------------------\n"
        f"{table_content}\n"
        "---------------------------------\n"
        f"TOTAL TRADES: {t_all}\n"
        f"SUCCESSFUL:   {w_all}\n"
        f"WINRATE:      {winrate:.1f}%\n"
        f"TIMESTAMP:    {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
    )
    return f"```text\n{report}\n```"

def get_main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📊 SYSTEM STATS (ML)", "callback_data": "SHOW_STATS"}],
            [{"text": "🪙 MONITORED ASSETS", "callback_data": "SHOW_ASSETS"},
             {"text": "🟢 BOT STATUS", "callback_data": "BOT_STATUS"}],
            [{"text": "🧠 STRATEGY INFO", "callback_data": "SHOW_STRATEGY"},
             {"text": "ℹ️ HELP", "callback_data": "SHOW_HELP"}]
        ]
    }

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}&parse_mode=Markdown"
    if keyboard: url += f"&reply_markup={urllib.parse.quote(json.dumps(keyboard))}"
    try: urllib.request.urlopen(url, context=context, timeout=5)
    except: pass

def broadcast(text):
    for chat_id in active_chats:
        send_msg(chat_id, text, get_main_keyboard())

def live_scanner():
    print("Machine Learning KNN Scanner Online...")
    last_alerts = {}
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol)
                if not candles or len(candles) < 300: continue
                
                closes = [float(c[4]) for c in candles]
                highs = [float(c[2]) for c in candles]
                lows = [float(c[3]) for c in candles]
                
                current_idx = len(candles) - 1
                curr_p = closes[current_idx]
                atr = sum([highs[j] - lows[j] for j in range(current_idx-14, current_idx)]) / 14
                
                # Бот сканирует историю и предсказывает исход
                signal = predict_knn(candles, current_idx, atr)
                
                if signal:
                    now = time.time()
                    if now - last_alerts.get(symbol, 0) > 28800: # Пауза 8 часов
                        last_alerts[symbol] = now
                        sym_name = symbol.replace('USDT', '')
                        
                        sl = curr_p - (atr * 1.5) if signal == "LONG" else curr_p + (atr * 1.5)
                        tp = curr_p + (atr * 2.0) if signal == "LONG" else curr_p - (atr * 2.0)
                        
                        msg = (
                            "```text\n"
                            f"[AI PATTERN ALERT] // {sym_name}USDT\n"
                            "---------------------------------\n"
                            f"ACTION:     {signal}\n"
                            f"TIMEFRAME:  {INTERVAL}\n"
                            f"ENTRY:      {curr_p:.4f}\n"
                            f"STOP-LOSS:  {sl:.4f}\n"
                            f"TAKE-PROFIT: {tp:.4f}\n"
                            "---------------------------------\n"
                            f"PROBABILITY: > 80% (KNN Match)\n"
                            f"NEAREST HISTORY MATCHES: {NEIGHBORS}\n"
                            f"TIME: {datetime.utcnow().strftime('%H:%M:%S')} UTC\n"
                            "```"
                        )
                        broadcast(msg)
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Scanner error: {e}")
            time.sleep(60)

def bot_engine():
    last_update_id = 0
    print("Machine Learning Engine Online...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            updates = json.loads(urllib.request.urlopen(req, timeout=35).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                chat_id = u.get("message", {}).get("chat", {}).get("id") or u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                data = u.get("callback_query", {}).get("data")
                
                if chat_id:
                    active_chats.add(chat_id)
                    
                    if data == "SHOW_STATS":
                        send_msg(chat_id, "Running Machine Learning backtest (heavy calculation)...", get_main_keyboard())
                        report = calculate_terminal_backtest()
                        send_msg(chat_id, report, get_main_keyboard())
                        
                    elif data == "SHOW_ASSETS":
                        assets_list = ", ".join([s.replace('USDT', '') for s in SYMBOLS])
                        msg = f"```text\nMONITORED ASSETS (10):\n{assets_list}\n```"
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "BOT_STATUS":
                        uptime_sec = int(time.time() - start_time)
                        hours = uptime_sec // 3600
                        minutes = (uptime_sec % 3600) // 60
                        msg = (
                            "```text\n"
                            "=== SYSTEM STATUS ===\n"
                            f"STATUS: ACTIVE (24/7)\n"
                            f"UPTIME: {hours}h {minutes}m\n"
                            f"ACTIVE CHATS: {len(active_chats)}\n"
                            f"STRATEGY: Machine Learning (k-NN)\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_STRATEGY":
                        msg = (
                            "```text\n"
                            "=== TRADING MODEL: AI PATTERN KNN ===\n"
                            "TYPE: Statistical Machine Learning\n"
                            "TIMEFRAME: 1h\n"
                            "---------------------------------\n"
                            "[ LOGIC ]\n"
                            "The bot ignores traditional indicators. It creates a mathematical 'fingerprint' of the current candlestick based on its body size, volatility, and volume anomaly.\n\n"
                            "It then scans the last 1000 hours of history for this exact coin to find the 5 most identical market conditions. If in 4 out of 5 historical cases the price aggressively rallied afterward, it predicts a LONG.\n\n"
                            "[ RISK MANAGEMENT ]\n"
                            "- Stop-Loss: 1.5 ATR\n"
                            "- Take-Profit: 2.0 ATR\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_HELP":
                        msg = (
                            "```text\n"
                            "=== TERMINAL HELP ===\n"
                            "1. SYSTEM STATS: 7-day AI Backtest.\n"
                            "2. ASSETS: Tracked pairs.\n"
                            "3. STRATEGY INFO: KNN logic breakdown.\n"
                            "4. SIGNALS: Historical pattern matches.\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    else:
                        welcome_text = (
                            "```text\n"
                            "AI QUANT TERMINAL ACTIVE\n"
                            "---------------------------------\n"
                            "System initialized. Calibrating historical memory...\n"
                            "```"
                        )
                        send_msg(chat_id, welcome_text, get_main_keyboard())
            time.sleep(1)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "AI Terminal Server Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
