from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()

def get_data(symbol, interval="15m", limit=50):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(url, context=context, timeout=5) as r: 
            return json.loads(r.read())
    except: return []

def evaluate_pattern(candles):
    """Паттерн пробоя локального экстремума с подтверждением объема"""
    if len(candles) < 30: return None
    
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    
    current_close = closes[-1]
    prev_high = max(highs[-20:-1]) # Локальный максимум за 20 свечей
    prev_low = min(lows[-20:-1])   # Локальный минимум за 20 свечей
    
    avg_vol = sum(volumes[-20:-1]) / 20
    current_vol = volumes[-1]
    
    # Условие объема: объем текущей свечи выше среднего на 30%
    is_volume_confirmed = current_vol > (avg_vol * 1.3)
    
    if is_volume_confirmed:
        if current_close > prev_high:
            return "LONG (Пробой вверх)"
        elif current_close < prev_low:
            return "SHORT (Пробой вниз)"
            
    return None

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

def active_scanner():
    """Активный сканер паттернов на 15m"""
    print("📡 Активный сканер паттернов запущен...")
    # Словарь для защиты от спама по одной монете (чтобы не слать сигнал каждый цикл)
    last_signals = {}
    
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol, "15m", 50)
                signal = evaluate_pattern(candles)
                
                if signal:
                    # Проверяем, не отправляли ли мы уже этот сигнал недавно
                    last_time = last_signals.get(symbol, 0)
                    if time.time() - last_time > 3600: # Пауза 1 час на один символ
                        last_signals[symbol] = time.time()
                        curr_price = candles[-1][4]
                        msg = (f"🔥 АКТИВНЫЙ ПАТТЕРН!\n"
                               f"Монета: {symbol.replace('USDT','')}\n"
                               f"Сигнал: {signal}\n"
                               f"Цена входа: {curr_price}\n"
                               f"Таймфрейм: 15m")
                        broadcast(msg)
                time.sleep(2)
            time.sleep(300) # Проверять каждые 5 минут
        except Exception as e:
            print(f"Ошибка сканера: {e}")
            time.sleep(30)

def bot_engine():
    last_update_id = 0
    print("🤖 Бот слушает Telegram...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            updates = json.loads(urllib.request.urlopen(url, timeout=35).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                chat_id = None
                if "message" in u and "text" in u["message"]:
                    chat_id = u["message"]["chat"]["id"]
                elif "callback_query" in u:
                    chat_id = u["callback_query"]["message"]["chat"]["id"]
                
                if chat_id:
                    active_chats.add(chat_id)
                    keyboard = {"inline_keyboard": [[{"text": "📊 Проверить статус бота", "callback_data": "STATUS"}]]}
                    send_msg(chat_id, "🚀 Бот переведен на активный поиск паттернов (15m)! Сигналы будут поступать регулярно при пробое уровней с объемами.", keyboard)
            
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка телеграма: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Active Pattern Bot is live"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=active_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
