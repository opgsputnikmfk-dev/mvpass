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

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains, losses = 0, 0
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i-1]
        if diff >= 0: gains += diff
        else: losses -= diff
    if losses == 0: return 100
    rs = (gains / period) / (losses / period)
    return 100 - (100 / (1 + rs))

def calculate_ema(prices, period):
    if len(prices) < period: return prices[-1]
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_profitable_backtest():
    t_all, w_all = 0, 0
    details = ""
    
    for symbol in SYMBOLS:
        candles = get_data(symbol)
        if not candles or len(candles) < 50: continue
        
        try:
            closes = [float(c[4]) for c in candles]
        except:
            continue
        
        t, w = 0, 0
        for i in range(40, len(candles) - 8):
            p_slice = closes[:i+1]
            ema20 = calculate_ema(p_slice, 20)
            ema50 = calculate_ema(p_slice, 50)
            rsi = calculate_rsi(p_slice, 14)
            
            curr_p = closes[i]
            sig = None
            if ema20 > ema50 and rsi < 40: sig = "LONG"
            elif ema20 < ema50 and rsi > 60: sig = "SHORT"
                
            if sig:
                t += 1
                next_p = closes[i+6]
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
    return (f"📊 **СТАТИСТИКА СТРАТЕГИИ (7 ДНЕЙ)**\n\n"
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

def broadcast(text):
    for chat_id in active_chats:
        send_msg(chat_id, text)

def live_scanner():
    print("📡 Живой сканер с расчетом рисков запущен...")
    last_alerts = {}
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol)
                if not candles or len(candles) < 50: continue
                
                closes = [float(c[4]) for c in candles]
                highs = [float(c[2]) for c in candles]
                lows = [float(c[3]) for c in candles]
                
                ema20 = calculate_ema(closes, 20)
                ema50 = calculate_ema(closes, 50)
                rsi = calculate_rsi(closes, 14)
                curr_p = closes[-1]
                
                # Расчет ATR за последние 14 баров для динамических стопов
                atr = sum([highs[j] - lows[j] for j in range(-14, 0)]) / 14
                
                signal = None
                if ema20 > ema50 and rsi < 40:
                    signal = "LONG"
                    entry = curr_p
                    sl = entry - (atr * 1.2)
                    tp1 = entry + (atr * 1.5)
                    tp2 = entry + (atr * 3.0)
                elif ema20 < ema50 and rsi > 60:
                    signal = "SHORT"
                    entry = curr_p
                    sl = entry + (atr * 1.2)
                    tp1 = entry - (atr * 1.5)
                    tp2 = entry - (atr * 3.0)
                
                if signal:
                    now = time.time()
                    if now - last_alerts.get(symbol, 0) > 7200: # Пауза 2 часа на монету
                        last_alerts[symbol] = now
                        sym_name = symbol.replace('USDT', '')
                        msg = (
                            f"⚡️ **ТОРГОВЫЙ СИГНАЛ: {sym_name}**\n\n"
                            f"• Направление: **{signal} (Откат по тренду)**\n"
                            f"• Таймфрейм: `15m`\n\n"
                            f"📍 **Точка входа:** `{entry:.4f}`\n"
                            f"🛑 **Стоп-лосс (SL):** `{sl:.4f}`\n"
                            f"🎯 **Тейк-профит 1 (TP1):** `{tp1:.4f}`\n"
                            f"🎯 **Тейк-профит 2 (TP2):** `{tp2:.4f}`\n\n"
                            f"📊 **Индикаторы:** RSI = `{rsi:.1f}` | ATR = `{atr:.4f}`"
                        )
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
                            [{"text": "📊 Статистика стратегии (7 дней)", "callback_data": "SHOW_STATS"}]
                        ]
                    }
                    
                    if data == "SHOW_STATS":
                        send_msg(chat_id, "⏳ Считаю статистику...", keyboard)
                        report = calculate_profitable_backtest()
                        send_msg(chat_id, report, keyboard)
                    else:
                        welcome_text = (
                            "👋 **Торговый терминал активен**\n\n"
                            "Бот автоматически отслеживает тренды, считает риски и присылает готовые сигналы в этот чат.\n\n"
                            "Нажми кнопку ниже для проверки статистики:"
                        )
                        send_msg(chat_id, welcome_text, keyboard)
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка бота: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Trading Bot Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
