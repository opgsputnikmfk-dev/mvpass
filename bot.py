from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]

def get_data(symbol):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=500"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=10) as r: 
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"Ошибка загрузки {symbol}: {e}")
        return []

def calculate_active_backtest():
    t_all, w_all = 0, 0
    details = ""
    
    for symbol in SYMBOLS:
        candles = get_data(symbol)
        if not candles or len(candles) < 30: 
            details += f"🔹 {symbol.replace('USDT','')}: нет данных\n"
            continue
        
        try:
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            closes = [float(c[4]) for c in candles]
        except Exception as e:
            print(f"Ошибка парсинга {symbol}: {e}")
            continue
        
        t, w = 0, 0
        for i in range(20, len(candles) - 4):
            prev_high = max(highs[i-20:i])
            prev_low = min(lows[i-20:i])
            curr_p = closes[i]
            
            sig = None
            if curr_p > prev_high:
                sig = "LONG"
            elif curr_p < prev_low:
                sig = "SHORT"
                
            if sig:
                t += 1
                next_p = closes[i+4]
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
            f"🏆 **WinRate:** {winrate:.1f}%")

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}&parse_mode=Markdown"
    if keyboard: url += f"&reply_markup={urllib.parse.quote(json.dumps(keyboard))}"
    try: urllib.request.urlopen(url, context=context, timeout=5)
    except: pass

def bot_engine():
    last_update_id = 0
    print("🤖 Бот запущен и готов к работе...")
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
                    keyboard = {"inline_keyboard": [[{"text": "📊 Посмотреть активные сделки", "callback_data": "ACTIVE_STATS"}]]}
                    if data == "ACTIVE_STATS":
                        send_msg(chat_id, "⏳ Сканирую 15-минутные графики...")
                        report = calculate_active_backtest()
                        send_msg(chat_id, report, keyboard)
                    else:
                        send_msg(chat_id, "🚀 Терминал готов.\n\nНажми кнопку ниже, чтобы увидеть сделки:", keyboard)
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка в боте: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Active Bot Running"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
