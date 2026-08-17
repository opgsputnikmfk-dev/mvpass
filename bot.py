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

# --- ПАРАМЕТРЫ SWING AI (4H) ---
INTERVAL = "4h"
HISTORY_LIMIT = 1000
SCAN_INTERVAL = 300 # Снизили до 5 минут, чтобы бот быстро фиксировал закрытие сделок
NEIGHBORS = 5

def get_data(symbol, interval=INTERVAL, limit=HISTORY_LIMIT):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=15) as r: 
            return json.loads(r.read().decode())
    except:
        return []

def get_macro_trend():
    btc_1d = get_data("BTCUSDT", "1d", 50)
    if not btc_1d: return "NEUTRAL"
    closes = [float(c[4]) for c in btc_1d]
    sma20 = sum(closes[-20:]) / 20
    if closes[-1] > sma20: return "BULLISH"
    elif closes[-1] < sma20: return "BEARISH"
    return "NEUTRAL"

def get_fingerprint(opens, highs, lows, closes, volumes, i, atr):
    if atr == 0: return [0, 0, 0]
    body = (closes[i] - opens[i]) / atr
    volatility = (highs[i] - lows[i]) / atr
    avg_vol = sum(volumes[i-20:i]) / 20 if i >= 20 else volumes[i]
    vol_ratio = volumes[i] / avg_vol if avg_vol > 0 else 1.0
    return [body, volatility, vol_ratio]

def calculate_distance(f1, f2):
    return math.sqrt((f1[0]-f2[0])**2 + (f1[1]-f2[1])**2 + (f1[2]-f2[2])**2)

def predict_knn(candles, current_idx, atr, macro_trend):
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    
    current_fp = get_fingerprint(opens, highs, lows, closes, volumes, current_idx, atr)
    distances = []
    
    for hist_i in range(25, current_idx - 10):
        hist_atr = sum([highs[j] - lows[j] for j in range(hist_i-14, hist_i)]) / 14
        if hist_atr == 0: continue
        hist_fp = get_fingerprint(opens, highs, lows, closes, volumes, hist_i, hist_atr)
        dist = calculate_distance(current_fp, hist_fp)
        
        future_move = closes[hist_i + 5] - closes[hist_i]
        outcome = 1 if future_move > hist_atr * 0.5 else (-1 if future_move < -hist_atr * 0.5 else 0)
        distances.append((dist, outcome, abs(future_move) / hist_atr))
        
    distances.sort(key=lambda x: x[0])
    top_neighbors = distances[:NEIGHBORS]
    
    ups = sum(1 for d, o, m in top_neighbors if o == 1)
    downs = sum(1 for d, o, m in top_neighbors if o == -1)
    
    if ups >= 3 and macro_trend != "BEARISH":
        avg_move = sum(m for d, o, m in top_neighbors if o == 1) / ups
        if ups == 5: conviction = "HIGH (2.0% RISK)"
        elif ups == 4: conviction = "NORMAL (1.0% RISK)"
        else: conviction = "LOW (0.5% RISK)"
        return "LONG", max(1.5, avg_move), conviction
        
    if downs >= 3 and macro_trend != "BULLISH":
        avg_move = sum(m for d, o, m in top_neighbors if o == -1) / downs
        if downs == 5: conviction = "HIGH (2.0% RISK)"
        elif downs == 4: conviction = "NORMAL (1.0% RISK)"
        else: conviction = "LOW (0.5% RISK)"
        return "SHORT", max(1.5, avg_move), conviction
        
    return None, 0, ""

def get_main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🪙 MONITORED ASSETS", "callback_data": "SHOW_ASSETS"},
             {"text": "🟢 BOT STATUS", "callback_data": "BOT_STATUS"}],
            [{"text": "🧠 STRATEGY INFO", "callback_data": "SHOW_STRATEGY"},
             {"text": "ℹ️ HELP", "callback_data": "SHOW_HELP"}]
        ]
    }

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard: data["reply_markup"] = keyboard
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, context=context, timeout=10) as r:
            resp = json.loads(r.read().decode())
            return resp.get("result", {}).get("message_id")
    except Exception as e:
        print(f"Send error: {e}")
        return None

def edit_msg(chat_id, message_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if keyboard: data["reply_markup"] = keyboard
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, context=context, timeout=10)
    except Exception as e:
        print(f"Edit error: {e}")

def broadcast(text):
    msgs = []
    for chat_id in active_chats:
        mid = send_msg(chat_id, text, get_main_keyboard())
        if mid: msgs.append((chat_id, mid))
    return msgs

def live_scanner():
    print("Swing AI Scanner + Trade Tracker Online...")
    last_alerts = {}
    active_trades = {} # Храним открытые сделки для трекинга
    
    while True:
        try:
            macro_trend = get_macro_trend()
            now = time.time()
            
            for symbol in SYMBOLS:
                # 1. СНАЧАЛА ПРОВЕРЯЕМ ОТКРЫТЫЕ СДЕЛКИ (ТРЕКИНГ)
                if symbol in active_trades:
                    trade = active_trades[symbol]
                    # Загружаем 15-минутный график для точной проверки зацепа стопов/тейков
                    recent_15m = get_data(symbol, "15m", 10) 
                    if recent_15m:
                        hit_result = None
                        for c_15 in recent_15m:
                            h_15 = float(c_15[2])
                            l_15 = float(c_15[3])
                            
                            if trade["signal"] == "LONG":
                                if l_15 <= trade["sl"]: hit_result = "SL"
                                elif h_15 >= trade["tp"]: hit_result = "TP"
                            else:
                                if h_15 >= trade["sl"]: hit_result = "SL"
                                elif l_15 <= trade["tp"]: hit_result = "TP"
                                
                            if hit_result: break
                            
                        # Если сделка закрылась, редактируем сообщение в Telegram
                        if hit_result:
                            reason = ""
                            if hit_result == "TP":
                                reason = "✅ **PROFIT (ТЕЙК-ПРОФИТ):** Цена успешно достигла расчетной зоны ликвидности. Исторический паттерн отработал идеально, алгоритм зафиксировал прибыль."
                            else:
                                reason = "❌ **STOP-LOSS (УБЫТОК):** Паттерн сломан. Произошел импульсный сквиз или смена локального тренда. Жесткий риск-менеджмент защитил капитал от ликвидации."
                                
                            updated_msg = trade["original_msg"].replace("[SWING AI ALERT]", f"[{hit_result} HIT - СДЕЛКА ЗАКРЫТА]")
                            updated_msg += f"\n\n**Аналитика закрытия:**\n{reason}"
                            
                            for chat_id, msg_id in trade["messages"]:
                                edit_msg(chat_id, msg_id, updated_msg, get_main_keyboard())
                                
                            del active_trades[symbol]
                            continue # Переходим к следующей монете
                
                # 2. ЕСЛИ СДЕЛОК НЕТ - ИЩЕМ НОВЫЙ СИГНАЛ
                if symbol not in active_trades:
                    candles = get_data(symbol, INTERVAL, 300)
                    if not candles: continue
                    
                    highs = [float(c[2]) for c in candles]
                    lows = [float(c[3]) for c in candles]
                    closes = [float(c[4]) for c in candles]
                    
                    current_idx = len(candles) - 1
                    curr_p = closes[current_idx]
                    atr = sum([highs[j] - lows[j] for j in range(current_idx-14, current_idx)]) / 14
                    
                    signal, tp_atr_mult, conviction = predict_knn(candles, current_idx, atr, macro_trend)
                    
                    if signal:
                        if now - last_alerts.get(symbol, 0) > 86400: # Пауза 24 часа на одну монету
                            last_alerts[symbol] = now
                            sym_name = symbol.replace('USDT', '')
                            
                            sl_dist = atr * 2.0
                            tp_dist = atr * tp_atr_mult
                            
                            if signal == "LONG":
                                sl = curr_p - sl_dist
                                tp = curr_p + tp_dist
                                be_point = curr_p + (atr * 1.0)
                            else:
                                sl = curr_p + sl_dist
                                tp = curr_p - tp_dist
                                be_point = curr_p - (atr * 1.0)
                            
                            msg_text = (
                                "```text\n"
                                f"[SWING AI ALERT] // {sym_name}USDT\n"
                                "---------------------------------\n"
                                f"ACTION:     {signal}\n"
                                f"TIMEFRAME:  4h\n"
                                f"ENTRY:      {curr_p:.4f}\n"
                                f"STOP-LOSS:  {sl:.4f}\n"
                                f"TAKE-PROFIT: {tp:.4f}\n"
                                "---------------------------------\n"
                                f"MOVE SL TO BREAK-EVEN AT: {be_point:.4f}\n"
                                "---------------------------------\n"
                                f"BTC 1D MACRO: {macro_trend}\n"
                                f"RECOMMENDED RISK: {conviction}\n"
                                f"TIME: {datetime.utcnow().strftime('%H:%M:%S')} UTC\n"
                                "```"
                            )
                            
                            # Отправляем и запоминаем ID сообщений
                            msgs = broadcast(msg_text)
                            if msgs:
                                active_trades[symbol] = {
                                    "signal": signal,
                                    "entry": curr_p,
                                    "sl": sl,
                                    "tp": tp,
                                    "messages": msgs,
                                    "original_msg": msg_text
                                }
                                
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Scanner error: {e}")
            time.sleep(60)

def bot_engine():
    last_update_id = 0
    print("Swing AI Engine Online...")
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
                    
                    if data == "SHOW_ASSETS":
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
                            f"STRATEGY: Swing AI (4H) + AutoTracker\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_STRATEGY":
                        msg = (
                            "```text\n"
                            "=== TRADING MODEL: SWING AI ===\n"
                            "TYPE: 4H Institutional Machine Learning\n"
                            "---------------------------------\n"
                            "The bot captures large market swings. It predicts price action by finding historical matches to current candlestick fingerprints.\n\n"
                            "[ TRADE TRACKING ]\n"
                            "Bot continuously monitors active signals. Once TP or SL is hit, the original Telegram message will be dynamically edited with the final trade result and analytics.\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_HELP":
                        msg = (
                            "```text\n"
                            "=== TERMINAL HELP ===\n"
                            "You do not need to check the charts. The bot will edit the signal message to [TP HIT] or [SL HIT] automatically when the trade concludes.\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    else:
                        welcome_text = (
                            "```text\n"
                            "SWING AI TERMINAL ACTIVE\n"
                            "---------------------------------\n"
                            "System initialized. Trade Auto-Tracking enabled.\n"
                            "```"
                        )
                        send_msg(chat_id, welcome_text, get_main_keyboard())
            time.sleep(1)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Swing AI Tracker Server Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
