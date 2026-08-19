from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
import math
from datetime import datetime
import pandas as pd
import numpy as np

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
# Белый список доступов (оба твоих ID)
ADMIN_CHAT_IDS = {8299008675, 7639836087}

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()
DB_FILE = "trades_log.json"
MEM_FILE = "bot_memory.json"

SCAN_INTERVAL = 60  # Турбо-режим: бот проверяет рынок каждую минуту
NEIGHBORS = 5

# --- МОДУЛЬ САМООБУЧЕНИЯ (РАЗДЕЛЬНАЯ ПАМЯТЬ ПО ТАЙМФРЕЙМАМ) ---
def get_memory(symbol, interval):
    key = f"{symbol}_{interval}"
    if not os.path.exists(MEM_FILE): return {"min_adx": 10}
    try:
        with open(MEM_FILE, 'r') as f: mem = json.load(f)
        return mem.get(key, {"min_adx": 10})
    except:
        return {"min_adx": 10}

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
        except Exception as e:
            print(f"Memory update error: {e}")

# --- ТЕХНИЧЕСКИЙ АНАЛИЗ (УМНЫЙ ADX + RSI НА ЧИСТОМ PANDAS) ---
def get_advanced_filters(candles):
    df = pd.DataFrame(candles, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'i1', 'i2', 'i3', 'i4', 'i5', 'i6'])
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    
    # 1. EMA 200 (Тренд)
    ema200 = df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
    
    # 2. Умный ADX (Относительная волатильность таймфрейма)
    candle_size = (df['high'] - df['low']) / df['close'] * 100
    recent_vol = candle_size.rolling(14).mean().iloc[-1]
    avg_vol = candle_size.rolling(100).mean().iloc[-1]
    adx = (recent_vol / avg_vol) * 20.0 if avg_vol > 0 else 20.0
    adx = max(5.0, min(50.0, adx))
    
    # 3. Чистый RSI (14) для защиты от покупок на хаях / продаж на дне
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    ema_gain = gain.ewm(com=13, adjust=False).mean()
    ema_loss = loss.ewm(com=13, adjust=False).mean()
    rs = ema_gain / ema_loss
    rsi = (100 - (100 / (1 + rs))).iloc[-1]
    
    return ema200, adx, rsi, df['close'].iloc[-1]

# --- ФУНКЦИИ БАЗЫ ДАННЫХ И СТАТИСТИКИ ---
def save_trade_to_db(symbol, signal, timeframe_label, reason, entry, close_price, is_news_anomaly):
    trade_data = {
        "symbol": symbol,
        "signal": signal,
        "timeframe": timeframe_label,
        "reason": reason,
        "entry": entry,
        "close_price": close_price,
        "news_anomaly": is_news_anomaly,
        "timestamp": time.time(),
        "date_str": datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    }
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, 'r') as f: log = json.load(f)
        else:
            log = []
        log.append(trade_data)
        with open(DB_FILE, 'w') as f: json.dump(log, f)
    except Exception as e:
        print(f"DB Error: {e}")

def generate_monthly_report():
    try:
        if not os.path.exists(DB_FILE): return "📭 База данных пуста. Сделок в этом месяце еще не было."
        with open(DB_FILE, 'r') as f: log = json.load(f)
        
        current_month = datetime.utcnow().month
        current_year = datetime.utcnow().year
        
        monthly_trades = [t for t in log if datetime.utcfromtimestamp(t['timestamp']).month == current_month and datetime.utcfromtimestamp(t['timestamp']).year == current_year]
                
        if not monthly_trades: return "📭 В текущем месяце закрытых сделок пока нет."
        
        report = "📊 **СТАТИСТИКА И САМОАНАЛИЗ ИИ**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
        
        for tf in ["⏱ СКАЛЬПИНГ", "⚡️ ИНТРАДЕЙ", "🌊 СВИНГ"]:
            trades = [t for t in monthly_trades if t['timeframe'] == tf]
            total = len(trades)
            if total == 0:
                report += f"**{tf}:** Нет сделок\n"
                continue
            wins = sum(1 for t in trades if t['reason'] == 'TP2')
            be = sum(1 for t in trades if t['reason'] == 'BE')
            losses = sum(1 for t in trades if t['reason'] == 'SL')
            wr = (wins / (total - be) * 100) if (total - be) > 0 else 0
            report += f"**{tf}:** Всего {total} | Тейки: {wins} | БУ: {be} | Стопы: {losses}\n🎯 Winrate: **{wr:.1f}%**\n\n"
            
        return report
    except Exception as e:
        return f"Ошибка отчета: {e}"

# --- МАТЕМАТИКА ИИ И k-NN ---
def get_data(symbol, interval, limit=1000):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=15) as r: return json.loads(r.read().decode())
    except: return []

def get_macro_trend():
    btc_1d = get_data("BTCUSDT", "1d", 50)
    if not btc_1d: return "NEUTRAL"
    closes = [float(c[4]) for c in btc_1d]
    sma20 = sum(closes[-20:]) / 20
    return "BULLISH" if closes[-1] > sma20 else ("BEARISH" if closes[-1] < sma20 else "NEUTRAL")

def calculate_distance(f1, f2):
    return math.sqrt(1.0*(f1[0]-f2[0])**2 + 1.0*(f1[1]-f2[1])**2 + 0.3*(f1[2]-f2[2])**2)

def predict_knn(candles, symbol, interval, current_idx, atr, macro_trend):
    ema200, adx, rsi, curr_p = get_advanced_filters(candles)
    mem = get_memory(symbol, interval)
    
    # 1. Проверка силы тренда с учетом памяти ИИ
    if adx < mem["min_adx"]: 
        return None, 0, "", f"🛡 Тренд слаб"

    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    
    avg_v = sum(volumes[current_idx-20:current_idx])/20 if current_idx >= 20 else volumes[current_idx]
    curr_fp = [(closes[current_idx]-opens[current_idx])/atr, (highs[current_idx]-lows[current_idx])/atr, volumes[current_idx]/avg_v if avg_v > 0 else 1.0]
    
    distances = []
    for hist_i in range(25, current_idx - 10):
        h_atr = sum([highs[j]-lows[j] for j in range(hist_i-14, hist_i)])/14
        if h_atr == 0: continue
        avg_hv = sum(volumes[hist_i-20:hist_i])/20 if hist_i >= 20 else volumes[hist_i]
        h_fp = [(closes[hist_i]-opens[hist_i])/h_atr, (highs[hist_i]-lows[hist_i])/h_atr, volumes[hist_i]/avg_hv if avg_hv > 0 else 1.0]
        dist = calculate_distance(curr_fp, h_fp)
        
        future_move = closes[hist_i+5] - closes[hist_i]
        outcome = 1 if future_move > h_atr*0.5 else (-1 if future_move < -h_atr*0.5 else 0)
        distances.append((dist, outcome, abs(future_move) / h_atr))
        
    distances.sort(key=lambda x: x[0])
    top_neighbors = distances[:NEIGHBORS]
    
    ups = sum(1 for d, o, m in top_neighbors if o == 1)
    downs = sum(1 for d, o, m in top_neighbors if o == -1)
    
    is_bullish_trend = curr_p > ema200
    
    # 2. Логика LONG + Защита RSI от перекупленности (Анти-FOMO)
    if ups >= 3 and macro_trend != "BEARISH" and is_bullish_trend:
        if rsi > 72:
            return None, 0, "", f"🛡 RSI перегрет ({rsi:.1f} > 72)"
        avg_move = sum(m for d, o, m in top_neighbors if o == 1) / ups
        conviction = "🔥 ВЫСОКАЯ (Риск 2.0%)" if ups == 5 else "⚡️ СРЕДНЯЯ (Риск 1.0%)"
        return "LONG", max(1.5, avg_move), conviction, ""
        
    # 3. Логика SHORT + Защита RSI от перепроданности
    if downs >= 3 and macro_trend != "BULLISH" and not is_bullish_trend:
        if rsi < 28:
            return None, 0, "", f"🛡 RSI перепродан ({rsi:.1f} < 28)"
        avg_move = sum(m for d, o, m in top_neighbors if o == -1) / downs
        conviction = "🔥 ВЫСОКАЯ (Риск 2.0%)" if downs == 5 else "⚡️ СРЕДНЯЯ (Риск 1.0%)"
        return "SHORT", max(1.5, avg_move), conviction, ""
        
    return None, 0, "", ""

def get_main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📊 СТАТИСТИКА И ПАМЯТЬ ИИ", "callback_data": "SHOW_STATS"}],
            [{"text": "🪙 МОНИТОРИНГ", "callback_data": "SHOW_ASSETS"},
             {"text": "🟢 СТАТУС БОТА", "callback_data": "BOT_STATUS"}],
            [{"text": "🧠 СТРАТЕГИЯ", "callback_data": "SHOW_STRATEGY"},
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
            return json.loads(r.read().decode()).get("result", {}).get("message_id")
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
        mid = send_msg(chat_id, text) 
        if mid: msgs.append((chat_id, mid))
    return msgs

def scan_timeframe(interval_name, label_name, cooldown_sec):
    print(f"Scanner thread started for {interval_name} ({label_name})...")
    last_alerts = {}
    active_trades = {}
    
    while True:
        try:
            macro_trend = get_macro_trend()
            now = time.time()
            
            for symbol in SYMBOLS:
                trade_key = f"{symbol}_{interval_name}"
                
                if trade_key in active_trades:
                    trade = active_trades[trade_key]
                    recent_15m = get_data(symbol, "15m", 10) 
                    
                    if recent_15m:
                        hit_result = None
                        close_price = None
                        is_news_anomaly = False
                        tp1_just_hit = False
                        
                        for c_15 in recent_15m:
                            h_15 = float(c_15[2])
                            l_15 = float(c_15[3])
                            vol = float(c_15[5])
                            
                            avg_vol = float(recent_15m[0][5]) if float(recent_15m[0][5]) > 0 else 1.0
                            if vol / avg_vol > 3.0: is_news_anomaly = True
                            
                            if trade["signal"] == "LONG":
                                if not trade["tp1_hit"] and h_15 >= trade["tp1"]:
                                    trade["tp1_hit"] = True
                                    trade["sl"] = trade["entry"] 
                                    tp1_just_hit = True
                                    
                                if l_15 <= trade["sl"]:
                                    hit_result = "BE" if trade["tp1_hit"] else "SL"
                                    close_price = trade["sl"]
                                elif h_15 >= trade["tp2"]:
                                    hit_result = "TP2"
                                    close_price = trade["tp2"]
                            else:
                                if not trade["tp1_hit"] and l_15 <= trade["tp1"]:
                                    trade["tp1_hit"] = True
                                    trade["sl"] = trade["entry"]
                                    tp1_just_hit = True
                                    
                                if h_15 >= trade["sl"]:
                                    hit_result = "BE" if trade["tp1_hit"] else "SL"
                                    close_price = trade["sl"]
                                elif h_15 >= trade["tp2"]:
                                    hit_result = "TP2"
                                    close_price = trade["tp2"]
                                    
                            if hit_result: break
                            
                        if tp1_just_hit and not hit_result:
                            new_msg = trade["original_msg"].replace("🤖 **AI ALERT", f"🟡 **[{label_name} | TP1 ВЗЯТ]")
                            trade["original_msg"] = new_msg
                            for chat_id, msg_id in trade["messages"]:
                                edit_msg(chat_id, msg_id, new_msg)

                        if hit_result:
                            save_trade_to_db(symbol, trade["signal"], label_name, hit_result, trade["entry"], close_price, is_news_anomaly)
                            update_memory(symbol, interval_name, hit_result)
                            
                            header = f"✅ **[{label_name} | ТЕЙК 2 ВЗЯТ]" if hit_result == "TP2" else (f"⚖️ **[{label_name} | БЕЗУБЫТОК]" if hit_result == "BE" else f"❌ **[{label_name} | СТОП-ЛОСС]")
                            updated_msg = trade["original_msg"].replace("🤖 **AI ALERT", header).replace(f"🟡 **[{label_name} | TP1 ВЗЯТ]", header)
                            updated_msg += f"\n\n**Итог сделки:**\nПричина: {hit_result}"
                            
                            for chat_id, msg_id in trade["messages"]:
                                edit_msg(chat_id, msg_id, updated_msg)
                                
                            del active_trades[trade_key]
                            continue 
                
                if trade_key not in active_trades:
                    if now - last_alerts.get(symbol, 0) < cooldown_sec: continue
                        
                    candles = get_data(symbol, interval_name, 300)
                    if not candles: continue
                    
                    highs = [float(c[2]) for c in candles]
                    lows = [float(c[3]) for c in candles]
                    closes = [float(c[4]) for c in candles]
                    
                    current_idx = len(candles) - 1
                    curr_p = closes[current_idx]
                    atr = sum([highs[j] - lows[j] for j in range(current_idx-14, current_idx)]) / 14
                    
                    # ИЗОЛЯЦИЯ СКАЛЬПИНГА: Для 15m алгоритм игнорирует макро-тренд
                    effective_macro = "NEUTRAL" if interval_name == "15m" else macro_trend
                    
                    signal, tp_atr_mult, conviction, _ = predict_knn(candles, symbol, interval_name, current_idx, atr, effective_macro)
                    
                    if signal:
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
                            f"🤖 **AI ALERT | {sym_name}/USDT**\n"
                            f"⏳ **Срок:** `{label_name}` ({interval_name})\n"
                            f"📉 **Направление:** {emo} **{signal}**\n\n"
                            f"> Уверенность ИИ: {conviction}\n"
                            f"> Макро-тренд (1D): **{effective_macro}**\n\n"
                            f"**Ордера (Нажми для копирования):**\n"
                            f"Вход: `{curr_p:.4f}`\n"
                            f"Стоп-Лосс: `{sl:.4f}` 🛡\n\n"
                            f"Цель 1 (TP1): `{tp1:.4f}` 🎯\n"
                            f"Цель 2 (TP2): `{tp2:.4f}` 🚀\n"
                        )
                        
                        msgs = broadcast(msg_text)
                        if msgs:
                            active_trades[trade_key] = {
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
            print(f"Scanner error ({interval_name}): {e}")
            time.sleep(60)

def bot_engine():
    last_update_id = 0
    print("Telegram Engine Online...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            updates = json.loads(urllib.request.urlopen(req, timeout=35).read().decode()).get("result", [])
            
            for u in updates:
                last_update_id = u["update_id"] + 1
                chat_id = message_id = data = None
                
                if "callback_query" in u:
                    chat_id = u["callback_query"]["message"]["chat"]["id"]
                    message_id = u["callback_query"]["message"]["message_id"]
                    data = u["callback_query"]["data"]
                elif "message" in u:
                    chat_id = u["message"]["chat"]["id"]
                
                if chat_id:
                    # Проверка по белому списку
                    if int(chat_id) not in ADMIN_CHAT_IDS: continue 
                    active_chats.add(chat_id)
                    
                    if data == "SHOW_STATS":
                        edit_msg(chat_id, message_id, generate_monthly_report(), get_main_keyboard())
                    elif data == "SHOW_ASSETS":
                        assets_list = "\n".join([f"🔹 `{s.replace('USDT', '')}`" for s in SYMBOLS])
                        edit_msg(chat_id, message_id, f"🪙 **МОНИТОРИНГ (10)**\n\n{assets_list}", get_main_keyboard())
                    elif data == "BOT_STATUS":
                        uptime_sec = int(time.time() - start_time)
                        h, m = uptime_sec // 3600, (uptime_sec % 3600) // 60
                        edit_msg(chat_id, message_id, f"🟢 **СТАТУС**\nАптайм: {h}ч {m}м\nСистема активна (15m, 1H, 4H).", get_main_keyboard())
                    elif data == "SHOW_STRATEGY":
                        edit_msg(chat_id, message_id, "🧠 **СТРАТЕГИЯ:** k-NN + Умный ТА + Изолированная память ИИ.", get_main_keyboard())
                    elif data == "SHOW_HELP":
                        help_text = (
                            "ℹ️ **РАЗВЕРНУТАЯ СПРАВКА ПО АРХИТЕКТУРЕ БОТА**\n"
                            "➖➖➖➖➖➖➖➖➖➖➖➖➖➖\n"
                            "Этот бот — гибридная алгоритмическая система. Он не пытается «угадать» цену, а работает на основе строгой математики, истории и машинного обучения.\n\n"
                            "🧠 **1. ЯДРО: Машинное обучение (k-NN)**\n"
                            "• Бот берет текущую рыночную ситуацию (паттерн свечей, объемы) и ищет в истории 5 точно таких же моментов.\n"
                            "• Если в прошлом после таких ситуаций цена шла вверх (минимум в 3 из 5 случаев), ИИ дает сигнал LONG. Если вниз — SHORT.\n\n"
                            "🛡 **2. ТЕХНИЧЕСКИЕ ФИЛЬТРЫ (ТА)**\n"
                            "• **EMA 200:** Базовый вектор. Выше линии — только покупки, ниже — только продажи.\n"
                            "• **Умный ADX:** Детектор флэта. Сравнивает волатильность текущих 14 свечей с нормой конкретного таймфрейма. Запрещает торговать, если рынок «мертв».\n"
                            "• **RSI (14) [Анти-FOMO]:** Блокирует покупки на абсолютных пиках (когда вылетает гигантская зеленая свеча и RSI > 72) и шорты на самом дне (RSI < 28).\n\n"
                            "⏱ **3. ТАЙМФРЕЙМЫ И МАКРО-ТРЕНД**\n"
                            "• **1H (Интрадей) и 4H (Свинг):** Строго зависят от дневного (1D) тренда Биткоина. Бот не пойдет против глобального рынка.\n"
                            "• **15m (Скальпинг):** Изолирован. Торгует локальные движения в обе стороны (Long/Short), ловя быстрые откаты и игнорируя макро-тренд.\n\n"
                            "💾 **4. САМООБУЧЕНИЕ И ПАМЯТЬ**\n"
                            "• Бот ведет «Дневник ошибок» (полностью раздельный для 15m, 1h и 4h).\n"
                            "• Если сделка закрылась по стопу (SL) или в безубыток (BE), бот повышает для этой монеты требования к силе тренда (ADX). Он перестает торговать «шумным» активом до появления железобетонного тренда.\n\n"
                            "⚖️ **5. РИСК-МЕНЕДЖМЕНТ (ATR)**\n"
                            "• Все цели динамические и зависят от текущей волатильности монеты (ATR).\n"
                            "• **TP1 (Сейв):** Первая цель. При её достижении бот переводит сделку в Безубыток (Стоп-лосс сдвигается на цену входа).\n"
                            "• **TP2 (Фиксация):** Финальная расчетная цель для закрытия позиции."
                        )
                        edit_msg(chat_id, message_id, help_text, get_main_keyboard())
                    elif "message" in u and "text" in u["message"]:
                        send_msg(chat_id, "🚀 **ТЕРМИНАЛ АКТИВЕН**\nВыбери действие 👇", get_main_keyboard())
                        
            time.sleep(1)
        except Exception as e:
            print(f"Engine error: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "AI Trading Bot Active"

if __name__ == "__main__":
    # Запуск Telegram-движка
    threading.Thread(target=bot_engine, daemon=True).start()
    
    # Запуск 3 независимых потоков сканирования рынка с уменьшенной паузой между сделками
    threading.Thread(target=scan_timeframe, args=("15m", "⏱ СКАЛЬПИНГ", 900), daemon=True).start()   # 15 минут
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ", 3600), daemon=True).start()    # 1 час
    threading.Thread(target=scan_timeframe, args=("4h", "🌊 СВИНГ", 14400), daemon=True).start()      # 4 часа
    
    # Запуск веб-сервера
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
