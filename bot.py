from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
import math
from datetime import datetime

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = 8299008675  # Твой уникальный ID доступа

context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()
DB_FILE = "trades_log.json"

SCAN_INTERVAL = 300 
NEIGHBORS = 5

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
        
        total = len(monthly_trades)
        wins = sum(1 for t in monthly_trades if t['reason'] == 'TP2')
        be = sum(1 for t in monthly_trades if t['reason'] == 'BE')
        losses = sum(1 for t in monthly_trades if t['reason'] == 'SL')
        
        winrate = (wins / (total - be) * 100) if (total - be) > 0 else 0
        
        report = (
            f"📊 **АВТО-ОТЧЕТ ЗА ТЕКУЩИЙ МЕСЯЦ**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"Всего закрыто: **{total}**\n"
            f"✅ **Тейки:** {wins} | ⚖️ **БУ:** {be} | ❌ **Стопы:** {losses}\n\n"
            f"🎯 **Чистый Winrate:** {winrate:.1f}%\n"
        )
        return report
    except Exception as e:
        return f"Ошибка отчета: {e}"

# --- МАТЕМАТИКА ИИ ---
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

def predict_knn(candles, current_idx, atr, macro_trend):
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    
    avg_v = sum(volumes[current_idx-20:current_idx])/20
    curr_fp = [(closes[current_idx]-opens[current_idx])/atr, (highs[current_idx]-lows[current_idx])/atr, volumes[current_idx]/avg_v]
    
    distances = []
    for hist_i in range(25, current_idx - 10):
        h_atr = sum([highs[j]-lows[j] for j in range(hist_i-14, hist_i)])/14
        if h_atr == 0: continue
        avg_hv = sum(volumes[hist_i-20:hist_i])/20
        h_fp = [(closes[hist_i]-opens[hist_i])/h_atr, (highs[hist_i]-lows[hist_i])/h_atr, volumes[hist_i]/avg_hv]
        dist = calculate_distance(curr_fp, h_fp)
        outcome = 1 if (closes[hist_i+5]-closes[hist_i]) > h_atr*0.5 else (-1 if (closes[hist_i+5]-closes[hist_i]) < -h_atr*0.5 else 0)
        distances.append((dist, outcome))
        
    distances.sort(key=lambda x: x[0])
    ups = sum(1 for d, o in distances[:NEIGHBORS] if o == 1)
    downs = sum(1 for d, o in distances[:NEIGHBORS] if o == -1)
    
    if ups >= 3 and macro_trend != "BEARISH": return "LONG", "🔥 ВЫСОКАЯ" if ups == 5 else ("⚡️ СРЕДНЯЯ" if ups == 4 else "🛡 НИЗКАЯ")
    if downs >= 3 and macro_trend != "BULLISH": return "SHORT", "🔥 ВЫСОКАЯ" if downs == 5 else ("⚡️ СРЕДНЯЯ" if downs == 4 else "🛡 НИЗКАЯ")
    return None, ""

# --- БОТ И СКАНЕР ---
def broadcast(text):
    for chat_id in active_chats: send_msg(chat_id, text)

def send_msg(chat_id, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard}
    urllib.request.urlopen(urllib.request.Request(url, json.dumps(data).encode(), {'Content-Type': 'application/json'}))

def edit_msg(chat_id, mid, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {"chat_id": chat_id, "message_id": mid, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard}
    urllib.request.urlopen(urllib.request.Request(url, json.dumps(data).encode(), {'Content-Type': 'application/json'}))

def scan_timeframe(interval, label, cooldown):
    last_alerts, active_trades = {}, {}
    while True:
        try:
            macro = get_macro_trend()
            for sym in SYMBOLS:
                key = f"{sym}_{interval}"
                if key in active_trades:
                    # Логика трекинга... (упрощено для компактности)
                    pass
                else:
                    if time.time() - last_alerts.get(sym, 0) < cooldown: continue
                    c = get_data(sym, interval, 300)
                    if not c: continue
                    sig, conv = predict_knn(c, len(c)-1, sum([float(x[2])-float(x[3]) for x in c[-15:-1]])/14, macro)
                    if sig:
                        last_alerts[sym] = time.time()
                        msg = f"🤖 **AI ALERT | {sym}**\n⏳ **{label}**\n📉 {sig}\n> Уверенность: {conv}"
                        broadcast(msg)
            time.sleep(SCAN_INTERVAL)
        except: time.sleep(60)

def bot_engine():
    last_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_id}&timeout=30"
            upd = json.loads(urllib.request.urlopen(url).read().decode()).get("result", [])
            for u in upd:
                last_id = u["update_id"] + 1
                chat = u.get("message", {}).get("chat", {}).get("id") or u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                # ЗАМОК
                if chat and int(chat) != ADMIN_CHAT_ID: continue
                active_chats.add(chat)
                # ...логика меню...
            time.sleep(1)
        except: time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("1h", "⚡️ ИНТРАДЕЙ", 14400), daemon=True).start()
    threading.Thread(target=scan_timeframe, args=("4h", "🌊 СВИНГ", 43200), daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
