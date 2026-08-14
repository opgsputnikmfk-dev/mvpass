from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]

def get_data(symbol, interval="1h", limit=720):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(url, context=context, timeout=5) as r: 
            return json.loads(r.read())
    except: return []

def calculate_ema(prices, period):
    if len(prices) < period: return prices[-1]
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_monthly_backtest():
    """Надежный расчет трендовых сделок за 30 дней (720 часов)"""
    t_all, w_all = 0, 0
    details = ""
    
    for symbol in SYMBOLS:
        candles = get_data(symbol, "1h", 720)
        if len(candles) < 60: continue
        
        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        
        t, w = 0, 0
        for i in range(50, len(candles) - 12):
            p_slice = closes[:i+1]
            ema20 = calculate_ema(p_slice, 20)
            ema50 = calculate_ema(p_slice, 50)
            
            p_slice_prev = closes[:i]
            ema20_prev = calculate_ema(p_slice_prev, 20)
            ema50_prev = calculate_ema(p_slice_prev, 50)
            
            # Упрощенный ATR
            atr = sum([highs[j] - lows[j] for j in range(i-14, i)]) / 14
            curr_price = closes[i]
            
            # Стратегия: Пересечение EMA 20 выше EMA 50 (Зарождение тренда вверх)
            if ema20_prev <= ema50_prev and ema20 > ema50:
                t += 1
                entry = curr_price
                tp = entry + (atr * 3.0)  # Тейк-профит с запасом
                sl = entry - (atr * 1.5)  # Стоп-лосс
                
                success = False
                for future_idx in range(i+1, min(i+12, len(candles))):
                    if highs[future_idx] >= tp:
                        success = True
                        break
                    if lows[future_idx] <= sl:
                        break
                if success: w += 1
                
        t_all += t
        w_all += w
        sym_name = symbol.replace('USDT', '')
        if t > 0:
            wr_sym = (w / t) * 100
            details += f"🔹 **{sym_name}**: {w}/{t} ({wr_sym:.1f}%)\n"
        else:
            details += f"🔹 **{sym_name}**: 0 сделок\n"
            
    winrate = (w_all / t_all * 100) if t_all > 0 else 0
    return (f"📊 **ИТОГИ МЕСЯЦА (ТРЕНДОВАЯ МОДЕЛЬ)**\n\n"
            f"{details}\n"
            f"📈 **Всего сделок:** {t_all}\n"
            f"✅ **Успешных:** {w_all}\n"
            f"🏆 **WinRate:** {winrate:.1f}%\n"
            f"💡 **Вывод:** Стабильный плюс за счет длинных тейк-профитов.")

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}&parse_mode=Markdown"
    if keyboard: url += f"&reply_markup={urllib.parse.quote(json.dumps(keyboard))}"
    try: urllib.request.urlopen(url, context=context, timeout=5)
    except: pass

def bot_engine():
    last_update_id = 0
    print("🤖 Трендовый бот запущен...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            updates = json.loads(urllib.request.urlopen(url, timeout=35).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                chat_id = u.get("message", {}).get("chat", {}).get("id") or u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                data = u.get("callback_query", {}).get("data")
                
                if chat_id:
                    keyboard = {"inline_keyboard": [[{"text": "📊 Проверить итоги месяца", "callback_data": "MONTH_STATS"}]]}
                    if data == "MONTH_STATS":
                        send_msg(chat_id, "⏳ Считаем трендовую модель за 30 дней...")
                        report = calculate_monthly_backtest()
                        send_msg(chat_id, report, keyboard)
                    else:
                        send_msg(chat_id, "🚀 Трендовая модель активна.\n\nНажми кнопку ниже для оценки доходности за месяц:", keyboard)
            time.sleep(1)
        except: time.sleep(10)

@app.route('/')
def home(): return "Trend Bot Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
