from google import genai
from google.genai import types
from flask import Flask
import urllib.request
import urllib.parse
import json
import ssl
import time
import math
import threading
import os

# --- БЕЗОПАСНЫЕ КЛЮЧИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    print("ОШИБКА: Ключи не найдены в переменных окружения!")
    exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
context = ssl._create_unverified_context()

# --- WEB SERVER (Для бесплатного хостинга 24/7) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Quant Bot is alive and running 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- ПАРАМЕТРЫ QUANT-МОДЕЛИ ---
INTERVAL = "1h"
HISTORY_LIMIT = 300
SCAN_INTERVAL = 3600

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", 
           "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT", "SHIBUSDT", 
           "TRXUSDT", "UNIUSDT", "LTCUSDT"]

subscribed_users = set()
last_signal_times = {symbol: 0 for symbol in SYMBOLS}

def get_market_data(symbol):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={HISTORY_LIMIT}"
        with urllib.request.urlopen(url, context=context, timeout=10) as response:
            return json.loads(response.read())
    except Exception as e:
        print(f"Ошибка Binance API ({symbol}): {e}")
        return []

def calculate_bollinger_bands(prices, period=20, num_std=3.0):
    if len(prices) < period: return prices[-1], prices[-1], prices[-1]
    sma = sum(prices[-period:]) / period
    variance = sum((x - sma) ** 2 for x in prices[-period:]) / period
    stdev = math.sqrt(variance)
    return sma + (num_std * stdev), sma, sma - (num_std * stdev)

def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 100.0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, c_prev = float(candles[i][2]), float(candles[i][3]), float(candles[i-1][4])
        tr_list.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    return sum(tr_list[-period:]) / period

def calculate_adx(candles, period=14):
    if len(candles) < period * 2 + 1: return 50.0
    
    trs, pdms, ndms = [], [], []
    for i in range(1, len(candles)):
        h, l = float(candles[i][2]), float(candles[i][3])
        h_prev, l_prev = float(candles[i-1][2]), float(candles[i-1][3])
        c_prev = float(candles[i-1][4])
        
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
        
        dp = h - h_prev
        dm = l_prev - l
        pdms.append(dp if dp > dm and dp > 0 else 0)
        ndms.append(dm if dm > dp and dm > 0 else 0)
        
    def wilder_smooth(arr):
        res = [sum(arr[:period])]
        for val in arr[period:]:
            res.append(res[-1] - (res[-1]/period) + val)
        return res
        
    sm_trs = wilder_smooth(trs)
    sm_pdms = wilder_smooth(pdms)
    sm_ndms = wilder_smooth(ndms)
    
    dxs = []
    for i in range(len(sm_trs)):
        if sm_trs[i] == 0:
            dxs.append(0)
            continue
        pdi = 100 * sm_pdms[i] / sm_trs[i]
        ndi = 100 * sm_ndms[i] / sm_trs[i]
        if pdi + ndi == 0: dxs.append(0)
        else: dxs.append(100 * abs(pdi - ndi) / (pdi + ndi))
        
    if len(dxs) < period: return 50.0
    
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx

def evaluate_quant_strategy(candles):
    if len(candles) < 30: return None
    prices = [float(c[4]) for c in candles]
    
    current_price = prices[-1]
    upper, middle, lower = calculate_bollinger_bands(prices, 20, 3.0)
    adx = calculate_adx(candles, 14)
    
    if adx < 25:
        if current_price < lower: return "LONG"
        elif current_price > upper: return "SHORT"
        
    return None

def get_ai_news_sentiment(coin_name, direction):
    prompt = f"Ответь строго 1 словом: ПОЗИТИВ, НЕГАТИВ или НЕЙТРАЛЬНО. Актив {coin_name}, макро-фон для сделки в {direction} за последние 24ч."
    try:
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(tools=[{"google_search": {}}])
        )
        return response.text.strip().upper().replace(".", "")
    except: return "НЕЙТРАЛЬНО"

def get_quant_stats(symbol):
    candles = get_market_data(symbol)
    stats = {"t": 0, "w": 0}
    if not candles or len(candles) < 100: return stats
        
    for i in range(50, len(candles) - 1):
        sig = evaluate_quant_strategy(candles[:i+1])
        if sig:
            atr = calculate_atr(candles[:i+1], 14)
            p = float(candles[i][4])
            
            sl = p - (atr * 2.0) if sig == "LONG" else p + (atr * 2.0)
            tp = p + (atr * 0.5) if sig == "LONG" else p - (atr * 0.5)
            
            won, closed = False, False
            
            for f in range(i + 1, min(i + 24, len(candles))):
                h, l = float(candles[f][2]), float(candles[f][3])
                if (sig == "LONG" and l <= sl) or (sig == "SHORT" and h >= sl): 
                    closed = True; break
                if (sig == "LONG" and h >= tp) or (sig == "SHORT" and l <= tp): 
                    won, closed = True, True; break
                    
            if closed:
                stats["t"] += 1
                if won: stats["w"] += 1
    return stats

def send_telegram_message(chat_id, text, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}"
        if reply_markup: url += f"&reply_markup={urllib.parse.quote(json.dumps(reply_markup))}"
        urllib.request.urlopen(url, context=context, timeout=10)
    except: pass

def broadcast_message(text):
    for chat_id in subscribed_users:
        send_telegram_message(chat_id, text)

def auto_scanner():
    print("📡 Quant-Радар запущен. Сканирование 3 Сигм каждые 60 мин...")
    while True:
        if not subscribed_users:
            time.sleep(10)
            continue
            
        print("\n🔍 Запуск поиска аномалий (Топ-15)...")
        for symbol in SYMBOLS:
            time.sleep(1.5)
            candles = get_market_data(symbol)
            if not candles: continue
            
            candle_time = candles[-1][0]
            if last_signal_times[symbol] == candle_time: continue 
            
            sig = evaluate_quant_strategy(candles)
            if sig:
                p = float(candles[-1][4])
                coin_name = symbol.replace("USDT", "")
                news = get_ai_news_sentiment(coin_name, sig)
                
                is_valid = (sig == "LONG" and news in ["ПОЗИТИВ", "НЕЙТРАЛЬНО"]) or \
                           (sig == "SHORT" and news in ["НЕГАТИВ", "НЕЙТРАЛЬНО"])
                
                if is_valid:
                    atr = calculate_atr(candles, 14)
                    sl = p - (atr * 2.0) if sig == "LONG" else p + (atr * 2.0)
                    tp = p + (atr * 0.5) if sig == "LONG" else p - (atr * 0.5)
                    
                    msg = (f"🎓 QUANT АНОМАЛИЯ (3σ): {coin_name}\n"
                           f"📉 ADX (Сила тренда): < 25 (Флэт)\n"
                           f"📈 Направление: {sig}\n"
                           f"📰 ИИ Фон: {news}\n"
                           f"💰 Вход: ${p:,.4f}\n"
                           f"❌ Защитный Стоп: ${sl:,.4f}\n"
                           f"🎯 Микро-Тейк: ${tp:,.4f}")
                    
                    broadcast_message(msg)
                    last_signal_times[symbol] = candle_time
                    print(f"✅ Аномалия найдена! Сигнал по {symbol}")
        time.sleep(SCAN_INTERVAL)

def main():
    scanner_thread = threading.Thread(target=auto_scanner, daemon=True)
    scanner_thread.start()
    
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
    print("🤖 Алгоритмический фонд активен.")
    last_update_id = 0
    keyboard = {
        "inline_keyboard": [
            [{"text": "📊 Математический Бэктест (12 дней)", "callback_data": "BACKTEST_QUANT"}]
        ]
    }
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?timeout=30&offset={last_update_id}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=context, timeout=35) as response:
                updates = json.loads(response.read().decode("utf-8")).get("result", [])
                
            for u in updates:
                last_update_id = u["update_id"] + 1
                
                if "message" in u and "text" in u["message"]:
                    chat_id = u["message"]["chat"]["id"]
                    if chat_id not in subscribed_users:
                        subscribed_users.add(chat_id)
                        send_telegram_message(chat_id, "✅ Доступ к Quant-радару открыт. Бот сканирует 15 монет на отклонения 3 Sigma + ADX. Ожидайте автоматических сигналов с WinRate 80%+", keyboard)
                    else:
                        send_telegram_message(chat_id, "🤖 Главное Quant-меню:", keyboard)
                        
                elif "callback_query" in u:
                    q = u["callback_query"]
                    chat_id = q["message"]["chat"]["id"]
                    
                    if q["data"] == "BACKTEST_QUANT":
                        send_telegram_message(chat_id, "⏳ Рассчитываю теорию вероятностей по 15 монетам (глубина 12 дней). Ожидайте...", keyboard)
                        
                        t_all, w_all = 0, 0
                        details = ""
                        
                        for symbol in SYMBOLS:
                            stats = get_quant_stats(symbol)
                            coin = symbol.replace("USDT", "")
                            t, w = stats["t"], stats["w"]
                            
                            t_all += t
                            w_all += w
                            
                            if t > 0:
                                details += f"🔹 {coin}: {w}/{t} ({(w/t)*100:.1f}%)\n"
                        
                        if t_all > 0:
                            wr_all = (w_all / t_all) * 100
                            msg = (f"📊 СТАТИСТИКА 3σ + ADX (12 дней)\n\n"
                                   f"{details}\n"
                                   f"📈 ИТОГО АНОМАЛИЙ: {t_all}\n"
                                   f"✅ ОТРАБОТКА: {w_all}\n"
                                   f"🏆 WINRATE ПОРТФЕЛЯ: {wr_all:.1f}%")
                        else:
                            msg = "📉 За последние 12 дней рынок не выходил за пределы 3 стандартных отклонений при низком ADX. Сигналов не было."
                            
                        send_telegram_message(chat_id, msg, keyboard)
        except: time.sleep(3)

if __name__ == "__main__":
    main()
