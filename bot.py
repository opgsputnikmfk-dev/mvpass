from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
import math
from datetime import datetime
import pandas as pd
import numpy as np

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_IDS = {8299008675}
SIGNAL_CHANNEL_ID = os.getenv("SIGNAL_CHANNEL_ID")

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()

MEM_FILE = "bot_memory.json"
ACTIVE_TRADES_FILE = "active_trades_memory.json"
STATS_FILE = "bot_stats.json"
COOLDOWNS_FILE = "cooldowns_memory.json"
LEDGER_FILE = "bot_ledger.json"
REJECT_STATS_FILE = "reject_stats.json"  

SCAN_INTERVAL = 60

# --- ПАРАМЕТРЫ KNN ---
NEIGHBORS = 15                    
HISTORY_LIMIT = 700               
MIN_NEIGHBOR_VOTES_RATIO = 0.4    
FEATURE_WEIGHTS = [1.0, 1.0, 0.3, 0.6, 0.4]  

# --- ГЛОБАЛЬНЫЕ ПЕРЕКЛЮЧАТЕЛИ И ЛИМИТЫ ---
SIGNALS_ENABLED = True
SCALP_ENABLED = True
MAX_TRADES_PER_TF = 3
MAX_SAME_DIRECTION_PER_TF = 2     

# --- РИСК-МЕНЕДЖМЕНТ ---
SL_ATR_MULT = 1.5
TP1_ATR_MULT = 1.5                
FEE_SLIPPAGE_PCT = 0.12           

# --- ПОТОКОБЕЗОПАСНОСТЬ ---
active_trades_lock = threading.Lock()
cooldowns_lock = threading.Lock()
rejects_lock = threading.Lock() 

# --- СТАТИСТИКА ОТКАЗОВ (ТЕЛЕМЕТРИЯ) ---
reject_stats = {}

def load_rejects():
    global reject_stats
    if os.path.exists(REJECT_STATS_FILE):
        try:
            with open(REJECT_STATS_FILE, 'r') as f:
                reject_stats = json.load(f)
        except: pass

load_rejects()

def log_reject(interval, reason):
    if not reason: return
    clean_reason = reason.replace("🛡 ", "").replace("🛡", "").strip()
    
    with rejects_lock:
        if interval not in reject_stats:
            reject_stats[interval] = {}
        reject_stats[interval][clean_reason] = reject_stats[interval].get(clean_reason, 0) + 1
        try:
            with open(REJECT_STATS_FILE, 'w') as f:
                json.dump(reject_stats, f)
        except: pass

def generate_reject_report():
    if not reject_stats:
        return "📭 Статистика отказов пока пуста. Бот только начал сканирование."
    
    report = "🚫 **СТАТИСТИКА ОТКАЗОВ (Фильтры)**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
    for tf in ["15m", "1h", "4h"]:
        if tf in reject_stats and reject_stats[tf]:
            report += f"**Таймфрейм: {tf}**\n"
            sorted_reasons = sorted(reject_stats[tf].items(), key=lambda x: x[1], reverse=True)
            for reason, count in sorted_reasons:
                report += f"• {reason}: `{count}`\n"
            report += "\n"
    
    report += "💡 *Помогает понять, какой фильтр срезает больше всего сделок.*"
    return report.strip()

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
    ledger = ledger[:10]
    try:
        with open(LEDGER_FILE, 'w') as f: json.dump(ledger, f)
    except: pass

# --- СИСТЕМА КУЛДАУНОВ ---
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
    with cooldowns_lock:
        key = f"{symbol}_{interval}"
        cooldowns[key] = time.time() + duration_sec
        save_cooldowns()

def is_on_cooldown(symbol, interval):
    with cooldowns_lock:
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
        stats[tf_label] = {"TP2": 0, "BE": 0, "SL": 0, "TIMEOUT": 0, "total_pnl": 0.0, "streak": 0}
    if "TIMEOUT" not in stats[tf_label]:
        stats[tf_label]["TIMEOUT"] = 0

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

    report = "📊 **ОБЩИЙ PNL И АНАЛИТИКА** (после комиссий)\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
    total_pnl_all = 0.0

    for tf in ["⏱ СКАЛЬПИНГ", "⚡️ ИНТРАДЕЙ", "🌊 СВИНГ"]:
        if tf not in stats:
            continue

        data = stats[tf]
        tp2 = data.get('TP2', 0)
        be = data.get('BE', 0)
        sl = data.get('SL', 0)
        timeout = data.get('TIMEOUT', 0)
        pnl = data.get('total_pnl', 0.0)
        streak = data.get('streak', 0)

        total = tp2 + be + sl + timeout
        if total == 0: continue

        total_pnl_all += pnl
        valid_trades = tp2 + sl
        wr = (tp2 / valid_trades * 100) if valid_trades > 0 else 0

        if streak > 0:
            streak_str = f"🔥 {streak} в плюс"
        elif streak < 0:
            streak_str = f"🩸 {abs(streak)} в минус"
        else:
            streak_str = "➖"

        report += f"**{tf}**\n"
        report += f"📈 Сделок: {total} (Тейки: {tp2} | БУ: {be} | Стопы: {sl} | Таймауты: {timeout})\n"
        report += f"🎯 Winrate (TP2 vs SL): **{wr:.1f}%**\n"
        report += f"💰 Реальный PnL: **{pnl:+.2f}%**\n"
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

# --- ФИЛЬТРЫ И АНАЛИТИКА ---
def get_memory(symbol, interval):
    key = f"{symbol}_{interval}"
    if not os.path.exists(MEM_FILE): return {"min_adx": 15}
    try:
        with open(MEM_FILE, 'r') as f: mem = json.load(f)
        return mem.get(key, {"min_adx": 15})
    except:
        return {"min_adx": 15}

def update_memory(symbol, interval, reason):
    key = f"{symbol}_{interval}"
    if reason in ['BE', 'SL']:
        try:
            mem = {}
            if os.path.exists(MEM_FILE):
                with open(MEM_FILE, 'r') as f: mem = json.load(f)
            data = mem.get(key, {"min_adx": 15})
            data["min_adx"] = min(40, data["min_adx"] + 1)
            mem[key] = data
            with open(MEM_FILE, 'w') as f: json.dump(mem, f)
        except Exception as e:
            print(f"Memory update error: {e}")

def check_order_book(symbol, signal_type):
    url = f"https://data-api.binance.vision/api/v3/depth?symbol={symbol}&limit=20"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=5) as r:
            depth = json.loads(r.read().decode())

        bids = sum([float(item[1]) for item in depth.get("bids", [])])
        asks = sum([float(item[1]) for item in depth.get("asks", [])])

        if asks == 0 or bids == 0: return True
        ratio = bids / asks

        if signal_type == "LONG":
            return ratio >= 1.05   
        else:
            return ratio <= 0.95   
    except Exception as e:
        print(f"Order Book Error for {symbol}: {e}")
        return True

def build_feature_frame(candles):
    cols = ['ts', 'open', 'high', 'low', 'close', 'vol', 'i1', 'i2', 'i3', 'i4', 'i5', 'i6']
    df = pd.DataFrame(candles, columns=cols)
    for c in ['open', 'high', 'low', 'close', 'vol']:
        df[c] = df[c].astype(float)

    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ema_gain = gain.ewm(com=13, adjust=False).mean()
    ema_loss = loss.ewm(com=13, adjust=False).mean()
    rs = ema_gain / ema_loss
    df['rsi'] = 100 - (100 / (1 + rs))

    up_move = df['high'].diff()
    down_move = -df['low'].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs()
    ], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1/14, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr14
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr14
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    df['adx'] = dx.ewm(alpha=1/14, adjust=False).mean()
    df['atr14'] = atr14

    candle_range_pct = (df['high'] - df['low']) / df['close'] * 100
    avg_range_50 = candle_range_pct.shift(1).rolling(50).mean()
    df['is_anomaly'] = (candle_range_pct / avg_range_50) > 2.5

    sma20 = df['close'].rolling(20).mean()
    std20 = df['close'].rolling(20).std()
    df['upper_3sigma'] = sma20 + 3.0 * std20
    df['lower_3sigma'] = sma20 - 3.0 * std20

    avg_vol_20 = df['vol'].rolling(20).mean()
    df['avg_vol20'] = avg_vol_20.bfill()
    df['vol_climax'] = (df['vol'] / avg_vol_20) > 4.5

    return df

def get_macro_trend():
    btc_1d = get_data("BTCUSDT", "1d", 50)
    if not btc_1d: return "NEUTRAL"
    closes = [float(c[4]) for c in btc_1d]
    sma20 = sum(closes[-20:]) / 20
    return "BULLISH" if closes[-1] > sma20 else ("BEARISH" if closes[-1] < sma20 else "NEUTRAL")

def calculate_distance(f1, f2, weights):
    return math.sqrt(sum(w * (a - b) ** 2 for w, a, b in zip(weights, f1, f2)))

def predict_knn(df, symbol, interval, current_idx, atr, macro_trend):
    row = df.iloc[current_idx]
    ema200 = row['ema200']; adx = row['adx']; rsi = row['rsi']
    is_anomaly = bool(row['is_anomaly']); is_vol_climax = bool(row['vol_climax'])
    upper_3sigma = row['upper_3sigma']; lower_3sigma = row['lower_3sigma']
    curr_p = row['close']

    mem = get_memory(symbol, interval)

    if is_anomaly or is_vol_climax:
        return None, 0, "", "🛡 Аномалия / Кульминация объема"

    if pd.isna(adx) or adx < mem["min_adx"]:
        return None, 0, "", "🛡 Тренд слаб (ADX)"

    closes = df['close'].values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    vols = df['vol'].values
    avg_vol20 = df['avg_vol20'].values
    ema200_arr = df['ema200'].values
    rsi_arr = df['rsi'].values
    atr_arr = df['atr14'].values

    def feature_vec(i):
        a = atr_arr[i] if not np.isnan(atr_arr[i]) and atr_arr[i] > 0 else atr
        return [
            (closes[i] - opens[i]) / a,
            (highs[i] - lows[i]) / a,
            vols[i] / avg_vol20[i] if avg_vol20[i] else 1.0,
            (closes[i] - ema200_arr[i]) / a if not np.isnan(ema200_arr[i]) else 0.0,
            rsi_arr[i] / 100 if not np.isnan(rsi_arr[i]) else 0.5,
        ]

    curr_fp = feature_vec(current_idx)

    distances = []
    
    # --- ИСПРАВЛЕННАЯ СИМУЛЯЦИЯ (PATH-DEPENDENT) ---
    lookahead = 20
    
    for hist_i in range(210, current_idx - 10):
        if np.isnan(atr_arr[hist_i]) or atr_arr[hist_i] == 0:
            continue
            
        h_fp = feature_vec(hist_i)
        dist = calculate_distance(curr_fp, h_fp, FEATURE_WEIGHTS)
        h_atr = atr_arr[hist_i]
        
        target_move = h_atr * SL_ATR_MULT 
        outcome = 0
        actual_move = 0.0
        
        end_idx = min(hist_i + lookahead, len(closes))
        
        for j in range(hist_i + 1, end_idx):
            hit_up = highs[j] >= closes[hist_i] + target_move
            hit_down = lows[j] <= closes[hist_i] - target_move
            
            if hit_up and not hit_down:
                outcome = 1
                actual_move = (highs[j] - closes[hist_i]) / h_atr
                break
            elif hit_down and not hit_up:
                outcome = -1
                actual_move = (closes[hist_i] - lows[j]) / h_atr
                break
            elif hit_up and hit_down:
                outcome = 0  
                break
                
        if outcome == 0:
            future_move = closes[end_idx - 1] - closes[hist_i]
            if future_move > h_atr * 0.5:
                outcome = 1
                actual_move = abs(future_move) / h_atr
            elif future_move < -h_atr * 0.5:
                outcome = -1
                actual_move = abs(future_move) / h_atr
                
        distances.append((dist, outcome, actual_move))

    if len(distances) < NEIGHBORS:
        return None, 0, "", "🛡 Недостаточно исторических данных"

    distances.sort(key=lambda x: x[0])
    top_neighbors = distances[:NEIGHBORS]

    weights = [1.0 / (d + 1e-6) for d, o, m in top_neighbors]
    total_w = sum(weights)
    up_w = sum(w for w, (d, o, m) in zip(weights, top_neighbors) if o == 1)
    down_w = sum(w for w, (d, o, m) in zip(weights, top_neighbors) if o == -1)
    up_ratio = up_w / total_w
    down_ratio = down_w / total_w

    ups = sum(1 for d, o, m in top_neighbors if o == 1)
    downs = sum(1 for d, o, m in top_neighbors if o == -1)
    min_votes = max(2, int(NEIGHBORS * MIN_NEIGHBOR_VOTES_RATIO))

    is_4h = (interval == "4h")
    is_bullish_trend = curr_p > ema200
    signal = None

    if up_ratio >= 0.55 and ups >= min_votes and (curr_p > ema200 if is_4h else (macro_trend != "BEARISH" and is_bullish_trend)):
        if not is_4h and (rsi > 70 or curr_p >= upper_3sigma):
            return None, 0, "", "🛡 Экстремум 3-Сигм / RSI перегрет"
        signal = "LONG"
        avg_move = sum(m for d, o, m in top_neighbors if o == 1) / ups

    elif down_ratio >= 0.55 and downs >= min_votes and (curr_p < ema200 if is_4h else (macro_trend != "BULLISH" and not is_bullish_trend)):
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

        conviction = "⚡️ 4H АКТИВНЫЙ" if is_4h else ("🔥 ВЫСОКАЯ (Риск 2.0%)" if max(up_ratio, down_ratio) >= 0.75 else "⚡️ СРЕДНЯЯ (Риск 1.0%)")
        return signal, max(1.5, avg_move), conviction, ""

    return None, 0, "", "🛡 Нет уверенного паттерна (KNN)"

# --- МЕНЮ ---
def get_main_keyboard():
    scalp_status = "🟢 ВКЛ" if SCALP_ENABLED else "🔴 ВЫКЛ"
    signals_status = "🟢 АКТИВЕН" if SIGNALS_ENABLED else "🔴 ПАУЗА"
    return {
        "inline_keyboard": [
            [{"text": "📈 ОТКРЫТЫЕ СДЕЛКИ (Live)", "callback_data": "SHOW_LIVE_TRADES"}],
            [{"text": "📊 PNL И АНАЛИТИКА", "callback_data": "SHOW_STATS"},
             {"text": "🚫 ОТКАЗЫ ФИЛЬТРОВ", "callback_data": "SHOW_REJECTS"}],
            [{"text": "📜 ИСТОРИЯ И КУЛДАУНЫ", "callback_data": "SHOW_HISTORY"}],
            [{"text": "🧠 СТАТУС АДАПТАЦИИ (ADX)", "callback_data": "SHOW_AI_MEMORY"}],
            [{"text": f"📡 СИГНАЛЫ: {signals_status}", "callback_data": "TOGGLE_SIGNALS"},
             {"text": f"⏱ СКАЛЬП: {scalp_status}", "callback_data": "TOGGLE_SCALP"}]
        ]
    }

# --- ПОТОК КОНТРОЛЯ СДЕЛОК ---
def trade_monitor():
    print("Trade Monitor Thread Online...")
    
    # Динамические таймауты (каждой сделке дается 48 свечей на отработку)
    TIMEOUTS = {
        "15m": 43200,   # 12 часов
        "1h": 172800,   # 48 часов (2 суток)
        "4h": 691200    # 192 часа (8 суток)
    }

    while True:
        try:
            with active_trades_lock:
                items_snapshot = list(active_trades.items())

            for key, trade in items_snapshot:
                if key not in active_trades:
                    continue

                # Берем нужный таймаут в зависимости от таймфрейма
                interval = trade.get("interval_name", "15m")
                max_duration = TIMEOUTS.get(interval, 43200)

                if time.time() - trade.get("timestamp", time.time()) > max_duration:
                    price = get_current_price(trade["symbol"]) or trade["entry"]
                    if trade["signal"] == "LONG":
                        pnl_percent = ((price - trade["entry"]) / trade["entry"]) * 100
                    else:
                        pnl_percent = ((trade["entry"] - price) / trade["entry"]) * 100
                    pnl_percent -= FEE_SLIPPAGE_PCT

                    save_local_stat(trade["label_name"], "TIMEOUT", pnl_percent)
                    ledger_entry = f"⌛ {trade['symbol']} ({trade['interval_name']}) | TIMEOUT | {pnl_percent:+.2f}%"
                    add_to_ledger(ledger_entry)

                    timeout_hours = max_duration // 3600
                    updated_msg = trade["original_msg"] + (
                        f"\n\n**Итог сделки:**\nПричина: TIMEOUT "
                        f"(сигнал не закрылся за {timeout_hours}ч)\n"
                        f"Цена на момент закрытия: `{price:.4f}`\n"
                        f"Результат (с учетом комиссии): **{pnl_percent:+.2f}%**"
                    )
                    for chat_id, msg_id in trade["messages"]:
                        edit_msg(chat_id, msg_id, updated_msg)

                    with active_trades_lock:
                        if key in active_trades:
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
                    new_msg = trade["original_msg"].replace("🤖 **АЛГО АЛЕРТ", f"🟡 **[{trade['label_name']} | TP1 ВЗЯТ]")
                    trade["original_msg"] = new_msg
                    with active_trades_lock:
                        if key in active_trades:
                            active_trades[key] = trade
                            save_active_trades()
                    for chat_id, msg_id in trade["messages"]:
                        edit_msg(chat_id, msg_id, new_msg)

                if hit_result:
                    close_price = trade["tp2"] if hit_result == "TP2" else (trade["entry"] if hit_result == "BE" else trade["sl"])

                    if hit_result == "SL":
                        set_cooldown(trade["symbol"], trade["interval_name"], 6 * 3600)

                    if trade["signal"] == "LONG":
                        pnl_percent = ((close_price - trade["entry"]) / trade["entry"]) * 100
                    else:
                        pnl_percent = ((trade["entry"] - close_price) / trade["entry"]) * 100
                    pnl_percent -= FEE_SLIPPAGE_PCT

                    save_local_stat(trade["label_name"], hit_result, pnl_percent)
                    update_memory(trade["symbol"], trade["interval_name"], hit_result)

                    icon = "✅" if hit_result == "TP2" else "⚖️" if hit_result == "BE" else "❌"
                    ledger_entry = f"{icon} {trade['symbol']} ({trade['interval_name']}) | {hit_result} | {pnl_percent:+.2f}%"
                    add_to_ledger(ledger_entry)

                    header = f"✅ **[{trade['label_name']} | ТЕЙК 2 ВЗЯТ]" if hit_result == "TP2" else (f"⚖️ **[{trade['label_name']} | БЕЗУБЫТОК]" if hit_result == "BE" else f"❌ **[{trade['label_name']} | СТОП-ЛОСС]")
                    updated_msg = trade["original_msg"].replace("🤖 **АЛГО АЛЕРТ", header).replace(f"🟡 **[{trade['label_name']} | TP1 ВЗЯТ]", header)
                    updated_msg += f"\n\n**Итог сделки:**\nПричина: {hit_result}\nЦена закрытия: `{close_price:.4f}`\nРезультат (с учетом комиссии): **{pnl_percent:+.2f}%**"

                    for chat_id, msg_id in trade["messages"]:
                        edit_msg(chat_id, msg_id, updated_msg)

                    with active_trades_lock:
                        if key in active_trades:
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

            with active_trades_lock:
                open_for_this_tf = sum(1 for t in active_trades.values() if t.get("interval_name") == interval_name)
            if open_for_this_tf >= MAX_TRADES_PER_TF:
                time.sleep(10)
                continue

            macro_trend = get_macro_trend()
            now = time.time()

            for symbol in SYMBOLS:
                with active_trades_lock:
                    open_for_this_tf = sum(1 for t in active_trades.values() if t.get("interval_name") == interval_name)
                if open_for_this_tf >= MAX_TRADES_PER_TF:
                    break

                if interval_name == "15m" and not SCALP_ENABLED:
                    break

                trade_key = f"{symbol}_{interval_name}"

                with active_trades_lock:
                    already_open = trade_key in active_trades
                if already_open:
                    continue

                if is_on_cooldown(symbol, interval_name):
                    continue

                if now - last_alerts.get(symbol, 0) < cooldown_sec: continue

                candles = get_data(symbol, interval_name, HISTORY_LIMIT)
                if not candles or len(candles) < 250: continue

                df = build_feature_frame(candles)
                current_idx = len(df) - 2 if interval_name in ["1h", "4h"] else len(df) - 1

                atr = df['atr14'].iloc[current_idx]
                if pd.isna(atr) or atr <= 0:
                    continue

                curr_p = df['close'].iloc[current_idx]
                effective_macro = "NEUTRAL" if interval_name == "15m" else macro_trend

                signal, tp_atr_mult, conviction, reject_reason = predict_knn(df, symbol, interval_name, current_idx, atr, effective_macro)

                if not signal and reject_reason:
                    log_reject(interval_name, reject_reason)
                    continue

                if signal:
                    with active_trades_lock:
                        same_dir_count = sum(
                            1 for t in active_trades.values()
                            if t.get("interval_name") == interval_name and t.get("signal") == signal
                        )
                    if same_dir_count >= MAX_SAME_DIRECTION_PER_TF:
                        continue

                    last_alerts[symbol] = now
                    sym_name = symbol.replace('USDT', '')

                    if interval_name == "15m":
                        tag = f"#SCALP #{sym_name} #M15"
                    elif interval_name == "1h":
                        tag = f"#INTRADAY #{sym_name} #H1"
                    else:
                        tag = f"#SWING #{sym_name} #H4"

                    sl_dist = atr * SL_ATR_MULT
                    tp1_dist = atr * TP1_ATR_MULT
                    tp2_dist = max(tp1_dist * 1.3, atr * tp_atr_mult)

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
                        f"🤖 **АЛГО АЛЕРТ | {sym_name}/USDT**  {tag}\n"
                        f"⏳ **Срок:** `{label_name}` ({interval_name})\n"
                        f"📉 **Направление:** {emo} **{signal}**\n\n"
                        f"> Сила сигнала: {conviction}\n"
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
                        with active_trades_lock:
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
                        with active_trades_lock:
                            trades_snapshot = dict(active_trades)
                        if not trades_snapshot:
                            msg = "📭 **ОТКРЫТЫХ СДЕЛОК НЕТ**\nБот ждет подходящих рыночных условий."
                        else:
                            msg = "📈 **ОТКРЫТЫЕ СДЕЛКИ (Live PnL)**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
                            for key, t in trades_snapshot.items():
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
                        with cooldowns_lock:
                            cooldowns_snapshot = dict(cooldowns)
                        for k, v in cooldowns_snapshot.items():
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
                            adx = v.get("min_adx", 15)
                            status = "Базовый" if adx <= 15 else "Повышенный" if adx <= 25 else "Жесткий"
                            mem_txt += f"🔹 `{k}`: ADX **{adx}** ({status})\n"
                        if not mem_txt: mem_txt = "Бот работает на базовых настройках (ADX: 15)."

                        msg = f"🧠 **СТАТУС АДАПТАЦИИ (ADX)**\n➖➖➖➖➖➖➖➖➖➖➖➖\n{mem_txt}\n\n💡 *Бот повышает требования к тренду (ADX) после убытков, чтобы защитить капитал.*"
                        edit_msg(chat_id, message_id, msg, get_main_keyboard())

                    elif data == "SHOW_STATS":
                        edit_msg(chat_id, message_id, generate_local_report(), get_main_keyboard())
                        
                    elif data == "SHOW_REJECTS":
                        edit_msg(chat_id, message_id, generate_reject_report(), get_main_keyboard())

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
def home(): return "Trading Bot Active (Path-Dependent KNN + Fixes)"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=trade_monitor, daemon=True).start()

    threading.Thread(target=scan_timeframe, args=("15m", "⏱ СКАЛЬПИНГ", 900), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ", 3600), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("4h", "🌊 СВИНГ", 14400), daemon=True).start()

    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
