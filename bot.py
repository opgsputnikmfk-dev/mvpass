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

def calculate_mean_reversion_backtest():
    """Стратегия возврата к среднему (Bollinger Bands): высокая частота и высокий WinRate"""
    t_all, w_all = 0, 0
    details = ""
    
    for symbol in SYMBOLS:
        candles = get_data(symbol)
        if not candles or len(candles) < 30: continue
        
        try:
            closes = [float(c[4]) for c in candles]
        except:
            continue
        
        t, w = 0, 0
        for i in range(20, len(candles) - 4):
            p_slice = closes[i-20:i+1]
            sma = sum(p_slice) / 20
            stdev = (sum((x - sma)**2 for x in p_slice) / 20)**0.5
            
            # Границы полос Боллинджера (2 стандартных отклонения)
            upper_band = sma + (2.0 * stdev)
            lower_band = sma - (2.0 * stdev)
            
            curr_p = closes[i]
            sig = None
            
            # Если цена упала ниже нижней границы — ждем отскок вверх (LONG)
            if curr_p <= lower_band:
                sig = "LONG"
            # Если цена выросла выше верхней границы — ждем откат вниз (SHORT)
            elif curr_p >= upper_band:
                sig = "SHORT"
                
            if sig:
                t += 1
                next_p = closes[i+4] # Проверка через 1 час (возврат к среднему)
                # Успех, если цена пошла в сторону SMA
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
    return (f"📊 **СТАТИСТИКА СТРАТЕГИИ «ВОЗВРАТ К СРЕДНЕМУ» (7 ДНЕЙ)**\n\n"
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
    """Фоновый сканер точек возврата к среднему"""
    print("📡 Сканер возврата к среднему запущен...")
    last_alerts = {}
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol)
                if not candles or len(candles) < 25: continue
                
                closes = [float(c[4]) for c in candles]
                p_slice = closes[-20:]
                sma = sum(p_slice) / 20
                stdev = (sum((x - sma)**2 for x in p_slice) / 20)**0.5
                
                upper_band = sma + (2.0 * stdev)
                lower_band = sma - (2.0 * stdev)
                curr_p = closes[-1]
                
                signal = None
                if curr_p <= lower_band: signal = "LONG (Отскок от нижней границы)"
                elif curr_p >= upper_band: signal = "SHORT (Откат от верхней границы)"
                
                if signal:
                    now = time.time()
                    if now - last_alerts.get(symbol, 0) > 7200: # Пауза 2 часа на монету
                        last_alerts[symbol] = now
                        msg = (f"🎯 **СИГНАЛ НА ОТСКОК**\n\n"
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
                            [{"text": "📊 Статистика отскоков за 7 дней", "callback_data": "SHOW_STATS"}]
                        ]
                    }
                    
                    if data == "SHOW_STATS":
                        send_msg(chat_id, "⏳ Считаю точность стратегии возврата к среднему...", keyboard)
                        report = calculate_mean_reversion_backtest()
                        send_msg(chat_id, report, keyboard)
                    else:
                        welcome_text = (
                            "👋 **Торговый терминал (Стратегия отскоков)**\n\n"
                            "Бот ищет моменты перекупленности и перепроданности для торговли на возврат цены к среднему.\n\n"
                            "Нажми кнопку ниже, чтобы проверить статистику:"
                        )
                        send_msg(chat_id, welcome_text, keyboard)
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка бота: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Mean Reversion Bot Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
