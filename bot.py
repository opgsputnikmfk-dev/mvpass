from flask import Flask
import urllib.request, urllib.parse, json, ssl, time, threading, os
from datetime import datetime

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
context = ssl._create_unverified_context()
app = Flask(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"]
active_chats = set()
start_time = time.time()

# --- ПАРАМЕТРЫ ADAPTIVE 4H SWING ---
INTERVAL = "4h"
HISTORY_LIMIT = 400
SCAN_INTERVAL = 14400 
TIME_STOP_BARS = 10 # 40 часов (10 свечей по 4H). Максимальное время жизни сделки.

def get_data(symbol):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={HISTORY_LIMIT}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=context, timeout=15) as r: 
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

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2: return 20
    try:
        tr, plus_dm, minus_dm = [], [], []
        for i in range(1, len(closes)):
            tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
            p_move = highs[i] - highs[i-1]
            m_move = lows[i-1] - lows[i]
            plus_dm.append(p_move if p_move > m_move and p_move > 0 else 0)
            minus_dm.append(m_move if m_move > p_move and m_move > 0 else 0)
            
        atr_val = sum(tr[-period:]) / period
        if atr_val == 0: return 0
        p_di = 100 * (sum(plus_dm[-period:]) / period) / atr_val
        m_di = 100 * (sum(minus_dm[-period:]) / period) / atr_val
        dx = 100 * abs(p_di - m_di) / (p_di + m_di) if (p_di + m_di) > 0 else 0
        return dx
    except:
        return 20

def get_adaptive_multipliers(current_atr, historical_atrs):
    # Динамический расчет множителей на основе фазы волатильности
    avg_atr = sum(historical_atrs) / len(historical_atrs) if historical_atrs else current_atr
    if current_atr > avg_atr * 1.2:
        return 2.5, 3.0, "HIGH" # Рынок штормит, расширяем стопы
    else:
        return 1.5, 2.0, "NORMAL" # Спокойный рынок, сужаем стопы

def calculate_terminal_backtest():
    t_all, w_all = 0, 0
    rows = []
    
    for symbol in SYMBOLS:
        candles = get_data(symbol)
        if not candles or len(candles) < 200: continue
        
        try:
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
        except:
            continue
        
        t, w = 0, 0
        start_idx = max(200, len(candles) - 180) # 30 дней теста
        
        for i in range(start_idx, len(candles) - 1):
            p_slice = closes[:i+1]
            h_slice_adx = highs[:i+1]
            l_slice_adx = lows[:i+1]
            
            ema200 = calculate_ema(p_slice, 200)
            ema50 = calculate_ema(p_slice, 50)
            rsi = calculate_rsi(p_slice, 14)
            adx = calculate_adx(h_slice_adx, l_slice_adx, p_slice, 14)
            
            curr_p = closes[i]
            
            h_slice = highs[i-14:i+1]
            l_slice = lows[i-14:i+1]
            current_atr = sum([h_slice[j] - l_slice[j] for j in range(len(h_slice))]) / 14
            
            # Собираем историю ATR за последние 60 свечей (10 дней)
            hist_atrs = []
            for k in range(i-60, i):
                hk = highs[k-14:k+1]
                lk = lows[k-14:k+1]
                if len(hk) == 15:
                    hist_atrs.append(sum([hk[j] - lk[j] for j in range(15)]) / 15)
            
            sl_mult, tp_mult, _ = get_adaptive_multipliers(current_atr, hist_atrs)
            
            sig = None
            if adx > 20:
                if ema50 > ema200 and curr_p < ema50 and curr_p > ema200 and rsi < 45:
                    sig = "LONG"
                    entry = curr_p
                    sl = entry - (current_atr * sl_mult)
                    tp = entry + (current_atr * tp_mult)
                elif ema50 < ema200 and curr_p > ema50 and curr_p < ema200 and rsi > 55:
                    sig = "SHORT"
                    entry = curr_p
                    sl = entry + (current_atr * sl_mult)
                    tp = entry - (current_atr * tp_mult)
                
            if sig:
                t += 1
                hit = False
                for j in range(1, TIME_STOP_BARS + 2):
                    if i + j >= len(candles): break
                    h_f = highs[i+j]
                    l_f = lows[i+j]
                    c_f = closes[i+j]
                    
                    if sig == "LONG":
                        if l_f <= sl: break # Выбило по стопу
                        if h_f >= tp:
                            hit = True # Взяли профит
                            break
                        # TIME STOP: Если прошло 10 свечей, а профита нет - закрываем. Если цена хуже точки входа - это убыток.
                        if j == TIME_STOP_BARS and c_f < entry:
                            break 
                    else:
                        if h_f >= sl: break
                        if l_f <= tp:
                            hit = True
                            break
                        # TIME STOP для шорта
                        if j == TIME_STOP_BARS and c_f > entry:
                            break
                            
                if hit:
                    w += 1
                    
        t_all += t
        w_all += w
        sym_name = symbol.replace('USDT', '').ljust(5)
        wr_sym = (w / t * 100) if t > 0 else 0.0
        rows.append(f"{sym_name} | {str(w).rjust(2)}/{str(t).rjust(3)} | {wr_sym:5.1f}%")
            
    winrate = (w_all / t_all * 100) if t_all > 0 else 0
    table_content = "\n".join(rows)
    
    report = (
        f"=== ADAPTIVE 4H SWING (30D) ===\n"
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
            [{"text": "📊 SYSTEM STATS (ADAPTIVE)", "callback_data": "SHOW_STATS"}],
            [{"text": "🪙 MONITORED ASSETS", "callback_data": "SHOW_ASSETS"},
             {"text": "🟢 BOT STATUS", "callback_data": "BOT_STATUS"}],
            [{"text": "🧠 STRATEGY INFO", "callback_data": "SHOW_STRATEGY"},
             {"text": "ℹ️ HELP", "callback_data": "SHOW_HELP"}]
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
    print(f"Adaptive 4H Scanner Online (Interval: {INTERVAL})...")
    last_alerts = {}
    while True:
        try:
            for symbol in SYMBOLS:
                candles = get_data(symbol)
                if not candles or len(candles) < 250: continue
                
                closes = [float(c[4]) for c in candles]
                highs = [float(c[2]) for c in candles]
                lows = [float(c[3]) for c in candles]
                
                ema200 = calculate_ema(closes, 200)
                ema50 = calculate_ema(closes, 50)
                rsi = calculate_rsi(closes, 14)
                adx = calculate_adx(highs, lows, closes, 14)
                
                curr_p = closes[-1]
                current_atr = sum([highs[j] - lows[j] for j in range(-14, 0)]) / 14
                
                hist_atrs = []
                for k in range(len(candles)-60, len(candles)):
                    hk = highs[k-14:k+1]
                    lk = lows[k-14:k+1]
                    if len(hk) == 15:
                        hist_atrs.append(sum([hk[j] - lk[j] for j in range(15)]) / 15)
                        
                sl_mult, tp_mult, vol_phase = get_adaptive_multipliers(current_atr, hist_atrs)
                
                signal = None
                if adx > 20:
                    if ema50 > ema200 and curr_p < ema50 and curr_p > ema200 and rsi < 45:
                        signal = "LONG"
                        entry = curr_p
                        sl = entry - (current_atr * sl_mult)
                        tp = entry + (current_atr * tp_mult)
                    elif ema50 < ema200 and curr_p > ema50 and curr_p < ema200 and rsi > 55:
                        signal = "SHORT"
                        entry = curr_p
                        sl = entry + (current_atr * sl_mult)
                        tp = entry - (current_atr * tp_mult)
                
                if signal:
                    now = time.time()
                    if now - last_alerts.get(symbol, 0) > 86400:
                        last_alerts[symbol] = now
                        sym_name = symbol.replace('USDT', '')
                        
                        # Расчет времени экспирации сигнала
                        time_stop_hours = TIME_STOP_BARS * 4
                        
                        msg = (
                            "```text\n"
                            f"[ADAPTIVE 4H ALERT] // {sym_name}USDT\n"
                            "---------------------------------\n"
                            f"ACTION:     {signal}\n"
                            f"TIMEFRAME:  {INTERVAL}\n"
                            f"ENTRY:      {entry:.4f}\n"
                            f"STOP-LOSS:  {sl:.4f}\n"
                            f"TAKE-PROFIT: {tp:.4f}\n"
                            "---------------------------------\n"
                            f"VOLATILITY PHASE: {vol_phase}\n"
                            f"TIME LIMIT: MAX {time_stop_hours} HOURS\n"
                            f"RSI: {rsi:.1f} | ADX: {adx:.1f}\n"
                            f"TIME: {datetime.utcnow().strftime('%H:%M:%S')} UTC\n"
                            "```"
                        )
                        broadcast(msg)
            time.sleep(3600)
        except Exception as e:
            print(f"Scanner error: {e}")
            time.sleep(60)

def bot_engine():
    last_update_id = 0
    print("Adaptive Engine Online...")
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
                        send_msg(chat_id, "Processing adaptive backtest with time-decay...", get_main_keyboard())
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
                            f"STRATEGY: Adaptive 4H w/ Time Stop\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_STRATEGY":
                        msg = (
                            "```text\n"
                            "=== TRADING MODEL: ADAPTIVE DYNAMICS ===\n"
                            "TYPE: Machine-Logic Swing Trading\n"
                            "TIMEFRAME: 4h\n"
                            "---------------------------------\n"
                            "[ DYNAMIC LOGIC ]\n"
                            "This bot learns from current market rhythm.\n"
                            "1. Volatility Scaling: If market is erratic, stops expand automatically to avoid wicks. If calm, stops tighten to increase R:R.\n"
                            "2. Time-In-Market Decay: Real institutional moves happen fast. If a trade does not hit target within 40 hours (10 bars), the setup is considered failed and capital is freed.\n\n"
                            "[ RISK MANAGEMENT ]\n"
                            "- SL: 1.5x - 2.5x ATR (Auto-scaled)\n"
                            "- TP: 2.0x - 3.0x ATR (Auto-scaled)\n"
                            "- Hard Time Stop: 40 Hours max hold.\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_HELP":
                        msg = (
                            "```text\n"
                            "=== TERMINAL HELP ===\n"
                            "1. SYSTEM STATS: 30-day dynamic backtest.\n"
                            "2. ASSETS: Tracked pairs.\n"
                            "3. STRATEGY INFO: Auto-scaling rules.\n"
                            "4. SIGNALS: Adaptive pullbacks with time limits.\n"
                            "```"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    else:
                        welcome_text = (
                            "```text\n"
                            "ADAPTIVE 4H TERMINAL ACTIVE\n"
                            "---------------------------------\n"
                            "System initialized. Calibrating volatility...\n"
                            "```"
                        )
                        send_msg(chat_id, welcome_text, get_main_keyboard())
            time.sleep(1)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Adaptive Terminal Server Active"

if __name__ == "__main__":
    threading.Thread(target=bot_engine, daemon=True).start()
    threading.Thread(target=live_scanner, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
