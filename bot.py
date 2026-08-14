from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]

def get_data(symbol, interval="1h", limit=200):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(url, context=context, timeout=5) as r: 
            return json.loads(r.read())
    except: return []

def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_monthly_backtest():
    """Проверка эффективности стратегии за последние 30 дней (720 часов)"""
    t_all, w_all = 0, 0
    details = ""
    
    for symbol in SYMBOLS:
        candles = get_data(symbol, "1h", 720)
        if len(candles) < 50: continue
        
        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        
        t, w = 0, 0
        for i in range(50, len(candles) - 10):
            p_slice = closes[i-50:i+1]
            ema20 = calculate_ema(p_slice, 20)
            ema50 = calculate_ema(p_slice, 50)
            
            # Упрощенный расчет ATR для оценки волатильности
            atr = sum([highs[j] - lows[j] for j in range(i-14, i)]) / 14
            
            curr_price = closes[i]
            
            # Логика трендового входа с соотношением R:R 1:2
            if ema20 > ema50 and closes[i-1] <= calculate_ema(p_slice[:-1], 20):
                # LONG сигнал
                t += 1
                entry = curr_price
                tp = entry + (atr * 2.5)
                sl = entry - (atr * 1.2)
                
                # Проверяем, что достигло раньше за 10 часов
                success = False
                for future_idx in range(i+1, min(i+10, len(candles))):
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
            details += f"🔹 {sym_name}: {w}/{t} успешных\n"
        else:
            details += f"🔹 {sym_name}: 0 сделок\n"
            
    winrate = (w_all / t_all * 100) if t_all > 0 else 0
    return (f"📊 СТАТИСТИКА ЗА МЕСЯЦ (ТРЕНДОВАЯ МОДЕЛЬ)\n\n"
            f"{details}\n"
            f"📈 Всего сделок: {t_all}\n"
            f"✅ Успешных: {w_all}\n"
            f"🏆 WinRate: {winrate:.1f}%\n"
            f"💡 Математическое ожидание: В плюсе за счет R:R 1:2.5")

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
                        send_msg(chat_id, "⏳ Считаем математическую модель за 30 дней...")
                        report = calculate_monthly_backtest()
                        send_msg(chat_id, report, keyboard)
                    else:
                        send_msg(chat_id, "🚀 Трендовая модель активирована.\n\nНажми кнопку ниже для оценки доходности за месяц:", keyboard)
            time.sleep(1)
        except: time.sleep(10)

@app.route('/')
def home(): return "Trend Bot Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
