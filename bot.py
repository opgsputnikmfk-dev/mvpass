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

# --- ПАРАМЕТРЫ INTRADAY AI (1H) ---
INTERVAL = "1h"
HISTORY_LIMIT = 1000
SCAN_INTERVAL = 300 # Трекер проверяет сделки каждые 5 минут
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
    # Глобальный фильтр всегда остается на дневке (1D)
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
        
        # На 1H графике смотрим будущее на 5 баров (5 часов)
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
        return "LONG", max(1.5, avg_move), conviction
        
    if downs >= 3 and macro_trend != "BULLISH":
        avg_move = sum(m for d, o, m in top_neighbors if o == -1) / downs
        if downs == 5: conviction = "🔥 ВЫСОКАЯ (Риск 2.0%)"
        elif downs == 4: conviction = "⚡️ СРЕДНЯЯ (Риск 1.0%)"
        else: conviction = "🛡 НИЗКАЯ (Риск 0.5%)"
        return "SHORT", max(1.5, avg_move), conviction
        
    return None, 0, ""

def get_main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🪙 МОНИТОРИНГ", "callback_data": "SHOW_ASSETS"},
             {"text": "🟢 СТАТУС БОТА", "callback_data": "BOT_STATUS"}],
            [{"text": "🧠 СТРАТЕГИЯ ИИ", "callback_data": "SHOW_STRATEGY"},
             {"text": "ℹ️ СПРАВКА", "callback_data": "SHOW_HELP"}]
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
        mid = send_msg(chat_id, text) # Кнопки отключены для идеальной ленты
        if mid: msgs.append((chat_id, mid))
    return msgs

def live_scanner():
    print("Intraday AI Scanner + Auto-Tracker Online (1H)...")
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
                                if not trade["tp1_hit"] and h_15 >= trade["tp1"]:
                                    trade["tp1_hit"] = True
                                    trade["sl"] = trade["entry"] 
                                    tp1_just_hit = True
                                    
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
                            
                        if tp1_just_hit and not hit_result:
                            new_msg = trade["original_msg"].replace("🤖 **INTRADAY AI ALERT", "🟡 **[TP1 ВЗЯТ - СТОП В БУ]")
                            trade["original_msg"] = new_msg
                            for chat_id, msg_id in trade["messages"]:
                                edit_msg(chat_id, msg_id, new_msg) # Кнопки отключены

                        if hit_result:
                            reason = ""
                            if hit_result == "TP2":
                                header = "✅ **[ТЕЙК-ПРОФИТ 2 ВЗЯТ]"
                                reason = "🚀 **ФУЛЛ ПРОФИТ:** Цена успешно достигла главной цели. ИИ отработал паттерн на 100%."
                            elif hit_result == "BE":
                                header = "⚖️ **[СДЕЛКА ЗАКРЫТА ПО БЕЗУБЫТКУ]"
                                reason = "🛡 **БЕЗУБЫТОК:** Был взят TP1, после чего рынок развернулся. Защита спасла депозит от убытка."
                            else:
                                header = "❌ **[СТОП-ЛОСС]"
                                reason = "📉 **УБЫТОК:** Произошел импульсный сквиз, паттерн сломан. Риск-менеджмент защитил капитал."
                                
                            updated_msg = trade["original_msg"].replace("🤖 **INTRADAY AI ALERT", header).replace("🟡 **[TP1 ВЗЯТ - СТОП В БУ]", header)
                            updated_msg += f"\n\n**Итог сделки:**\n{reason}"
                            
                            for chat_id, msg_id in trade["messages"]:
                                edit_msg(chat_id, msg_id, updated_msg) # Кнопки отключены
                                
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
                        if now - last_alerts.get(symbol, 0) > 28800: 
                            last_alerts[symbol] = now
                            sym_name = symbol.replace('USDT', '')
                            
                            sl_dist = atr * 2.0
                            tp1_dist = atr * 1.0 
                            tp2_dist = atr * tp_atr_mult 
                            
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
                                f"🤖 **INTRADAY AI ALERT | {sym_name}/USDT**\n"
                                f"📉 **Направление:** {emo} **{signal}** (1H)\n\n"
                                f"> Уверенность ИИ: {conviction}\n"
                                f"> Макро-тренд Биткоина (1D): **{macro_trend}**\n\n"
                                f"**Ордера (Нажми на цену для копирования):**\n"
                                f"Вход: `{curr_p:.4f}`\n"
                                f"Стоп-Лосс: `{sl:.4f}` 🛡\n\n"
                                f"Цель 1 (TP1): `{tp1:.4f}` 🎯 *(При достижении Стоп в БУ)*\n"
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
    print("Intraday AI Engine Online...")
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
                        assets_list = "\n".join([f"🔹 `{s.replace('USDT', '')}`" for s in SYMBOLS])
                        msg = (
                            "🪙 **МОНИТОРИНГ АКТИВОВ (10)**\n\n"
                            f"Алгоритм непрерывно анализирует:\n{assets_list}"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "BOT_STATUS":
                        uptime_sec = int(time.time() - start_time)
                        hours = uptime_sec // 3600
                        minutes = (uptime_sec % 3600) // 60
                        msg = (
                            "🟢 **СИСТЕМНЫЙ СТАТУС**\n\n"
                            f"▫️ **Ядро:** Активно (24/7)\n"
                            f"▫️ **Аптайм:** {hours}ч {minutes}м\n"
                            f"▫️ **Активных чатов:** {len(active_chats)}\n"
                            f"▫️ **Модель:** Intraday AI (1H) + Dual TP\n\n"
                            f"✅ *Служба авто-трекинга сделок работает в фоновом режиме.*"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_STRATEGY":
                        msg = (
                            "🧠 **ТОРГОВАЯ МОДЕЛЬ: INTRADAY AI**\n\n"
                            "**Тип:** 1H Внутридневное Машинное Обучение\n"
                            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
                            "⚙️ **Логика ИИ (k-NN):**\n"
                            "Бот анализирует часовые графики. Ищет 5 совпадений из прошлого и прогнозирует короткие интрадей-волны.\n\n"
                            "🛡 **Система Dual TP (Ведение сделки):**\n"
                            "• **TP1 (Сейф):** Расстояние 1 ATR. Сделка переводится в безубыток.\n"
                            "• **TP2 (Макс):** Главная цель по истории паттерна.\n\n"
                            "🧭 **Макро-фильтр (1D BTC):**\n"
                            "Жесткий запрет на интрадей-сделки, идущие против дневного глобального тренда Биткоина."
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_HELP":
                        msg = (
                            "ℹ️ **СПРАВКА ПО ТЕРМИНАЛУ**\n\n"
                            "💡 **Инструкция к действию:**\n"
                            "1. Все цифры (вход, стоп, тейки) **кликабельны** — копируй в один клик.\n"
                            "2. Оформляй сделку на бирже по сигналам.\n"
                            "3. Бот **сам проследит** за графиком и изменит сообщение на `[TP1 ВЗЯТ]`, `[TP2 ВЗЯТ]` или `[СТОП-ЛОСС]`. Тебе не нужно сидеть у монитора.\n"
                            "4. Чтобы вызвать это меню в любой момент, просто отправь боту любое текстовое сообщение."
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif "message" in u and "text" in u["message"]:
                        welcome_text = (
                            "🚀 **INTRADAY AI ТЕРМИНАЛ АКТИВЕН**\n"
                            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
                            "Система инициализирована. ИИ сканирует 1H график с макро-защитой 1D...\n\n"
                            "Ожидайте сигналов. Выберите действие в меню ниже 👇"
                        )
                        send_msg(chat_id, welcome_text, get_main_keyboard())
            time.sleep(1)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Intraday AI Tracker Server Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
