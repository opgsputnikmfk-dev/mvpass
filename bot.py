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
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
active_trades = {}
start_time = time.time()
SCALP_ENABLED = True
SCAN_INTERVAL = 60
NEIGHBORS = 5
MEM_FILE = "bot_memory.json"

# --- БАЗА ДАННЫХ (SUPABASE) ---
def save_trade_to_db(symbol, signal, timeframe_label, reason, entry, close_price, is_news_anomaly=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Supabase credentials missing!")
        return
    
    url = f"{SUPABASE_URL}/rest/v1/trades"
    trade_data = {
        "symbol": symbol, "signal": signal, "timeframe": timeframe_label,
        "reason": reason, "entry": entry, "close_price": close_price,
        "news_anomaly": is_news_anomaly, "timestamp": time.time(),
        "date_str": datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    }
    
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    
    try:
        req = urllib.request.Request(url, data=json.dumps(trade_data).encode('utf-8'), headers=headers, method="POST")
        urllib.request.urlopen(req, context=context, timeout=10)
    except Exception as e:
        print(f"Supabase DB Save Error: {e}")

def generate_monthly_report():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return "📭 Ошибка подключения к базе данных Supabase."
    
    url = f"{SUPABASE_URL}/rest/v1/trades?select=*"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, context=context, timeout=10) as r:
            log = json.loads(r.read().decode())
            
        if not log: return "📭 База данных пуста. Сделок еще не было."
        
        current_month, current_year = datetime.utcnow().month, datetime.utcnow().year
        monthly_trades = [t for t in log if datetime.utcfromtimestamp(t['timestamp']).month == current_month and datetime.utcfromtimestamp(t['timestamp']).year == current_year]
        
        if not monthly_trades: return "📭 В текущем месяце закрытых сделок пока нет."
        
        report = "📊 **СТАТИСТИКА И САМОАНАЛИЗ ИИ (Supabase)**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
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
        return f"Ошибка отчета Supabase: {e}"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
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
        send_msg(SIGNAL_CHANNEL_ID, text)

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

# --- ИНТЕЛЛЕКТУАЛЬНЫЕ ФУНКЦИИ ---
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
    
    is_bullish_trend = curr_p > ema200
    is_4h = (interval == "4h")
    required_neighbors = 2 if is_4h else 3
    trend_allowed = True if is_4h else (macro_trend != "BEARISH" if ups >= required_neighbors else macro_trend != "BULLISH")
    
    signal = None
    if ups >= required_neighbors and (curr_p > ema200 if is_4h else (macro_trend != "BEARISH" and is_bullish_trend)):
        if not is_4h and (rsi > 70 or curr_p >= upper_3sigma): return None, 0, "", "🛡 Экстремум"
        if not is_4h and (not check_order_book(symbol, "LONG") or not ask_ai_oracle(symbol, "LONG", curr_p, rsi, adx, closes_list)): return None, 0, "", "🛡 Фильтр"
        signal = "LONG"
        avg_move = sum(m for d, o, m in top_neighbors if o == 1) / ups
    elif downs >= required_neighbors and (curr_p < ema200 if is_4h else (macro_trend != "BULLISH" and not is_bullish_trend)):
        if not is_4h and (rsi < 30 or curr_p <= lower_3sigma): return None, 0, "", "🛡 Экстремум"
        if not is_4h and (not check_order_book(symbol, "SHORT") or not ask_ai_oracle(symbol, "SHORT", curr_p, rsi, adx, closes_list)): return None, 0, "", "🛡 Фильтр"
        signal = "SHORT"
        avg_move = sum(m for d, o, m in top_neighbors if o == -1) / downs

    return signal, max(1.5, avg_move), "⚡️ 4H АКТИВНЫЙ" if is_4h else "🔥 ВЫСОКАЯ", ""

def get_main_keyboard():
    scalp_status = "🟢 ВКЛ" if SCALP_ENABLED else "🔴 ВЫКЛ"
    return {
        "inline_keyboard": [
            [{"text": "📊 СТАТИСТИКА И ПАМЯТЬ ИИ", "callback_data": "SHOW_STATS"}],
            [{"text": f"⏱ СКАЛЬПИНГ: {scalp_status}", "callback_data": "TOGGLE_SCALP"}],
            [{"text": "🪙 МОНИТОРИНГ", "callback_data": "SHOW_ASSETS"},
             {"text": "🟢 СТАТУС БОТА", "callback_data": "BOT_STATUS"}],
            [{"text": "🧠 СТРАТЕГИЯ", "callback_data": "SHOW_STRATEGY"},
             {"text": "ℹ️ СПРАВКА", "callback_data": "SHOW_HELP"}]
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
                    continue

                symbol = trade["symbol"]
                price = get_current_price(symbol)
                if not price: continue
                
                candles_1m = get_data(symbol, "1m", 3)
                curr_high = max(price, float(candles_1m[-1][2])) if candles_1m else price
                curr_low = min(price, float(candles_1m[-1][3])) if candles_1m else price

                hit_result = None
                if trade["signal"] == "LONG":
                    if not trade["tp1_hit"] and (price >= trade["tp1"] or curr_high >= trade["tp1"]):
                        trade["tp1_hit"] = True
                        trade["sl"] = trade["entry"]
                        for c, m in trade["messages"]: edit_msg(c, m, trade["original_msg"].replace("🤖 **AI ALERT", f"🟡 **[{trade['label_name']} | TP1 ВЗЯТ]"))
                    if price <= trade["sl"] or curr_low <= trade["sl"]: hit_result = "BE" if trade["tp1_hit"] else "SL"
                    elif price >= trade["tp2"] or curr_high >= trade["tp2"]: hit_result = "TP2"
                else:
                    if not trade["tp1_hit"] and (price <= trade["tp1"] or curr_low <= trade["tp1"]):
                        trade["tp1_hit"] = True
                        trade["sl"] = trade["entry"]
                        for c, m in trade["messages"]: edit_msg(c, m, trade["original_msg"].replace("🤖 **AI ALERT", f"🟡 **[{trade['label_name']} | TP1 ВЗЯТ]"))
                    if price >= trade["sl"] or curr_high >= trade["sl"]: hit_result = "BE" if trade["tp1_hit"] else "SL"
                    elif price <= trade["tp2"] or curr_low <= trade["tp2"]: hit_result = "TP2"
                
                if hit_result:
                    close_price = trade["tp2"] if hit_result == "TP2" else (trade["entry"] if hit_result == "BE" else trade["sl"])
                    save_trade_to_db(symbol, trade["signal"], trade["label_name"], hit_result, trade["entry"], close_price)
                    update_memory(trade["symbol"], trade["interval_name"], hit_result)
                    
                    header = f"✅ **[{trade['label_name']} | ТЕЙК 2 ВЗЯТ]" if hit_result == "TP2" else (f"⚖️ **[{trade['label_name']} | БЕЗУБЫТОК]" if hit_result == "BE" else f"❌ **[{trade['label_name']} | СТОП-ЛОСС]")
                    updated_msg = trade["original_msg"].replace("🤖 **AI ALERT", header).replace(f"🟡 **[{trade['label_name']} | TP1 ВЗЯТ]", header)
                    updated_msg += f"\n\n**Итог сделки:**\nПричина: {hit_result}\nЦена закрытия: `{close_price:.4f}`"
                    
                    for chat_id, msg_id in trade["messages"]:
                        edit_msg(chat_id, msg_id, updated_msg)
                        
                    del active_trades[key]
                    
        except Exception as e:
            print(f"Trade monitor error: {e}")
        time.sleep(2)

# --- ПОТОК СКАНИРОВАНИЯ РЫНКА ---
def scan_timeframe(interval_name, label_name, cooldown_sec):
    print(f"Scanner thread started for {interval_name} ({label_name})...")
    last_alerts = {}
    while True:
        try:
            if interval_name == "15m" and not SCALP_ENABLED: time.sleep(10); continue
            macro_trend = get_macro_trend()
            now = time.time()
            for symbol in SYMBOLS:
                trade_key = f"{symbol}_{interval_name}"
                if trade_key in active_trades or now - last_alerts.get(symbol, 0) < cooldown_sec: continue
                candles = get_data(symbol, interval_name, 300)
                if not candles or len(candles) < 100: continue
                current_idx = len(candles) - 1
                atr = sum([float(candles[j][2]) - float(candles[j][3]) for j in range(current_idx-14, current_idx)]) / 14
                
                signal, tp_atr_mult, conviction, _ = predict_knn(candles, symbol, interval_name, current_idx, atr, macro_trend)
                
                if signal:
                    last_alerts[symbol] = now
                    sym_name = symbol.replace('USDT', '')
                    sl_dist, tp1_dist, tp2_dist = atr * 2.0, atr * 1.0, atr * tp_atr_mult
                    curr_p = float(candles[current_idx][4])
                    sl, tp1, tp2 = (curr_p - sl_dist, curr_p + tp1_dist, curr_p + tp2_dist) if signal == "LONG" else (curr_p + sl_dist, curr_p - tp1_dist, curr_p - tp2_dist)
                    
                    msg_text = f"🤖 **AI ALERT | {sym_name}/USDT**\n⏳ {label_name}\n📉 {signal}\n🎯 TP: {tp1:.4f} / {tp2:.4f}\n🛡 SL: {sl:.4f}"
                    
                    msgs = broadcast(msg_text)
                    channel_msg_id = send_msg(SIGNAL_CHANNEL_ID, msg_text)
                    if channel_msg_id: msgs.append((SIGNAL_CHANNEL_ID, channel_msg_id))
                    
                    active_trades[trade_key] = {
                        "symbol": symbol, "signal": signal, "label_name": label_name,
                        "interval_name": interval_name, "entry": curr_p, "sl": sl,
                        "tp1": tp1, "tp2": tp2, "tp1_hit": False, "messages": msgs,
                        "original_msg": msg_text, "timestamp": time.time()
                    }
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Scanner error ({interval_name}): {e}"); time.sleep(60)

# --- ТЕЛЕГРАМ ДВИЖОК ---
def bot_engine():
    last_update_id = 0
    print("Telegram Engine Online...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            updates = json.loads(urllib.request.urlopen(url, timeout=35).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                chat = u.get("callback_query", {}).get("message", {}).get("chat", {}) or u.get("message", {}).get("chat", {})
                if chat and int(chat.get("id")) in ADMIN_CHAT_IDS:
                    active_chats.add(chat["id"])
                    data = u.get("callback_query", {}).get("data")
                    if data == "SHOW_STATS": edit_msg(chat["id"], u["callback_query"]["message"]["message_id"], generate_monthly_report(), get_main_keyboard())
                    elif data == "BOT_STATUS": edit_msg(chat["id"], u["callback_query"]["message"]["message_id"], "🟢 Бот работает стабильно.", get_main_keyboard())
                    elif "message" in u: send_msg(chat["id"], "🚀 Терминал активен", get_main_keyboard())
        except: time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=trade_monitor, daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("15m", "⏱ СКАЛЬПИНГ", 900), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ", 3600), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("4h", "🌊 СВИНГ", 14400), daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
