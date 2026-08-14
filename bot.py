from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]

def get_data(symbol, interval="15m", limit=500):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(url, context=context, timeout=5) as r: 
            return json.loads(r.read())
    except: return []

def calculate_active_backtest():
    """Активный поиск пробоев на 15m — гарантирует постоянные сделки"""
    t_all, w_all = 0, 0
    details = ""
    
    for symbol in SYMBOLS:
        candles = get_data(symbol, "15m", 500)
        if len(candles) < 30: continue
        
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        
        t, w = 0, 0
        # Сканируем каждую 15-минутку по истории
        for i in range(20, len(candles) - 4):
            prev_highs = highs[i-20:i]
            prev_lows = lows[i-20:i]
            
            curr_p = closes[i]
            max_level = max(prev_highs)
            min_level = min(prev_lows)
            
            sig = None
            if curr_p > max_level:
                sig = "LONG"
            elif curr_p < min_level:
                sig = "SHORT"
                
            if sig:
                t += 1
                next_p = closes[i+4] # Проверка через 1 час (4 свечи)
                if (sig == "LONG" and next_p > curr_p) or (sig == "SHORT" and next_p < curr_p):
                    w += 1
                    
        t_all += t
        w_all += w
        sym_name = symbol.replace('USDT', '')
        if t > 0:
            wr_sym = (w / t) * 100
            details += f"🔹 **{sym_name}**: {w}/{t} сделок ({wr_sym:.1f}%)\n"
        else:
            details += f"🔹 **{sym_name}**: 0 сделок\n"
            
    winrate = (w_all / t_all * 100) if t_all > 0 else 0
    return (f"📊 **АКТИВНЫЙ ИТОГ СТРАТЕГИИ (15m)**\n\n"
            f"{details}\n"
            f"📈 **Всего сделок:** {t_all}\n"
            f"✅ **Успешных:** {w_all}\n"
            f"🏆 **WinRate:** {winrate:.1f}%\n"
            f"💡 **Статус:** Стратегия генерирует постоянный поток сделок.")

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}&parse_mode=Markdown"
    if keyboard: url += f"&reply_markup={urllib.parse.quote(json.dumps(keyboard))}"
    try: urllib.request.urlopen(url, context=context, timeout=5)
    except: pass

def bot_engine():
    last_update_id = 0
    print("🤖 Активный бот запущен...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            updates = json.loads(urllib.request.urlopen(url, timeout=35).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                chat_id = u.get("message", {}).get("chat", {}).get("id") or u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                data = u.get("callback_query", {}).get("data")
                
                if chat_id:
                    keyboard = {"inline_keyboard": [[{"text": "📊 Посмотреть активные сделки", "callback_data": "ACTIVE_STATS"}]]}
                    if data == "ACTIVE_STATS":
                        send_msg(chat_id, "⏳ Сканирую 15-минутные графики...")
                        report = calculate_active_backtest()
                        send_msg(chat_id, report, keyboard)
                    else:
                        send_msg(chat_id, "🚀 Активный терминал готов.\n\nНажми кнопку ниже, чтобы увидеть сделки:", keyboard)
            time.sleep(1)
        except: time.sleep(10)

@app.route('/')
def home(): return "Active Bot Running"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
