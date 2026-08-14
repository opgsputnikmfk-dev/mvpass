from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()

def get_data(symbol):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=15m&limit=672"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=10) as r: 
            return json.loads(r.read().decode())
    except:
        return []

def calculate_ema(prices, period):
    if len(prices) < period: return prices[-1]
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_smart_backtest():
    """Бэктест с фильтрами тренда (EMA) и объема для высокого WinRate"""
    t_all, w_all = 0, 0
    details = ""
    
    for symbol in SYMBOLS:
        candles = get_data(symbol)
        if not candles or len(candles) < 50: continue
        
        try:
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            closes = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]
        except:
            continue
        
        t, w = 0, 0
        for i in range(50, len(candles) - 4):
            prev_high = max(highs[i-20:i])
            prev_low = min(lows[i-20:i])
            curr_p = closes[i]
            
            # Фильтр объема: объем свечи выше среднего за 20 баров
            avg_vol = sum(volumes[i-20:i]) / 20
            if volumes[i] < avg_vol: 
                continue
                
            # Трендовый фильтр (EMA 50)
            ema50 = calculate_ema(closes[:i+1], 50)
            
            sig = None
            if curr_p > prev_high and curr_p > ema50:
                sig = "LONG"
            elif curr_p < prev_low and curr_p < ema50:
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
            details += f"• **{sym_name}**: {w}/{t} ({wr_sym:.1f}%)\n"
        else:
            details += f"• **{sym_name}**: 0 сделок\n"
            
    winrate = (w_all / t_all * 100) if t_all > 0 else 0
    return (f"📊 **УМНАЯ СТАТИСТИКА ЗА 7 ДНЕЙ (Фильтры EMA + Объем)**\n\n"
            f"{details}\n"
            f"📈 **Всего сделок:** {t_all}\n"
            f"✅ **Успешных:** {w_all}\n"
            f"🏆 **Итоговый WinRate:** {winrate:.1f}%")

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}&parse_mode=Markdown"
    if keyboard: url += f"&reply_markup={urllib.parse.quote(json.dumps(keyboard))}"
    try: urllib.request.urlopen(url, context=context, timeout=5)
    except: pass

def broadcast(text):
    for chat_id in active_chats:
        send_msg(chat_id, text)

def live_scanner():
    """Фоновый сканер с фильтрами качества"""
    print("📡 Умный сканер запущен...")
    last_alerts = {}
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol)
                if not candles or len(candles) < 50: continue
                
                highs = [float(c[2]) for c in candles]
                lows = [float(c[3]) for c in candles]
                closes = [float(c[4]) for c in candles]
                volumes = [float(c[5]) for c in candles]
                
                prev_high = max(highs[-21:-1])
                prev_low = min(lows[-21:-1])
                curr_p = closes[-1]
                
                # Проверка фильтров в реальном времени
                avg_vol = sum(volumes[-21:-1]) / 20
                if volumes[-1] < avg_vol: continue
                
                ema50 = calculate_ema(closes, 50)
                
                signal = None
                if curr_p > prev_high and curr_p > ema50: signal = "LONG (По тренду с объемом)"
                elif curr_p < prev_low and curr_p < ema50: signal = "SHORT (По тренду с объемом)"
                
                if signal:
                    now = time.time()
                    if now - last_alerts.get(symbol, 0) > 7200: # Пауза 2 часа
                        last_alerts[symbol] = now
                        msg = (f"🔥 **КАЧЕСТВЕННЫЙ СИГНАЛ**\n\n"
                               f"• Монета: **{symbol.replace('USDT','')}**\n"
                               f"• Сигнал: **{signal}**\n"
                               f"• Цена: `{curr_p}`\n"
                               f"• Таймфрейм: 15m")
                        broadcast(msg)
                time.sleep(2)
            time.sleep(300)
        except Exception as e:
            print(f"Ошибка сканера: {e}")
            time.sleep(30)

def bot_engine():
    last_update_id = 0
    print("🤖 Бот запущен...")
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
                    active_chats.add(chat_id)
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "📊 Посмотреть умную статистику (7 дней)", "callback_data": "SHOW_STATS"}]
                        ]
                    }
                    
                    if data == "SHOW_STATS":
                        send_msg(chat_id, "⏳ Анализирую графики с учетом тренда и объемов...", keyboard)
                        report = calculate_smart_backtest()
                        send_msg(chat_id, report, keyboard)
                    else:
                        welcome_text = (
                            "👋 **Торговый терминал активен 24/7**\n\n"
                            "Бот автоматически отслеживает сильные пробои по тренду с подтверждением объема.\n\n"
                            "Нажми кнопку ниже, чтобы проверить обновленную статистику:"
                        )
                        send_msg(chat_id, welcome_text, keyboard)
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка бота: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Smart Trading Bot Running"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
