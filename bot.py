from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
import math
from datetime import datetime
import pandas as pd
import numpy as np
from google import genai

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_IDS = {8299008675}
SIGNAL_CHANNEL_ID = os.getenv("SIGNAL_CHANNEL_ID")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()

MEM_FILE = "bot_memory.json"
ACTIVE_TRADES_FILE = "active_trades_memory.json" 
STATS_FILE = "bot_stats.json" 
COOLDOWNS_FILE = "cooldowns_memory.json"
LEDGER_FILE = "bot_ledger.json" # Новый файл для истории сделок

SCAN_INTERVAL = 60
NEIGHBORS = 5

# --- ГЛОБАЛЬНЫЕ ПЕРЕКЛЮЧАТЕЛИ И ЛИМИТЫ ---
SIGNALS_ENABLED = True  # Глобальный рубильник поиска сигналов (Кнопка Паузы)
SCALP_ENABLED = True  
MAX_TRADES_PER_TF = 3  # Максимум 3 сделки НА КАЖДЫЙ ТАЙМФРЕЙМ отдельно

# --- LEDGER (ИСТОРИЯ СДЕЛОК) ---
def get_ledger():
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, 'r') as f: return json.load(f)
        except: pass
    return []

def add_to_ledger(trade_entry):
    ledger = get_ledger()
    ledger.insert(0, trade_entry)
    ledger = ledger[:10]  # Храним только 10 последних сделок
    try:
        with open(LEDGER_FILE, 'w') as f: json.dump(ledger, f)
    except: pass

# --- СИСТЕМА КУЛДАУНОВ (ЗАЩИТА ОТ ЛУЗСТРИКА) ---
def load_cooldowns():
    if os.path.exists(COOLDOWNS_FILE):
        try:
            with open(COOLDOWNS_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {}

def save_cooldowns():
    try:
        with open(COOLDOWNS_FILE, 'w') as f:
            json.dump(cooldowns, f)
    except: pass

cooldowns = load_cooldowns()

def set_cooldown(symbol, interval, duration_sec):
    key = f"{symbol}_{interval}"
    cooldowns[key] = time.time() + duration_sec
    save_cooldowns()

def is_on_cooldown(symbol, interval):
    key = f"{symbol}_{interval}"
    if key in cooldowns:
        if time.time() < cooldowns[key]:
            return True
        else:
            del cooldowns[key]
            save_cooldowns()
    return False

# --- ПАМЯТЬ АКТИВНЫХ СДЕЛОК ---
def load_active_trades():
    if os.path.exists(ACTIVE_TRADES_FILE):
        try:
            with open(ACTIVE_TRADES_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {}

def save_active_trades():
    try:
        with open(ACTIVE_TRADES_FILE, 'w') as f:
            json.dump(active_trades, f)
    except Exception as e:
        print(f"Ошибка сохранения памяти сделок: {e}")

active_trades = load_active_trades()

# --- ЛОКАЛЬНАЯ СТАТИСТИКА ПО ТАЙМФРЕЙМАМ ---
def save_local_stat(tf_label, reason, pnl_pct):
    stats = {}
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                stats = json.load(f)
        except: pass
    
    if tf_label not in stats:
        stats[tf_label] = {"TP2": 0, "BE": 0, "SL": 0, "total_pnl": 0.0, "streak": 0}
    
    if reason in stats[tf_label]:
        stats[tf_label][reason] += 1
        
    stats[tf_label]["total_pnl"] += pnl_pct
    
    current_streak = stats[tf_label].get("streak", 0)
    if reason == "TP2":
        stats[tf_label]["streak"] = current_streak + 1 if current_streak > 0 else 1
    elif reason == "SL":
        stats[tf_label]["streak"] = current_streak - 1 if current_streak < 0 else -1
        
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(stats, f)
    except Exception as e:
        print(f"Ошибка записи локальной статистики: {e}")

def generate_local_report():
    if not os.path.exists(STATS_FILE):
        return "📭 Статистика пуста. Закрытых сделок еще нет."
    
    try:
        with open(STATS_FILE, 'r') as f:
            stats = json.load(f)
    except:
        return "❌ Ошибка чтения файла статистики."

    if not stats: 
        return "📭 Статистика пуста. Закрытых сделок еще нет."
    
    report = "📊 **ОБЩИЙ PNL И АНАЛИТИКА**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
    total_pnl_all = 0.0
    
    for tf in ["⏱ СКАЛЬПИНГ", "⚡️ ИНТРАДЕЙ", "🌊 СВИНГ"]:
        if tf not in stats:
            continue
            
        data = stats[tf]
        tp2 = data.get('TP2', 0)
        be = data.get('BE', 0)
        sl = data.get('SL', 0)
        pnl = data.get('total_pnl', 0.0)
        streak = data.get('streak', 0)
        
        total = tp2 + be + sl
        if total == 0: continue
            
        total_pnl_all += pnl
        valid_trades = total - be
        wr = (tp2 / valid_trades * 100) if valid_trades > 0 else 0
        
        if streak > 0:
            streak_str = f"🔥 {streak} в плюс"
        elif streak < 0:
            streak_str = f"🩸 {abs(streak)} в минус"
        else:
            streak_str = "➖"
        
        report += f"**{tf}**\n"
        report += f"📈 Сделок: {total} (Тейки: {tp2} | БУ: {be} | Стопы: {sl})\n"
        report += f"🎯 Winrate: **{wr:.1f}%**\n"
        report += f"💰 Теоретический PnL: **{pnl:+.2f}%**\n"
        report += f"⚡️ Текущий стрик: {streak_str}\n\n"
        
    report += f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
    report += f"💵 **ОБЩИЙ ИТОГ (Без плеча): {total_pnl_all:+.2f}%**"
        
    return report

# --- ФУНКЦИИ ПОЛУЧЕНИЯ ДАННЫХ И СЕТИ ---
def get_current_price(symbol):
    url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=3) as r:
            return float(json.loads(r.read().decode())['price'])
    except: 
        return None

def get_data(symbol, interval, limit=1000):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=15) as r: return json.loads(r.read().decode())
    except: return []

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

def send_to_channel(text):
    if SIGNAL_CHANNEL_ID:
        return send_msg(SIGNAL_CHANNEL_ID, text)
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

# --- ИНТЕЛЛЕКТУАЛЬНЫЕ ФУНКЦИИ И ФИЛЬТРЫ ---
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

def ask_ai_oracle(symbol, signal, current_price, rsi, adx, recent_closes):
    try:
        prompt = f"Ты квантовый риск-менеджер. Анализ входа {signal} по {symbol}. Цена: {current_price}. RSI: {rsi:.1f}. ADX: {adx:.1f}. Последние цены: {recent_closes[-5:]}. Оцени риск отката. Если вход безопасен — 'APPROVE', если есть риск — 'REJECT'. Только одно слово."
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt,
        )
        return "APPROVE" in response.text.strip().upper()
    except Exception as e:
        print(f"AI Oracle Error: {e}")
        return True

def check_order_book(symbol, signal_type):
    url = f"https://data-api.binance.vision/api/v3/depth?symbol={symbol}&limit=20"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=5) as r:
            depth = json.loads(r.read().decode())
            
        bids = sum([float(item[1]) for item in depth.get("bids", [])])
        asks = sum([float(item[1]) for item in depth.get("asks", [])])
        
        if asks == 0: return True
        ratio = bids / asks
        
        if signal_type == "LONG" and ratio >= 0.85:
            return True
        elif signal_type == "SHORT" and ratio <= 1.15:
            return True
        return False
    except Exception as e:
        print(f"Order Book Error for {symbol}: {e}")
        return True

def get_advanced_filters(candles, idx):
    df = pd.DataFrame(candles[:idx+1], columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'i1', 'i2', 'i3', 'i4', 'i5', 'i6'])
    for col in ['open', 'high', 'low', 'close', 'vol']:
        df[col] = df[col].astype(float)
    
    ema200 = df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
    candle_size = (df['high'] - df['low']) / df['close'] * 100
    recent_vol = candle_size.rolling(14).mean().iloc[-1]
    avg_vol = candle_size.rolling(100).mean().iloc[-1]
    adx = (recent_vol / avg_vol) * 20.0 if avg_vol > 0 else 20.0
    adx = max(5.0, min(50.0, adx))
    
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    ema_gain = gain.ewm(com=13, adjust=False).mean()
    ema_loss = loss.ewm(com=13, adjust=False).mean()
    rs = ema_gain / ema_loss
    rsi = (100 - (100 / (1 + rs))).iloc[-1]
    
    avg_historical_size = candle_size.shift(1).rolling(50).mean().iloc[-1]
    current_size = candle_size.iloc[-1]
    is_anomaly = False
    if avg_historical_size > 0 and (current_size / avg_historical_size) > 2.5:
        is_anomaly = True

    sma20 = df['close'].rolling(20).mean().iloc[-1]
    std20 = df['close'].rolling(20).std().iloc[-1]
    upper_3sigma = sma20 + (3.0 * std20)
    lower_3sigma = sma20 - (3.0 * std20)
    
    avg_vol_20 = df['vol'].rolling(20).mean().iloc[-1]
    is_vol_climax = (df['vol'].iloc[-1] / avg_vol_20 > 4.5) if avg_vol_20 > 0 else False
    
    curr_p = df['close'].iloc[-1]
    closes_list = df['close'].tolist()
    return ema200, adx, rsi, is_anomaly, is_vol_climax, upper_3sigma, lower_3sigma, curr_p, closes_list

def get_macro_trend():
    btc_1d = get_data("BTCUSDT", "1d", 50)
    if not btc_1d: return "NEUTRAL"
    closes = [float(c[4]) for c in btc_1d]
    sma20 = sum(closes[-20:]) / 20
    return "BULLISH" if closes[-1] > sma20 else ("BEARISH" if closes[-1] < sma20 else "NEUTRAL")

def calculate_distance(f1, f2):
    return math.sqrt(1.0*(f1[0]-f2[0])**2 + 1.0*(f1[1]-f2[1])**2 + 0.3*(f1[2]-f2[2])**2)

def predict_knn(candles, symbol, interval, current_idx, atr, macro_trend):
    ema200, adx, rsi, is_anomaly, is_vol_climax, upper_3sigma, lower_3sigma, curr_p, closes_list = get_advanced_filters(candles, current_idx)
    mem = get_memory(symbol, interval)
    
    if is_anomaly or is_vol_climax:
        return None, 0, "", "🛡 Аномалия / Кульминация объема"
    
    if adx < mem["min_adx"]: 
        return None, 0, "", "🛡 Тренд слаб"

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
    
    is_4h = (interval == "4h")
    required_neighbors = 2 if is_4h else 3
    
    is_bullish_trend = curr_p > ema200
    signal = None
    
    if ups >= required_neighbors and (curr_p > ema200 if is_4h else (macro_trend != "BEARISH" and is_bullish_trend)):
        if not is_4h and (rsi > 70 or curr_p >= upper_3sigma):
            return None, 0, "", "🛡 Экстремум 3-Сигм / RSI перегрет"
        signal = "LONG"
        avg_move = sum(m for d, o, m in top_neighbors if o == 1) / ups
        
    elif downs >= required_neighbors and (curr_p < ema200 if is_4h else (macro_trend != "BULLISH" and not is_bullish_trend)):
        if not is_4h and (rsi < 30 or curr_p <= lower_3sigma):
            return None, 0, "", "🛡 Экстремум 3-Сигм / RSI перепродан"
        signal = "SHORT"
        avg_move = sum(m for d, o, m in top_neighbors if o == -1) / downs

    if signal:
        if interval == "1h":
            if signal == "LONG" and curr_p < ema200:
                return None, 0, "", "🛡 Локальный тренд 1h против макро-тренда"
            if signal == "SHORT" and curr_p > ema200:
                return None, 0, "", "🛡 Локальный тренд 1h против макро-тренда"

        if not is_4h:
            if not check_order_book(symbol, signal):
                return None, 0, "", "🛡 Стакан против сделки (Дисбаланс)"

            if interval != "15m":
                ai_approved = ask_ai_oracle(symbol, signal, curr_p, rsi, adx, closes_list)
                if not ai_approved:
                    return None, 0, "", "🛡 Отклонено ИИ-Оракулом"
        
        conviction = "⚡️ 4H АКТИВНЫЙ" if is_4h else ("🔥 ВЫСОКАЯ (Риск 2.0%)" if (ups == 5 if signal == "LONG" else downs == 5) else "⚡️ СРЕДНЯЯ (Риск 1.0%)")
        return signal, max(1.5, avg_move), conviction, ""
        
    return None, 0, "", ""

# --- НОВОЕ ПРОДВИНУТОЕ МЕНЮ ---
def get_main_keyboard():
    scalp_status = "🟢 ВКЛ" if SCALP_ENABLED else "🔴 ВЫКЛ"
    signals_status = "🟢 АКТИВЕН" if SIGNALS_ENABLED else "🔴 ПАУЗА"
    return {
        "inline_keyboard": [
            [{"text": "📈 ОТКРЫТЫЕ СДЕЛКИ (Live)", "callback_data": "SHOW_LIVE_TRADES"}],
            [{"text": "📊 ОБЩИЙ PNL И АНАЛИТИКА", "callback_data": "SHOW_STATS"}],
            [{"text": "📜 ИСТОРИЯ И КУЛДАУНЫ", "callback_data": "SHOW_HISTORY"}],
            [{"text": "🧠 СТАТУС ОБУЧЕНИЯ ИИ", "callback_data": "SHOW_AI_MEMORY"}],
            [{"text": f"📡 СИГНАЛЫ: {signals_status}", "callback_data": "TOGGLE_SIGNALS"},
             {"text": f"⏱ СКАЛЬП: {scalp_status}", "callback_data": "TOGGLE_SCALP"}]
        ]
    }

# --- БЫСТРЫЙ ПОТОК КОНТРОЛЯ СДЕЛОК В РЕАЛЬНОМ ВРЕМЕНИ ---
def trade_monitor():
    print("Trade Monitor Thread Online...")
    while True:
        try:
            for key, trade in list(active_trades.items()):
                if time.time() - trade.get("timestamp", time.time()) > 43200:
                    del active_trades[key]
                    save_active_trades() 
                    continue

                symbol = trade["symbol"]
                price = get_current_price(symbol)
                if not price: continue
                
                candles_1m = get_data(symbol, "1m", 3)
                curr_high = max(price, float(candles_1m[-1][2])) if candles_1m else price
                curr_low = min(price, float(candles_1m[-1][3])) if candles_1m else price

                hit_result = None
                tp1_just_hit = False
                
                if trade["signal"] == "LONG":
                    if not trade["tp1_hit"] and (price >= trade["tp1"] or curr_high >= trade["tp1"]):
                        trade["tp1_hit"] = True
                        trade["sl"] = trade["entry"]
                        tp1_just_hit = True
                        
                    if price <= trade["sl"] or curr_low <= trade["sl"]:
                        hit_result = "BE" if trade["tp1_hit"] else "SL"
                    elif price >= trade["tp2"] or curr_high >= trade["tp2"]:
                        hit_result = "TP2"
                else:  # SHORT
                    if not trade["tp1_hit"] and (price <= trade["tp1"] or curr_low <= trade["tp1"]):
                        trade["tp1_hit"] = True
                        trade["sl"] = trade["entry"]
                        tp1_just_hit = True
                        
                    if price >= trade["sl"] or curr_high >= trade["sl"]:
                        hit_result = "BE" if trade["tp1_hit"] else "SL"
                    elif price <= trade["tp2"] or curr_low <= trade["tp2"]:
                        hit_result = "TP2"
                
                if tp1_just_hit and not hit_result:
                    new_msg = trade["original_msg"].replace("🤖 **AI ALERT", f"🟡 **[{trade['label_name']} | TP1 ВЗЯТ]")
                    trade["original_msg"] = new_msg
                    save_active_trades() 
                    for chat_id, msg_id in trade["messages"]:
                        edit_msg(chat_id, msg_id, new_msg)

                if hit_result:
                    close_price = trade["tp2"] if hit_result == "TP2" else (trade["entry"] if hit_result == "BE" else trade["sl"])
                    
                    if hit_result == "SL":
                        set_cooldown(trade["symbol"], trade["interval_name"], 6 * 3600)
                    
                    pnl_percent = 0.0
                    if trade["signal"] == "LONG":
                        pnl_percent = ((close_price - trade["entry"]) / trade["entry"]) * 100
                    else:
                        pnl_percent = ((trade["entry"] - close_price) / trade["entry"]) * 100
                        
                    save_local_stat(trade["label_name"], hit_result, pnl_percent)
                    update_memory(trade["symbol"], trade["interval_name"], hit_result)
                    
                    # Запись в Ledger
                    icon = "✅" if hit_result == "TP2" else "⚖️" if hit_result == "BE" else "❌"
                    ledger_entry = f"{icon} {trade['symbol']} ({trade['interval_name']}) | {hit_result} | {pnl_percent:+.2f}%"
                    add_to_ledger(ledger_entry)
                    
                    header = f"✅ **[{trade['label_name']} | ТЕЙК 2 ВЗЯТ]" if hit_result == "TP2" else (f"⚖️ **[{trade['label_name']} | БЕЗУБЫТОК]" if hit_result == "BE" else f"❌ **[{trade['label_name']} | СТОП-ЛОСС]")
                    updated_msg = trade["original_msg"].replace("🤖 **AI ALERT", header).replace(f"🟡 **[{trade['label_name']} | TP1 ВЗЯТ]", header)
                    updated_msg += f"\n\n**Итог сделки:**\nПричина: {hit_result}\nЦена закрытия: `{close_price:.4f}`\nДвижение: **{pnl_percent:+.2f}%**"
                    
                    for chat_id, msg_id in trade["messages"]:
                        edit_msg(chat_id, msg_id, updated_msg)
                        
                    del active_trades[key]
                    save_active_trades() 
                    
        except Exception as e:
            print(f"Trade monitor error: {e}")
        
        time.sleep(2)

# --- ПОТОК СКАНИРОВАНИЯ РЫНКА ---
def scan_timeframe(interval_name, label_name, cooldown_sec):
    print(f"Scanner thread started for {interval_name} ({label_name})...")
    last_alerts = {}
    
    while True:
        try:
            global SIGNALS_ENABLED
            if not SIGNALS_ENABLED:
                time.sleep(10)
                continue

            if interval_name == "15m" and not SCALP_ENABLED:
                time.sleep(10)
                continue

            open_for_this_tf = sum(1 for t in active_trades.values() if t.get("interval_name") == interval_name)
            if open_for_this_tf >= MAX_TRADES_PER_TF:
                time.sleep(10)
                continue

            macro_trend = get_macro_trend()
            now = time.time()
            
            for symbol in SYMBOLS:
                if sum(1 for t in active_trades.values() if t.get("interval_name") == interval_name) >= MAX_TRADES_PER_TF:
                    break
                    
                if interval_name == "15m" and not SCALP_ENABLED:
                    break

                trade_key = f"{symbol}_{interval_name}"
                
                if trade_key in active_trades:
                    continue
                    
                if is_on_cooldown(symbol, interval_name):
                    continue
                
                if now - last_alerts.get(symbol, 0) < cooldown_sec: continue
                    
                candles = get_data(symbol, interval_name, 300)
                if not candles or len(candles) < 100: continue
                
                current_idx = len(candles) - 2 if interval_name in ["1h", "4h"] else len(candles) - 1
                
                highs = [float(c[2]) for c in candles]
                lows = [float(c[3]) for c in candles]
                closes = [float(c[4]) for c in candles]
                
                curr_p = closes[current_idx]
                atr = sum([highs[j] - lows[j] for j in range(current_idx-14, current_idx)]) / 14
                
                effective_macro = "NEUTRAL" if interval_name == "15m" else macro_trend
                
                signal, tp_atr_mult, conviction, _ = predict_knn(candles, symbol, interval_name, current_idx, atr, effective_macro)
                
                if signal:
                    last_alerts[symbol] = now
                    sym_name = symbol.replace('USDT', '')
                    
                    if interval_name == "15m":
                        tag = f"#SCALP #{sym_name} #M15"
                    elif interval_name == "1h":
                        tag = f"#INTRADAY #{sym_name} #H1"
                    else:
                        tag = f"#SWING #{sym_name} #H4"
                    
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
                        f"🤖 **AI ALERT | {sym_name}/USDT**  {tag}\n"
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
                    channel_msg_id = send_to_channel(msg_text)
                    if channel_msg_id:
                        msgs.append((SIGNAL_CHANNEL_ID, channel_msg_id))
                    
                    if msgs:
                        active_trades[trade_key] = {
                            "symbol": symbol,
                            "signal": signal,
                            "label_name": label_name,
                            "interval_name": interval_name,
                            "entry": curr_p,
                            "sl": sl,
                            "tp1": tp1,
                            "tp2": tp2,
                            "tp1_hit": False,
                            "messages": msgs,
                            "original_msg": msg_text,
                            "timestamp": time.time()
                        }
                        save_active_trades() 
                        
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Scanner error ({interval_name}): {e}")
            time.sleep(60)

# --- ТЕЛЕГРАМ ДВИЖОК И UI ---
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
                chat_id = message_id = data = callback_query_id = None
                
                if "callback_query" in u:
                    chat_id = u["callback_query"]["message"]["chat"]["id"]
                    message_id = u["callback_query"]["message"]["message_id"]
                    data = u["callback_query"]["data"]
                    callback_query_id = u["callback_query"]["id"]
                elif "message" in u:
                    chat_id = u["message"]["chat"]["id"]
                
                if chat_id:
                    if int(chat_id) not in ADMIN_CHAT_IDS: continue 
                    active_chats.add(chat_id)
                    
                    if data == "TOGGLE_SIGNALS":
                        global SIGNALS_ENABLED
                        SIGNALS_ENABLED = not SIGNALS_ENABLED
                        status_text = "🟢 Поиск сигналов АКТИВИРОВАН!" if SIGNALS_ENABLED else "🔴 Поиск сигналов ОСТАНОВЛЕН (Пауза)."
                        edit_msg(chat_id, message_id, f"{status_text}\n\nВыбери действие 👇", get_main_keyboard())
                        
                    elif data == "SHOW_LIVE_TRADES":
                        if not active_trades:
                            msg = "📭 **ОТКРЫТЫХ СДЕЛОК НЕТ**\nБот ждет подходящих рыночных условий."
                        else:
                            msg = "📈 **ОТКРЫТЫЕ СДЕЛКИ (Live PnL)**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
                            for key, t in active_trades.items():
                                cp = get_current_price(t['symbol'])
                                pnl = 0.0
                                if cp:
                                    if t['signal'] == 'LONG':
                                        pnl = ((cp - t['entry']) / t['entry']) * 100
                                    else:
                                        pnl = ((t['entry'] - cp) / t['entry']) * 100
                                
                                emo = "🟢" if t['signal'] == "LONG" else "🔴"
                                msg += f"**{t['label_name']} | {t['symbol']}**\n"
                                msg += f"Направление: {emo} {t['signal']}\n"
                                msg += f"Вход: `{t['entry']:.4f}` | Текущая: `{cp if cp else 'N/A'}`\n"
                                msg += f"Live PnL: **{pnl:+.2f}%**\n\n"
                        edit_msg(chat_id, message_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_HISTORY":
                        ledger = get_ledger()
                        hist_txt = "\n".join([f"{i+1}. {x}" for i, x in enumerate(ledger)]) if ledger else "Пока нет закрытых сделок."
                        
                        cd_txt = ""
                        now = time.time()
                        for k, v in list(cooldowns.items()):
                            if v > now:
                                rem_m = int((v - now) // 60)
                                h, m = rem_m // 60, rem_m % 60
                                cd_txt += f"🩸 `{k}` — Остывает: {h}ч {m}м\n"
                        if not cd_txt: cd_txt = "Все монеты торгуются свободно."
                        
                        msg = f"📜 **ИСТОРИЯ (Последние 10 сделок)**\n➖➖➖➖➖➖➖➖➖➖➖➖\n{hist_txt}\n\n🛑 **РАДАР КУЛДАУНОВ**\n➖➖➖➖➖➖➖➖➖➖➖➖\n{cd_txt}"
                        edit_msg(chat_id, message_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_AI_MEMORY":
                        mem = {}
                        if os.path.exists(MEM_FILE):
                            try:
                                with open(MEM_FILE, 'r') as f: mem = json.load(f)
                            except: pass
                        
                        mem_txt = ""
                        for k, v in mem.items():
                            adx = v.get("min_adx", 10)
                            status = "Базовый" if adx <= 10 else "Повышенный" if adx <= 15 else "Жесткий"
                            mem_txt += f"🔹 `{k}`: ADX **{adx}** ({status})\n"
                        if not mem_txt: mem_txt = "Бот работает на базовых настройках (ADX: 10)."
                        
                        msg = f"🧠 **СТАТУС ОБУЧЕНИЯ ИИ (Адаптация)**\n➖➖➖➖➖➖➖➖➖➖➖➖\n{mem_txt}\n\n💡 *Бот повышает требования к тренду (ADX) после убытков, чтобы защитить капитал.*"
                        edit_msg(chat_id, message_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_STATS":
                        edit_msg(chat_id, message_id, generate_local_report(), get_main_keyboard())
                        
                    elif data == "TOGGLE_SCALP":
                        global SCALP_ENABLED
                        SCALP_ENABLED = not SCALP_ENABLED
                        status_text = "🟢 Скальпинг (15m) активирован!" if SCALP_ENABLED else "🔴 Скальпинг (15m) отключен."
                        edit_msg(chat_id, message_id, f"{status_text}\n\nВыбери действие 👇", get_main_keyboard())
                        
                    elif "message" in u and "text" in u["message"]:
                        send_msg(chat_id, "🚀 **ГЛАВНЫЙ ТЕРМИНАЛ**\nВыбери нужный раздел аналитики 👇", get_main_keyboard())
                        
                    if callback_query_id:
                        try:
                            ans_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
                            ans_data = json.dumps({"callback_query_id": callback_query_id}).encode('utf-8')
                            urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={'Content-Type': 'application/json'}), context=context, timeout=5)
                        except: pass
                        
            time.sleep(1)
        except Exception as e:
            print(f"Engine error: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "AI Trading Bot Active (Professional Dashboard Mode)"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=trade_monitor, daemon=True).start()
    
    threading.Thread(target=scan_timeframe, args=("15m", "⏱ СКАЛЬПИНГ", 900), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ", 3600), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("4h", "🌊 СВИНГ", 14400), daemon=True).start()
    
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
