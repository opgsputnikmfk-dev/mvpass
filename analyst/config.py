from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from .universe import FALLBACK20

FILTERS = {
    "ema": "Направление EMA 50/200 (1ч)",
    "structure": "Структура максимумов и минимумов (1ч)",
    "regime": "Сценарий соответствует состоянию рынка",
    "adx": "Выраженность тренда ADX (1ч)",
    "btc": "Направление BTC (1ч)",
    "rsi": "Импульс RSI (15м)",
    "macd": "Гистограмма MACD (15м)",
    "atr": "Допустимая волатильность ATR (15м)",
    "bollinger": "Расширение полос Боллинджера (15м)",
    "volume": "Относительный объём (5м)",
    "vwap": "Сессионный VWAP UTC (5м)",
    "profile": "Профиль объёма: приближение по свечам",
    "fibonacci": "Откат Фибоначчи 38,2–61,8% (15м)",
    "room": "Пространство до встречного уровня",
    "candle": "Поглощение / отбойная свеча (5м)",
    "divergence": "Дивергенция RSI по подтверждённым экстремумам",
    "channel": "Пробой канала Donchian (5м)",
    "double_extreme": "Двойная вершина / основание (15м)",
    "fvg": "Возврат в трёхсвечный ценовой разрыв (15м)",
    "order_block": "Возврат в свечу перед импульсом (15м)",
    "oi": "Изменение открытого интереса",
    "funding": "Стоимость следующего funding",
    "spread": "Спред покупки / продажи",
    "depth": "Глубина стакана в пределах 0,1%",
    "events": "Пауза вокруг заданных событий",
}
SETUPS = {"pullback": "Откат по тренду", "breakout": "Пробой диапазона",
          "false_break": "Ложный пробой", "range_reversal": "Отбой в диапазоне"}

DEFAULT = {
    "schema": 1, "exchange": "bybit", "market": "linear_usdt_perpetual", "mode": "paper",
    "symbols": list(FALLBACK20),
    "setups": ["pullback", "breakout", "false_break", "range_reversal"],
    "filters": {k: ("on" if k in {"ema", "atr", "room", "spread"} else "observe") for k in FILTERS},
    "params": {"adx_min": 20.0, "rsi_long_min": 45.0, "rsi_long_max": 70.0,
               "rsi_short_min": 30.0, "rsi_short_max": 55.0,
               "atr_min_pct": 0.1, "atr_max_pct": 4.0, "volume_min": 1.1,
               "max_spread_bps": 8.0, "min_depth_usdt": 100000.0,
               "max_funding_cost": 0.0003, "oi_change_min_pct": 0.0,
               "fib_tolerance_atr": 0.2, "room_r": 1.5},
    "risk": {"initial_equity": 10000.0, "risk_pct": 0.0025, "max_open_risk_pct": 0.0075,
             "max_side_risk_pct": 0.005, "max_positions": 3, "max_notional_equity": 1.0,
             "daily_loss_pct": 0.02, "max_drawdown_pct": 0.08, "stop_atr_buffer": 0.2,
             "min_stop_pct": 0.002, "max_stop_pct": 0.05, "target_r": 2.0,
             "max_hold_minutes": 240, "entry_delay_minutes": 1,
             "max_entry_drift_atr": 0.35, "cooldown_minutes": 60,
             "trailing_atr": 0.0, "fee_rate": 0.00055, "slippage_bps": 3.0},
    "scanner": {"poll_seconds": 20, "warmup_days": 16, "max_data_age_seconds": 180},
    "events": [], "paused": False,
}

def number(x):
    return isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x)

def validate(raw: dict) -> dict:
    """Strict validation: reject unknown settings and invalid/NaN risks."""
    c = copy.deepcopy(raw)
    if set(c) != set(DEFAULT):
        raise ValueError("Неполная конфигурация или неизвестные поля")
    if c["schema"] != 1 or c["mode"] != "paper" or (c["exchange"],c["market"]) not in (("bybit","linear_usdt_perpetual"),("binance","spot_reference")):
        raise ValueError("Поддерживаются Bybit perpetual или Binance spot_reference, только paper")
    if not isinstance(c["symbols"], list) or not 1 <= len(c["symbols"]) <= 30:
        raise ValueError("Нужно от 1 до 30 символов")
    if any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{2,20}USDT", s) for s in c["symbols"]) or len(set(c["symbols"])) != len(c["symbols"]):
        raise ValueError("Неверные или повторные символы")
    if not isinstance(c["setups"], list) or any(x not in SETUPS for x in c["setups"]) or len(set(c["setups"])) != len(c["setups"]):
        raise ValueError("Неизвестный / повторный сценарий")
    if not isinstance(c["filters"], dict) or set(c["filters"]) != set(FILTERS) or any(x not in ("on", "off", "observe") for x in c["filters"].values()):
        raise ValueError("Режим фильтра: on / observe / off")
    for key in ("risk", "params", "scanner"):
        if not isinstance(c[key], dict) or set(c[key]) != set(DEFAULT[key]) or not all(number(v) for v in c[key].values()):
            raise ValueError(f"Некорректные числовые параметры {key}")
    r, p, s = c["risk"], c["params"], c["scanner"]
    limits = {"initial_equity": (10, 1e9), "risk_pct": (.00001, .02), "max_open_risk_pct": (.00001, .1),
              "max_side_risk_pct": (.00001, .1), "max_positions": (1, 30), "max_notional_equity": (.01, 1),
              "daily_loss_pct": (.001, .25), "max_drawdown_pct": (.001, .5), "stop_atr_buffer": (0, 5),
              "min_stop_pct": (.0001, .1), "max_stop_pct": (.0001, .3), "target_r": (.5, 10),
              "max_hold_minutes": (5, 10080), "entry_delay_minutes": (1, 30), "max_entry_drift_atr": (.01, 3),
              "cooldown_minutes": (5, 10080), "trailing_atr": (0, 20), "fee_rate": (0, .01), "slippage_bps": (0, 200)}
    if any(not lo <= r[k] <= hi for k, (lo, hi) in limits.items()):
        raise ValueError("Параметры риска вне допустимых границ")
    if not r["min_stop_pct"] <= r["max_stop_pct"] or r["risk_pct"] > min(r["max_open_risk_pct"], r["max_side_risk_pct"]):
        raise ValueError("Противоречивые ограничения риска")
    if any(r[k] != int(r[k]) for k in ("max_positions", "max_hold_minutes", "entry_delay_minutes", "cooldown_minutes")):
        raise ValueError("Количество и минуты должны быть целыми")
    if any(v < 0 for k, v in p.items() if k != "oi_change_min_pct") or not 0 <= p["adx_min"] <= 100:
        raise ValueError("Неверные параметры фильтров")
    if not 0 <= p["rsi_long_min"] <= p["rsi_long_max"] <= 100 or not 0 <= p["rsi_short_min"] <= p["rsi_short_max"] <= 100 or p["atr_min_pct"] > p["atr_max_pct"]:
        raise ValueError("Неверные границы RSI / ATR")
    if not 5 <= s["poll_seconds"] <= 300 or not 12 <= s["warmup_days"] <= 90 or not 60 <= s["max_data_age_seconds"] <= 600:
        raise ValueError("Неверные параметры сканера")
    if not isinstance(c["paused"], bool) or not isinstance(c["events"], list):
        raise ValueError("Неверная пауза / события")
    for e in c["events"]:
        if set(e) != {"title", "start_ms", "end_ms"} or not isinstance(e["title"], str) or len(e["title"]) > 200 or not number(e["start_ms"]) or not number(e["end_ms"]) or e["end_ms"] <= e["start_ms"]:
            raise ValueError("Событие: title, start_ms, end_ms (UTC)")
    return c

def version(c: dict) -> str:
    return hashlib.sha256(json.dumps(validate(c), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]

def read_config(path=None):
    return validate(json.loads(Path(path).read_text()) if path else DEFAULT)
