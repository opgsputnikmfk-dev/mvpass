from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
from datetime import datetime

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()

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

def calculate_terminal_backtest():
    t_all, w_all = 0, 0
    rows = []
    
    for symbol in SYMBOLS:
        candles = get_data(symbol)
        if not candles or len(candles) < 50: continue
        
        try:
            closes = [float(c[4]) for c in candles]
            opens = [float(c[1]) for c in candles]
        except:
            continue
        
        t, w = 0, 0
        for i in range(40, len(candles) - 8):
            p_slice = closes[:i+1]
            ema20 = calculate_ema(p_slice, 20)
            ema50 = calculate_ema(p_slice, 50)
            rsi = calculate_rsi(p_slice, 14)
            
            curr_p = closes[i]
            open_p = opens[i]
            
            sig = None
            if ema20 > ema50 and rsi < 42 and curr_p > open_p: 
                sig = "LONG"
            elif ema20 < ema50 and rsi > 58 and curr_p < open_p: 
                sig = "SHORT"
                
            if sig:
                t += 1
                next_p = closes[i+6]
                if (sig == "LONG" and next_p > curr_p) or (sig == "SHORT" and next_p < curr_p):
                    w += 1
                    
        t_all += t
        w_all += w
        sym_name = symbol.replace('USDT', '').ljust(5)
        wr_sym = (w / t * 100) if t > 0 else 0.0
        rows.append(f"{sym_name} | {str(w).rjust(2)}/{str(t).rjust(3)} | {wr_sym:5.1f}%")
            
    winrate = (w_all / t_all * 100) if t_all > 0 else 0
    table_content = "\n".join(rows)
    
    report = (
        "=== SYSTEM BACKTEST REPORT (7D) ===\n"
        "PAIR  | WIN/TOT | WINRATE\n"
        "---------------------------------\n"
        f"{table_content}\n"
        "---------------------------------\n"
        f"TOTAL TRADES: {t_all}\n"
        f"SUCCESSFUL:   {w_all}\n"
        f"WINRATE:      {winrate:.1f}%\n"
        f"TIMESTAMP:    {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
    )
    return f"```text\n{report}\n```"

def get_main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📊 SYSTEM STATS (7D)", "callback_data": "SHOW_STATS"}],
            [{"text": "🪙 MONITORED ASSETS", "callback_data": "SHOW_ASSETS"},
             {"text": "🟢 BOT STATUS", "callback_data": "BOT_STATUS"}],
            [{"text": "ℹ️ HELP & INFO", "callback_data": "SHOW_HELP"}]
        ]
    }

def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={chat_id}&text={urllib.parse.quote(text)}&parse_mode=Markdown"
    if keyboard: url += f"&reply_markup={urllib.parse.quote(json.dumps(keyboard))}"
    try: urllib.request.urlopen(url, context=context, timeout=5)
    except: pass

def broadcast(text):
    for chat_id in active_chats:
        send_msg(chat_id, text, get_main_keyboard())

def live_scanner():
    print("Terminal Scanner Online...")
    last_alerts = {}
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol)
                if not candles or len(candles) < 50: continue
                
                closes = [float(c[4]) for c in candles]
                opens = [float(c[1]) for c in candles]
                highs = [float(c[2]) for c in candles]
                lows = [float(c[3]) for c in candles]
                
                ema20 = calculate_ema(closes, 20)
                ema50 = calculate_ema(closes, 50)
                rsi = calculate_rsi(closes, 14)
                curr_p = closes[-1]
                open_p = opens[-1]
                
                atr = sum([highs[j] - lows[j] for j in range(-14, 0)]) / 14
                
                signal = None
                if ema20 > ema50 and rsi < 42 and curr_p > open_p:
                    signal = "LONG"
                    entry = curr_p
                    sl = entry - (atr * 1.2)
                    tp1 = entry + (atr * 1.5)
                    tp2 = entry + (atr * 3.0)
                elif ema20 < ema50 and rsi > 58 and curr_p < open_p:
                    signal = "SHORT"
                    entry = curr_p
                    sl = entry + (atr * 1.2)
                    tp1 = entry - (atr * 1.5)
                    tp2 = entry - (atr * 3.0)
                
                if signal:
                    now = time.time()
                    if now - last_alerts.get(symbol, 0) > 7200:
                        last_alerts[symbol] = now
                        sym_name = symbol.replace('USDT', '')
                        
                        msg = (
                            "```text\n"
                            f"[SIGNAL ALERT] // {sym_name}USDT\n"
                            "---------------------------------\n"
                            f"ACTION:     {signal}\n"
                            f"TIMEFRAME:  15m\n"
                            f"ENTRY:      {entry:.4f}\n"
                            f"STOP-LOSS:  {sl:.4f}\n"
                            f"TAKE-PROFIT 1: {tp1:.4f}\n"
                            f"TAKE-PROFIT 2: {tp2:.4f}\n"
                            "---------------------------------\n"
                            f"RSI: {rsi:.1f} | ATR: {atr:.4f}\n"
                            f"TIME: {datetime.utcnow().strftime('%H:%M:%S')} UTC\n"
                            "```"
                        )
                        broadcast(msg)
                time.sleep(2)
            time.sleep(300)
        except Exception as e:
            print(f"Scanner error: {e}")
            time.sleep(30)

def bot_engine():
    last_update_id = 0
    print("Terminal Bot Engine Online...")
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
                    
                    if data == "SHOW_STATS":
                        send_msg(chat_id, "Processing analytics...", get_main_keyboard())
                        report = calculate_terminal_backtest()
                        send_msg(chat_id, report, get_main_keyboard())
                        
                    elif data == "SHOW_ASSETS":
                        assets_list = ", ".join([s.replace('USDT', '') for s in SYMBOLS])
                        msg = f"```text\nMONITORED ASSETS (10):\n{assets_list}\n```"
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "BOT_STATUS":
                        uptime_sec = int(time.time() - start_time)
                        hours = uptime_sec // 3600
                        minutes = (uptime_sec % 3600) // 60
                        msg = (
                            "```text\n"
                            "=== SYSTEM STATUS ===\n"
                            f"STATUS: ACTIVE (24/7)\n"
                            f"UPTIME: {hours}h {minutes}m\n"
                            f"ACTIVE CHATS: {len(active_chats)}\n"
                            f"TIMEFRAME: 15m\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_HELP":
                        msg = (
                            "```text\n"
                            "=== TERMINAL HELP ===\n"
                            "1. SYSTEM STATS: 7-day historical backtest.\n"
                            "2. ASSETS: List of tracked pairs.\n"
                            "3. SIGNALS: Real-time 15m trend pullbacks.\n"
                            "   Includes Entry, SL, TP1, and TP2.\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    else:
                        welcome_text = (
                            "```text\n"
                            "TRADING TERMINAL v2.2 ACTIVE\n"
                            "---------------------------------\n"
                            "Select an option from the menu below:\n"
                            "```"
                        )
                        send_msg(chat_id, welcome_text, get_main_keyboard())
            time.sleep(1)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Terminal Server Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
