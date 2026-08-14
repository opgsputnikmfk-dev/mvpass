from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]

def get_data(symbol):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=20"
        with urllib.request.urlopen(url, context=context, timeout=5) as r: return json.loads(r.read())
    except: return []

def check_strategy(candles):
    prices = [float(c[4]) for c in candles]
    sma = sum(prices) / len(prices)
    stdev = (sum((x - sma)**2 for x in prices) / len(prices))**0.5
    if prices[-1] < (sma - 3.0 * stdev): return "LONG"
    if prices[-1] > (sma + 3.0 * stdev): return "SHORT"
    return None

def send_msg(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}"
    urllib.request.urlopen(url, context=context, timeout=5)

def bot_engine():
    last_update_id = 0
    print("🤖 Бот запущен и слушает Telegram...")
    while True:
        try:
            # 1. Слушаем команды
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}"
            updates = json.loads(urllib.request.urlopen(url, timeout=10).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                if "message" in u:
                    send_msg(u["message"]["chat"]["id"], "Бот активен. Мониторю рынок...")
            
            # 2. Сканируем рынок
            for s in SYMBOLS:
                sig = check_strategy(get_data(s))
                if sig:
                    for chat_id in [u.get("message", {}).get("chat", {}).get("id") for u in updates]:
                        if chat_id: send_msg(chat_id, f"СИГНАЛ: {s} {sig}")
            time.sleep(60)
        except Exception as e:
            print(f"Ошибка: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Bot is live"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
