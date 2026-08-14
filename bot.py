from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]

def get_data(symbol, interval="15m", limit=672): # 672 свечи по 15м = ровно 7 дней
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(url, context=context, timeout=5) as r: 
            return json.loads(r.read())
    except: return []

def calculate_backtest():
    """Анализ паттернов и сделок за последние 7 дней"""
    t_all, w_all = 0, 0
    details = ""
    
    for symbol in SYMBOLS:
        candles = get_data(symbol, "15m", 672)
        if len(candles) < 30: continue
        
        t, w = 0, 0
        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        
        # Симуляция прогона по истории за 7 дней с шагом в свечах
        for i in range(25, len(candles) - 4):
            prev_high = max(highs[i-20:i])
            prev_min = min(lows[i-20:i])
            avg_vol = sum(volumes[i-20:i]) / 20
            
            curr_close = closes[i]
            curr_vol = volumes[i]
            
            # Проверка условий стратегии (Пробой + Объем)
            if curr_vol > (avg_vol * 1.3):
                sig = None
                if curr_close > prev_high:
                    sig = "LONG"
                elif curr_close < prev_min:
                    sig = "SHORT"
                
                if sig:
                    t += 1
                    # Проверяем отработку через 4 свечи (1 час)
                    next_close = closes[i+4]
                    if (sig == "LONG" and next_close > curr_close) or (sig == "SHORT" and next_close < curr_close):
                        w += 1
                        
        t_all += t
        w_all += w
        sym_name = symbol.replace('USDT', '')
        if t > 0:
            wr_sym = (w / t) * 100
            details += f"🔹 **{sym_name}**: {w}/{t} сделок ({wr_sym:.1f}%)\n"
        else:
            details += f"🔹 **{sym_name}**: 0 сделок\n"
            
    total_wr = (w_all / t_all * 100) if t_all > 0 else 0
    
    report = (f"📊 **СТАТИСТИКА СТРАТЕГИИ ЗА 7 ДНЕЙ**\n\n"
              f"{details}\n"
              f"📈 **Всего сделок:** {t_all}\n"
              f"✅ **Успешных:** {w_all}\n"
              f"🏆 **Общий WinRate:** {total_wr:.1f}%")
    return report

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}&parse_mode=Markdown"
    if keyboard:
        url += f"&reply_markup={urllib.parse.quote(json.dumps(keyboard))}"
    try:
        urllib.request.urlopen(url, context=context, timeout=5)
    except: pass

def bot_engine():
    last_update_id = 0
    print("🤖 Бот с кнопкой статистики запущен...")
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
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "📊 Посмотреть статистику за 7 дней", "callback_data": "SHOW_STATS"}]
                        ]
                    }
                    if data == "SHOW_STATS":
                        send_msg(chat_id, "⏳ Анализирую графики топ-10 монет за последнюю неделю...")
                        report = calculate_backtest()
                        send_msg(chat_id, report, keyboard)
                    else:
                        send_msg(chat_id, "👋 Привет! Это торговый терминал-анализатор.\n\nНажми кнопку ниже, чтобы проверить результаты нашей стратегии:", keyboard)
            
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Stats Bot is active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
