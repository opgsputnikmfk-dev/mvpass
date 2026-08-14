from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()

def get_data(symbol, interval="1h", limit=168):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(url, context=context, timeout=5) as r: 
            return json.loads(r.read())
    except: return []

def calculate_backtest():
    t_all, w_all = 0, 0
    details = ""
    for symbol in SYMBOLS:
        candles = get_data(symbol, "1h", 168)
        if len(candles) < 20: continue
        t, w = 0, 0
        for i in range(20, len(candles) - 4):
            prices = [float(c[4]) for c in candles[i-20:i+1]]
            sma = sum(prices) / 20
            stdev = (sum((x - sma)**2 for x in prices) / 20)**0.5
            upper, lower = sma + (3.0 * stdev), sma - (3.0 * stdev)
            
            curr_p = prices[-1]
            if curr_p < lower or curr_p > upper:
                t += 1
                next_p = float(candles[i+4][4])
                if (curr_p < lower and next_p > curr_p) or (curr_p > upper and next_p < curr_p):
                    w += 1
        t_all += t
        w_all += w
        if t > 0:
            details += f"🔹 {symbol.replace('USDT','')}: {w}/{t} ({(w/t)*100:.1f}%)\n"
        else:
            details += f"🔹 {symbol.replace('USDT','')}: 0 сделок\n"
    
    wr = (w_all / t_all * 100) if t_all > 0 else 0
    return (f"📊 СТАТИСТИКА ЗА 7 ДНЕЙ\n\n"
              f"{details}\n"
              f"📈 ВСЕГО СИГНАЛОВ: {t_all}\n"
              f"✅ УСПЕШНЫХ: {w_all}\n"
              f"🏆 WINRATE: {wr:.1f}%")

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}"
    if keyboard:
        url += f"&reply_markup={urllib.parse.quote(json.dumps(keyboard))}"
    try:
        urllib.request.urlopen(url, context=context, timeout=5)
    except: pass

def broadcast(text):
    for chat_id in active_chats:
        send_msg(chat_id, text)

def live_scanner():
    """Живой сканер: проверяет рынок и шлет реальные сигналы"""
    print("📡 Живой сканер запущен...")
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol, "1h", 30)
                if len(candles) < 20: continue
                prices = [float(c[4]) for c in candles]
                sma = sum(prices[-20:]) / 20
                stdev = (sum((x - sma)**2 for x in prices[-20:]) / 20)**0.5
                upper, lower = sma + (3.0 * stdev), sma - (3.0 * stdev)
                
                curr_p = prices[-1]
                if curr_p < lower:
                    broadcast(f"🚨 ЖИВОЙ СИГНАЛ (LONG)\nМонета: {symbol.replace('USDT','')}\nЦена отскока: {curr_p}\nУровень нижней границы: {lower:.2f}")
                elif curr_p > upper:
                    broadcast(f"🚨 ЖИВОЙ СИГНАЛ (SHORT)\nМонета: {symbol.replace('USDT','')}\nЦена отскока: {curr_p}\nУровень верхней границы: {upper:.2f}")
                
                time.sleep(2)
            time.sleep(1800) # Проверка каждые 30 минут
        except Exception as e:
            print(f"Ошибка сканера: {e}")
            time.sleep(60)

def bot_engine():
    last_update_id = 0
    print("🤖 Бот слушает Telegram...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            updates = json.loads(urllib.request.urlopen(url, timeout=35).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                
                chat_id, data = None, None
                if "message" in u and "text" in u["message"]:
                    chat_id = u["message"]["chat"]["id"]
                elif "callback_query" in u:
                    chat_id = u["callback_query"]["message"]["chat"]["id"]
                    data = u["callback_query"]["data"]
                
                if chat_id:
                    active_chats.add(chat_id)
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🔄 Обновить статистику (7 дней)", "callback_data": "REFRESH_STATS"}]
                        ]
                    }
                    if data == "REFRESH_STATS":
                        send_msg(chat_id, "⏳ Считаю реальные данные за 7 дней...")
                        stats_text = calculate_backtest()
                        send_msg(chat_id, stats_text, keyboard)
                    else:
                        send_msg(chat_id, "✅ Бот подключен и работает в живом режиме! Как только появится аномалия на монетах, пришлю сигнал сюда. Также можешь запросить статистику:", keyboard)
            
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка телеграма: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Bot is live 24/7"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
