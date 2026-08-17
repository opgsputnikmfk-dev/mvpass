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
SCAN_INTERVAL = 300 
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
        if ups == 5: conviction = "🔥 ВЫСОКАЯ (Риск 2.0%)"
        elif ups == 4: conviction = "⚡️ СРЕДНЯЯ (Риск 1.0%)"
        else: conviction = "🛡 НИЗКАЯ (Риск 0.5%)"
        return "LONG", max(1.8, avg_move), conviction
        
    if downs >= 3 and macro_trend != "BULLISH":
        avg_move = sum(m for d, o, m in top_neighbors if o == -1) / downs
        if downs == 5: conviction = "🔥 ВЫСОКАЯ (Риск 2.0%)"
        elif downs == 4: conviction = "⚡️ СРЕДНЯЯ (Риск 1.0%)"
        else: conviction = "🛡 НИЗКАЯ (Риск 0.5%)"
        return "SHORT", max(1.8, avg_move), conviction
        
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
    print("AI Scanner + Advanced Tracker Online...")
    last_alerts = {}
    active_trades = {} 
    
    while True:
        try:
            macro_trend = get_macro_trend()
            now = time.time()
            
            for symbol in SYMBOLS:
                # 1. ТРЕКИНГ АКТИВНЫХ СДЕЛОК
                if symbol in active_trades:
                    trade = active_trades[symbol]
                    recent_15m = get_data(symbol, "15m", 10) 
                    
                    if recent_15m:
                        hit_result = None
                        tp1_just_hit = False
                        
                        for c_15 in recent_15m:
                            h_15 = float(c_15[2])
                            l_15 = float(c_15[3])
                            
                            if trade["signal"] == "LONG":
                                # Проверка TP1
                                if not trade["tp1_hit"] and h_15 >= trade["tp1"]:
                                    trade["tp1_hit"] = True
                                    trade["sl"] = trade["entry"] # Стоп в БУ
                                    tp1_just_hit = True
                                    
                                # Проверка SL / TP2
                                if l_15 <= trade["sl"]:
                                    hit_result = "BE" if trade["tp1_hit"] else "SL"
                                elif h_15 >= trade["tp2"]:
                                    hit_result = "TP2"
                            else:
                                if not trade["tp1_hit"] and l_15 <= trade["tp1"]:
                                    trade["tp1_hit"] = True
                                    trade["sl"] = trade["entry"]
                                    tp1_just_hit = True
                                    
                                if h_15 >= trade["sl"]:
                                    hit_result = "BE" if trade["tp1_hit"] else "SL"
                                elif l_15 <= trade["tp2"]:
                                    hit_result = "TP2"
                                    
                            if hit_result: break
                            
                        # Обновление сообщения при взятии TP1
                        if tp1_just_hit and not hit_result:
                            new_msg = trade["original_msg"].replace("🤖 **SWING AI ALERT", "🟡 **[TP1 ВЗЯТ - СТОП В БУ]")
                            trade["original_msg"] = new_msg
                            for chat_id, msg_id in trade["messages"]:
                                edit_msg(chat_id, msg_id, new_msg, get_main_keyboard())

                        # Финальное закрытие сделки
                        if hit_result:
                            reason = ""
                            if hit_result == "TP2":
                                header = "✅ **[ТЕЙК-ПРОФИТ 2 ВЗЯТ]"
                                reason = "🚀 **ФУЛЛ ПРОФИТ:** Цена успешно достигла главной зоны. ИИ отработал паттерн на 100%."
                            elif hit_result == "BE":
                                header = "⚖️ **[СДЕЛКА ЗАКРЫТА ПО БЕЗУБЫТКУ]"
                                reason = "🛡 **БЕЗУБЫТОК:** Был взят TP1, после чего рынок развернулся. Защитный алгоритм спас депозит от убытка."
                            else:
                                header = "❌ **[СТОП-ЛОСС]"
                                reason = "📉 **УБЫТОК:** Произошел импульсный сквиз, паттерн сломан. Риск-менеджмент защитил капитал."
                                
                            updated_msg = trade["original_msg"].replace("🤖 **SWING AI ALERT", header).replace("🟡 **[TP1 ВЗЯТ - СТОП В БУ]", header)
                            updated_msg += f"\n\n**Итог:**\n{reason}"
                            
                            for chat_id, msg_id in trade["messages"]:
                                edit_msg(chat_id, msg_id, updated_msg, get_main_keyboard())
                                
                            del active_trades[symbol]
                            continue 
                
                # 2. ПОИСК НОВЫХ СИГНАЛОВ
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
                        if now - last_alerts.get(symbol, 0) > 86400: 
                            last_alerts[symbol] = now
                            sym_name = symbol.replace('USDT', '')
                            
                            sl_dist = atr * 2.0
                            tp1_dist = atr * 1.0 # Консервативный первый тейк
                            tp2_dist = atr * tp_atr_mult # Основная цель от ИИ
                            
                            if signal == "LONG":
                                sl = curr_p - sl_dist
                                tp1 = curr_p + tp1_dist
                                tp2 = curr_p + tp2_dist
                                emo = "🟢"
                            else:
                                sl = curr_p + sl_dist
                                tp1 = curr_p - tp1_dist
                                tp2 = curr_p - tp2_dist
                                emo = "🔴"
                            
                            msg_text = (
                                f"🤖 **SWING AI ALERT | {sym_name}/USDT**\n"
                                f"📉 **Направление:** {emo} **{signal}** (4H)\n\n"
                                f"> Уверенность ИИ: {conviction}\n"
                                f"> Макро-тренд Биткоина: **{macro_trend}**\n\n"
                                f"**Ордера (Нажми на цифру для копирования):**\n"
                                f"Вход: `{curr_p:.4f}`\n"
                                f"Стоп-Лосс: `{sl:.4f}` 🛡\n\n"
                                f"Цель 1 (TP1): `{tp1:.4f}` 🎯 *(При достижении переведи Стоп в Вход)*\n"
                                f"Цель 2 (TP2): `{tp2:.4f}` 🚀\n"
                            )
                            
                            msgs = broadcast(msg_text)
                            if msgs:
                                active_trades[symbol] = {
                                    "signal": signal,
                                    "entry": curr_p,
                                    "sl": sl,
                                    "tp1": tp1,
                                    "tp2": tp2,
                                    "tp1_hit": False,
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
                            f"STRATEGY: Swing AI (4H) + Dual TP Tracker\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_STRATEGY":
                        msg = (
                            "```text\n"
                            "=== TRADING MODEL: SWING AI ===\n"
                            "TYPE: 4H Institutional Machine Learning\n"
                            "---------------------------------\n"
                            "[ DUAL TAKE-PROFIT LOGIC ]\n"
                            "1. TP1 (Safe): 1.0 ATR. Fast liquidity grab. Secures the trade.\n"
                            "2. TP2 (Max): Calculated by AI based on historical pattern magnitude.\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_HELP":
                        msg = (
                            "```text\n"
                            "=== TERMINAL HELP ===\n"
                            "You can click on any price in the signal to copy it directly to your clipboard.\n"
                            "Bot auto-tracks TP1, TP2, and Break-Even logic.\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    else:
                        welcome_text = (
                            "```text\n"
                            "SWING AI TERMINAL ACTIVE\n"
                            "---------------------------------\n"
                            "System initialized. Dual TP Auto-Tracking enabled.\n"
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
