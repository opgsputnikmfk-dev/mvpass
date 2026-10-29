"""Telegram signal bot v2. PAPER ONLY: no order API and no exchange credentials.

Signals use closed candles. Paper fills are committed for the next full minute
before its open is observed and settled using closed 1m OHLC. Intrabar order is
unknown: an existing stop wins a tie; a new break-even stop starts next bar.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

"""Read-only Telegram analytics. No strategy or account mutations."""
import csv
import io
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
_dash_VERSION = '2.1 · Dashboard'
_dash_PERIODS = {'ALL': 'Всё время', 'TODAY': 'Сегодня (UTC)', '7D': '7 дней', '30D': '30 дней'}
_dash_MODES = {'15m': 'Скальпинг', '1h': 'Интрадей', '4h': 'Свинг'}

def _dash_stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%d.%m %H:%M UTC') if ms else '—'

def _dash_select(trades, period='ALL', now=None):
    now = time.time() if now is None else now
    start = {'ALL': 0, 'TODAY': now - now % 86400, '7D': now - 7 * 86400, '30D': now - 30 * 86400}[period] * 1000
    return sorted((t for t in trades if start <= t['exit_ts'] <= now * 1000), key=lambda t: (t['exit_ts'], t['id']))

def _dash_metrics(trades):
    values = [float(t['net_pnl']) for t in trades]
    wins, losses = ([x for x in values if x > 0], [x for x in values if x < 0])
    streak = maximum = 0
    for x in values:
        streak = streak + 1 if x < 0 else 0
        maximum = max(maximum, streak)
    return dict(n=len(values), net=sum(values), winrate=100 * len(wins) / len(values) if values else 0, pf=f'{sum(wins) / -sum(losses):.2f}' if losses else '∞ (нет убытков)' if wins else '—', avg=sum(values) / len(values) if values else 0, avg_win=sum(wins) / len(wins) if wins else 0, avg_loss=sum(losses) / len(losses) if losses else 0, streak=maximum, current_streak=streak, fees=sum((t['fees'] for t in trades)), funding=sum((t['funding_paid'] for t in trades)))

def _dash_analytics(trades, period='ALL', now=None):
    rows = _dash_select(trades, period, now)
    m = _dash_metrics(rows)
    lines = ['АНАЛИТИКА  /  ' + _dash_PERIODS[period], 'Модельный счёт · USDT', '', f"Закрытых позиций: {m['n']}", f"Чистый результат: {m['net']:+.2f}", f"Прибыльных: {m['winrate']:.1f}%" if rows else 'Прибыльных: —', f"Profit Factor: {m['pf']}", f"Средний результат: {m['avg']:+.2f}", f"Средняя прибыль / убыток: {m['avg_win']:+.2f} / {m['avg_loss']:+.2f}", f"Серия убытков, максимум / в конце периода: {m['streak']} / {m['current_streak']}", f"Комиссии: {m['fees']:.2f} · Funding: {m['funding']:+.2f}", '', 'ПО РЕЖИМАМ']
    for tf, name in _dash_MODES.items():
        group = _dash_metrics([t for t in rows if t['tf'] == tf])
        lines.append(f"{name}: {group['net']:+.2f} · {group['n']} сделок · PF {group['pf']}")
    lines += ['', 'ПО НАПРАВЛЕНИЯМ']
    for side in ('LONG', 'SHORT'):
        group = _dash_metrics([t for t in rows if t['side'] == side])
        lines.append(f"{side}: {group['net']:+.2f} · {group['n']} сделок")
    lines += ['', 'Период — по времени полного закрытия. Частичные выходы объединены в одну позицию. Комиссии и funding уже учтены в результате; проскальзывание — в ценах исполнения.']
    return '\n'.join(lines)

def _dash_breakdown(trades, period='ALL', now=None):
    rows = _dash_select(trades, period, now)
    groups = defaultdict(list)
    for t in rows:
        groups[t['symbol']].append(t)
    lines = ['ДЕТАЛИ  /  ' + _dash_PERIODS[period], 'Модельный счёт · USDT', '', 'ПО МОНЕТАМ']
    for symbol, group in sorted(groups.items(), key=lambda p: sum((t['net_pnl'] for t in p[1])), reverse=True):
        m = _dash_metrics(group)
        lines.append(f"{symbol}: {m['net']:+.2f} · n={m['n']} · PF {m['pf']}")
    if not rows:
        lines.append('Нет закрытых позиций.')
    lines += ['', 'ПОСЛЕДНИЙ ВЫХОД']
    for reason, count in sorted(Counter((t['exit_reason'] for t in rows)).items()):
        lines.append(f'{reason}: {count}')
    lines += [f"Достигли TP1: {sum((bool(t['tp1_hit']) for t in rows))} из {len(rows)}", '', 'ПО ДНЯМ ЗАКРЫТИЯ (UTC)']
    days = defaultdict(float)
    for t in rows:
        days[datetime.fromtimestamp(t['exit_ts'] / 1000, timezone.utc).strftime('%Y-%m-%d')] += t['net_pnl']
    lines += [f'{day}: {net:+.2f}' for day, net in sorted(days.items())[-14:]]
    lines.append('Показаны последние 14 дней с закрытиями; весь журнал доступен в CSV.')
    return '\n'.join(lines)

def _dash_overview(state, cfg, equity):
    positions = list(state['positions'].values())
    open_realized = sum((p['gross_pnl'] - p['fees'] - p['funding_paid'] for p in positions))
    unrealized = equity - state['balance']
    dd = max(0, 1 - equity / max(state['peak_equity'], 1e-12)) * 100
    risk = sum((p['planned_risk'] for p in positions))
    return '\n'.join(['AI CRYPTO  /  ОБЗОР', f'v{_dash_VERSION} · Симуляция · {cfg.market.upper()}', '', f'Капитал: {equity:,.2f} USDT', f'Старт: {cfg.initial_equity:,.2f} USDT', f'С начала: {(equity / cfg.initial_equity - 1) * 100:+.2f}%', '', f"Полностью закрытые позиции: {state['realized_net']:+.2f} USDT", f'Учтённый результат ещё открытых: {open_realized:+.2f} USDT', f'Оценка закрытия остатка: {unrealized:+.2f} USDT', '', f"Просадка сейчас / максимум: {dd:.2f}% / {state['max_drawdown'] * 100:.2f}%", f'Исходный риск открытых: {risk:.2f} USDT', f'Лимит риска: {cfg.max_total_risk * 100:.2f}% капитала', f"Открыто: {len(positions)} · Ожидают входа: {len(state['pending'])}", '', 'Сигналы: ' + ('включены' if state['enabled'] else 'пауза'), 'Скальпинг: ' + ('включён' if state['scalp_enabled'] else 'пауза'), 'Защитная пауза: ' + (', '.join((x for x, flag in [('дневной лимит', state['day_paused']), ('просадка', state['dd_paused'])] if flag)) or 'нет'), '', 'Капитал — оценка по закрытым 1m свечам с расходами на выход. Просадка — по наблюдениям. Исходный риск не учитывает перенос стопа и не гарантирует предел убытка.'])

def _dash_ledger_csv(trades):
    stream = io.StringIO(newline='')
    fields = ['id', 'symbol', 'tf', 'side', 'entry_ts', 'exit_ts', 'entry', 'qty', 'net_pnl', 'net_r', 'fees', 'funding_paid', 'tp1_hit', 'exit_reason', 'ambiguous_bars']
    writer = csv.DictWriter(stream, fields, extrasaction='ignore')
    writer.writeheader()
    for row in sorted(trades, key=lambda t: (t['exit_ts'], t['id'])):
        writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for k, v in row.items() if k in fields})
    return ('\ufeff' + stream.getvalue()).encode('utf-8')
from types import SimpleNamespace
dashboard = SimpleNamespace(MODES=_dash_MODES, PERIODS=_dash_PERIODS, VERSION=_dash_VERSION, analytics=_dash_analytics, breakdown=_dash_breakdown, ledger_csv=_dash_ledger_csv, metrics=_dash_metrics, overview=_dash_overview, select=_dash_select, stamp=_dash_stamp)


import numpy as np
import pandas as pd

LOG = logging.getLogger('paper_bot')
MINUTE = 60_000
TF_MS = {'15m': 15 * MINUTE, '1h': 60 * MINUTE, '4h': 240 * MINUTE}
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
           'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT')
FEATURES = ('body_atr', 'range_atr', 'volume_ratio', 'ema_distance', 'rsi_scaled')


@dataclass(frozen=True)
class Config:
    market: str = 'spot'
    initial_equity: float = 10_000.0
    risk_fraction: float = 0.0025
    max_total_risk: float = 0.0075
    max_position_notional: float = 0.25
    max_gross_notional: float = 1.0
    max_positions: int = 3
    max_same_direction: int = 2
    daily_loss_limit: float = 0.02
    drawdown_limit: float = 0.08
    loss_streak_limit: int = 3
    # Scenario assumptions, not a claim about the user's actual exchange tier.
    fee_bps: float = 10.0
    slippage_bps: float = 2.0
    max_spread_bps: float = 10.0
    stop_atr: float = 1.5
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    tp1_fraction: float = 0.5
    horizon: int = 48
    history_limit: int = 2500
    warmup: int = 250
    neighbors: int = 15
    min_adx: float = 20.0
    min_neighbor_mean_r: float = 0.10
    min_neighbor_win_fraction: float = 0.55
    max_entry_gap_atr: float = 0.25

    def __post_init__(self):
        if self.market not in ('spot', 'usdm'):
            raise ValueError('MARKET must be spot or usdm')
        for name in ('initial_equity', 'risk_fraction', 'max_total_risk',
                     'max_position_notional', 'max_gross_notional', 'stop_atr',
                     'tp1_r', 'tp2_r', 'daily_loss_limit', 'drawdown_limit'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        for name in ('fee_bps', 'slippage_bps', 'max_spread_bps', 'max_entry_gap_atr'):
            if not math.isfinite(getattr(self, name)) or not 0 <= getattr(self, name) < 1000:
                raise ValueError(f'Invalid {name}')
        if not 0 < self.tp1_fraction < 1 or self.tp2_r <= self.tp1_r:
            raise ValueError('Invalid take-profit settings')
        if not 0 < self.min_neighbor_win_fraction <= 1:
            raise ValueError('Invalid KNN threshold')
        if not math.isfinite(self.min_neighbor_mean_r) or not math.isfinite(self.min_adx):
            raise ValueError('Invalid KNN/ADX settings')
        if self.risk_fraction > self.max_total_risk or self.max_gross_notional > 1:
            raise ValueError('Risk budget invalid; gross leverage above 1x is disabled')
        if self.max_position_notional > self.max_gross_notional:
            raise ValueError('Position cap exceeds gross cap')
        if not 0 < self.daily_loss_limit < 1 or not 0 < self.drawdown_limit < 1:
            raise ValueError('Loss limits must be fractions between 0 and 1')
        if any(getattr(self, n) < 1 for n in ('max_positions', 'max_same_direction',
                                             'loss_streak_limit', 'horizon', 'neighbors')):
            raise ValueError('Counts must be positive')
        if self.warmup < 210 or self.history_limit < self.warmup + self.horizon * self.neighbors:
            raise ValueError('Not enough history for non-overlapping KNN outcomes')

    @classmethod
    def from_env(cls):
        mapping = {'market': 'MARKET', 'initial_equity': 'INITIAL_EQUITY',
                   'risk_fraction': 'RISK_PER_TRADE', 'fee_bps': 'FEE_BPS',
                   'slippage_bps': 'SLIPPAGE_BPS'}
        defaults = cls()
        values = {}
        for name, key in mapping.items():
            if key in os.environ:
                values[name] = str(os.environ[key]) if name == 'market' else float(os.environ[key])
        return dataclasses.replace(defaults, **values)


def frame_from_klines(rows, now_ms: int) -> pd.DataFrame:
    columns = ['ts', 'open', 'high', 'low', 'close', 'volume', 'close_ts',
               'quote_volume', 'trades', 'buy_volume', 'buy_quote_volume', 'ignore']
    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        return df
    for name in ('ts', 'close_ts', 'open', 'high', 'low', 'close', 'volume'):
        df[name] = pd.to_numeric(df[name], errors='raise')
    df = df.loc[df.close_ts < now_ms].sort_values('ts').drop_duplicates('ts').reset_index(drop=True)
    validate_bars(df)
    return df


def validate_bars(df: pd.DataFrame, interval_ms: int | None = None):
    if df.empty:
        return
    required = ['ts', 'open', 'high', 'low', 'close', 'volume', 'close_ts']
    values = df[required].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError('Non-finite OHLCV data')
    if ((df[['open', 'high', 'low', 'close']] <= 0).any().any()
            or (df.volume < 0).any() or (df.close_ts < df.ts).any()
            or (df.high < df[['open', 'close', 'low']].max(axis=1)).any()
            or (df.low > df[['open', 'close', 'high']].min(axis=1)).any()
            or not df.ts.is_monotonic_increasing or df.ts.duplicated().any()):
        raise ValueError('Invalid OHLCV data/order')
    if interval_ms is not None:
        if (len(df) > 1 and not (df.ts.diff().dropna() == interval_ms).all()
                or not (df.close_ts == df.ts + interval_ms - 1).all()):
            raise ValueError('Missing or misaligned candles; do not silently fill gaps')


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Every feature depends exclusively on the current/past CLOSED candles."""
    d = df.copy()
    close = d.close
    d['ema200'] = close.ewm(span=200, adjust=False).mean()
    d['ema50'] = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d['rsi'] = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).where(loss > 0, 100.0)
    d.loc[(gain == 0) & (loss == 0), 'rsi'] = 50.0
    tr = pd.concat([d.high - d.low, (d.high - close.shift()).abs(),
                    (d.low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    d['atr'] = atr
    up, down = d.high.diff(), -d.low.diff()
    plus = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=d.index)
    minus = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=d.index)
    plus_di = 100 * plus.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    d['adx'] = dx.fillna(0).ewm(alpha=1/14, adjust=False).mean()
    avg_volume = d.volume.shift(1).rolling(20, min_periods=20).mean()
    avg_range = (d.high - d.low).shift(1).rolling(50, min_periods=50).mean()
    d['volume_ratio'] = d.volume / avg_volume.replace(0, np.nan)
    d['anomaly'] = ((d.high - d.low) / avg_range.replace(0, np.nan) > 2.5) | (d.volume_ratio > 4.5)
    safe_atr = atr.replace(0, np.nan)
    d['body_atr'] = (close - d.open) / safe_atr
    d['range_atr'] = (d.high - d.low) / safe_atr
    d['ema_distance'] = (close - d.ema200) / safe_atr
    d['rsi_scaled'] = d.rsi / 100
    d['slope'] = (d.ema200 - d.ema200.shift(20)) / safe_atr
    return d


def direction(side: str) -> int:
    if side not in ('LONG', 'SHORT'):
        raise ValueError('Invalid side')
    return 1 if side == 'LONG' else -1


@dataclass
class Position:
    id: str
    symbol: str
    tf: str
    side: str
    entry_ts: int
    entry: float
    qty: float
    remaining: float
    stop: float
    initial_stop: float
    tp1: float
    tp2: float
    deadline: int
    planned_risk: float
    fee_rate: float
    slip_rate: float
    partial_fraction: float
    last_bar_ts: int = -1
    mark: float = 0.0
    tp1_hit: bool = False
    gross_pnl: float = 0.0
    fees: float = 0.0
    funding_paid: float = 0.0
    funding_through: int = -1
    exit_ts: int | None = None
    exit_reason: str | None = None
    ambiguous_bars: int = 0
    fills: list = field(default_factory=list)

    @property
    def net_pnl(self):
        return self.gross_pnl - self.fees - self.funding_paid


def make_position(symbol, tf, side, raw_open, atr, ts, qty, cfg: Config) -> Position:
    sign = direction(side)
    if tf not in TF_MS or any(not math.isfinite(x) or x <= 0 for x in (raw_open, atr, qty)):
        raise ValueError('Invalid entry/ATR/quantity/timeframe')
    if cfg.market == 'spot' and side == 'SHORT':
        raise ValueError('Spot short disabled: no borrow/margin model')
    slip, fee = cfg.slippage_bps / 10000, cfg.fee_bps / 10000
    entry = raw_open * (1 + sign * slip)
    risk_distance = cfg.stop_atr * atr
    stop = entry - sign * risk_distance
    tp1, tp2 = entry + sign * cfg.tp1_r * risk_distance, entry + sign * cfg.tp2_r * risk_distance
    if min(stop, tp1, tp2) <= 0:
        raise ValueError('Non-positive exit level')
    slipped_stop = stop * (1 - sign * slip)
    risk_unit = sign * (entry - slipped_stop) + fee * (entry + slipped_stop)
    p = Position(str(uuid.uuid4()), symbol, tf, side, int(ts), entry, qty, qty,
                 stop, stop, tp1, tp2, int(ts + cfg.horizon * TF_MS[tf] - 1),
                 qty * risk_unit, fee, slip, cfg.tp1_fraction, mark=raw_open,
                 funding_through=int(ts), fees=entry * qty * fee)
    p.fills.append({'kind': 'ENTRY', 'ts': int(ts), 'price': entry, 'qty': qty,
                    'fee': p.fees, 'model': 'adverse_slippage'})
    return p


def exit_fill(p: Position, raw_price: float, qty: float, ts: int, reason: str):
    sign = direction(p.side)
    price = raw_price * (1 - sign * p.slip_rate)
    qty = min(qty, p.remaining)
    gross = sign * (price - p.entry) * qty
    fee = price * qty * p.fee_rate
    p.gross_pnl += gross
    p.fees += fee
    p.remaining = max(0.0, p.remaining - qty)
    p.fills.append({'kind': reason, 'ts': int(ts), 'price': price, 'qty': qty, 'fee': fee})
    if p.remaining <= p.qty * 1e-9:
        p.remaining = 0.0
        p.exit_reason, p.exit_ts = reason, int(ts)


def cost_aware_be_stop(p: Position) -> float:
    """Raw trigger that covers entry+exit fees/slip on the remaining leg.

    Funding and gaps cannot be guaranteed by a stop at this level.
    """
    f, s = p.fee_rate, p.slip_rate
    if p.side == 'LONG':
        return p.entry * (1 + f) / ((1 - f) * (1 - s))
    return p.entry * (1 - f) / ((1 + f) * (1 + s))


def step_position(p: Position, bar: dict, funding: list | None = None):
    """Single event implementation shared by paper monitor, labels, backtest."""
    ts, end = int(bar['ts']), int(bar['close_ts'])
    if p.remaining == 0 or ts < p.entry_ts or ts <= p.last_bar_ts:
        return
    # For an offline/missing-data gap, the caller must backfill, not skip bars.
    sign = direction(p.side)
    for event in sorted(funding or [], key=lambda e: int(e['fundingTime'])):
        t = int(event['fundingTime'])
        if p.funding_through < t <= end and p.entry_ts < t:
            rate, mark = float(event['fundingRate']), float(event['markPrice'])
            if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                raise ValueError('Invalid funding event')
            amount = sign * p.remaining * mark * rate
            p.funding_paid += amount
            p.fills.append({'kind': 'FUNDING', 'ts': t, 'amount': amount})
            p.funding_through = t
    p.funding_through = max(p.funding_through, end)
    o, h, l, c = (float(bar[k]) for k in ('open', 'high', 'low', 'close'))
    p.mark, p.last_bar_ts = c, ts
    stop_hit = l <= p.stop if sign == 1 else h >= p.stop
    target_hit = h >= p.tp1 if sign == 1 else l <= p.tp1
    if stop_hit:
        if target_hit and not p.tp1_hit:
            p.ambiguous_bars += 1
        # A stop gapped at open fills at the worse open, never magically at stop.
        raw = min(o, p.stop) if sign == 1 else max(o, p.stop)
        exit_fill(p, raw, p.remaining, end, 'BE' if p.tp1_hit else 'SL')
        return
    if not p.tp1_hit and target_hit:
        exit_fill(p, p.tp1, p.qty * p.partial_fraction, end, 'TP1')
        p.tp1_hit = True
        p.stop = cost_aware_be_stop(p)  # inspected only on a subsequent bar
    tp2_hit = h >= p.tp2 if sign == 1 else l <= p.tp2
    if tp2_hit:
        exit_fill(p, p.tp2, p.remaining, end, 'TP2')
    elif end >= p.deadline:
        exit_fill(p, c, p.remaining, end, 'TIMEOUT')


def eligible_history_indices(current_idx: int, cfg: Config):
    # The next-bar entry AND the whole outcome window are strictly before query.
    start = max(cfg.warmup, current_idx - cfg.history_limit + 1)
    return np.arange(start, max(start, current_idx - cfg.horizon), dtype=int)


def choose_neighbors(df: pd.DataFrame, current_idx: int, cfg: Config):
    indices = eligible_history_indices(current_idx, cfg)
    if len(indices) < cfg.neighbors:
        return []
    x = df.loc[indices, list(FEATURES)].to_numpy(dtype=float)
    query = df.loc[current_idx, list(FEATURES)].to_numpy(dtype=float)
    good = np.isfinite(x).all(axis=1)
    x, indices = x[good], indices[good]
    if len(x) < cfg.neighbors or not np.isfinite(query).all():
        return []
    median = np.median(x, axis=0)
    scale = np.percentile(x, 75, axis=0) - np.percentile(x, 25, axis=0)
    scale = np.maximum(scale, 1e-6)
    train = np.clip((x - median) / scale, -10, 10)
    target = np.clip((query - median) / scale, -10, 10)
    distances = np.sqrt(np.mean((train - target) ** 2, axis=1))
    selected = []
    # Avoid counting many adjacent candles with overlapping outcomes as evidence.
    for order in np.argsort(distances, kind='stable'):
        i = int(indices[order])
        if all(abs(i - other) >= cfg.horizon for other in selected):
            selected.append(i)
            if len(selected) == cfg.neighbors:
                return selected
    return []


def historical_r(df, i, side, tf, cfg):
    atr, reference = float(df.iloc[i].atr), float(df.iloc[i].close)
    first = df.iloc[i + 1]
    if abs(float(first.open) - reference) > cfg.max_entry_gap_atr * atr:
        return None
    p = make_position('HISTORY', tf, side, float(first.open), atr, int(first.ts), 1.0, cfg)
    for j in range(i + 1, i + cfg.horizon + 1):
        step_position(p, df.iloc[j].to_dict())
        if p.remaining == 0:
            break
    # Futures KNN scores exclude funding; their score is NOT a probability/EV
    # estimate for executable futures positions. Actual paper PnL includes funding.
    return p.net_pnl / p.planned_risk


def predict_knn(df, current_idx, tf, cfg: Config):
    row = df.iloc[current_idx]
    if current_idx < cfg.warmup or not np.isfinite(row[list(FEATURES)].to_numpy(dtype=float)).all():
        return None, 'insufficient_history'
    if bool(row.anomaly):
        return None, 'range_or_volume_anomaly'
    if not math.isfinite(row.adx) or row.adx < cfg.min_adx:
        return None, 'adx'
    if row.close > row.ema200 and row.ema50 > row.ema200 and row.slope > 0:
        side = 'LONG'
        if row.rsi > 70:
            return None, 'rsi_overbought'
    elif row.close < row.ema200 and row.ema50 < row.ema200 and row.slope < 0:
        side = 'SHORT'
        if cfg.market == 'spot':
            return None, 'spot_short_disabled'
        if row.rsi < 30:
            return None, 'rsi_oversold'
    else:
        return None, 'trend_conflict'
    neighbors = choose_neighbors(df, current_idx, cfg)
    if not neighbors:
        return None, 'insufficient_independent_neighbors'
    scores = [historical_r(df, i, side, tf, cfg) for i in neighbors]
    scores = [s for s in scores if s is not None and math.isfinite(s)]
    if len(scores) < cfg.neighbors:
        return None, 'neighbor_entry_gaps'
    mean_r, wins = float(np.mean(scores)), sum(x > 0 for x in scores) / len(scores)
    if mean_r < cfg.min_neighbor_mean_r or wins < cfg.min_neighbor_win_fraction:
        return None, 'knn_net_outcomes'
    return {'side': side, 'atr': float(row.atr), 'reference': float(row.close),
            'signal_ts': int(row.close_ts), 'neighbor_mean_r': mean_r,
            'neighbor_win_fraction': wins, 'neighbors': len(scores)}, ''


def new_state(cfg: Config):
    return {'schema': 2, 'config': dataclasses.asdict(cfg), 'balance': cfg.initial_equity,
            'positions': {}, 'pending': {}, 'last_scanned': {}, 'rejects': {},
            'evaluated': {}, 'cooldowns': {}, 'streaks': {}, 'tf_pause_until': {},
            'day': None, 'day_start_equity': cfg.initial_equity, 'day_paused': False,
            'peak_equity': cfg.initial_equity, 'max_drawdown': 0.0,
            'dd_paused': False, 'enabled': True, 'scalp_enabled': True,
            'closed_count': 0, 'realized_net': 0.0, 'last_update_id': 0}


def account_equity(state):
    result = state['balance']
    for raw in state['positions'].values():
        p = Position(**raw)
        exit_price = p.mark * (1 - direction(p.side) * p.slip_rate)
        result += p.remaining * (direction(p.side) * (exit_price - p.entry) - exit_price * p.fee_rate)
    return result


def update_risk_state(state, now_ms, cfg):
    equity = account_equity(state)
    day = time.strftime('%Y-%m-%d', time.gmtime(now_ms / 1000))
    if state['day'] != day:
        state['day'], state['day_start_equity'] = day, equity
        state['day_paused'] = False
    state['peak_equity'] = max(state['peak_equity'], equity)
    drawdown = 1 - equity / max(state['peak_equity'], 1e-12)
    state['max_drawdown'] = max(state['max_drawdown'], drawdown)
    if equity <= state['day_start_equity'] * (1 - cfg.daily_loss_limit):
        state['day_paused'] = True
    if drawdown >= cfg.drawdown_limit:
        state['dd_paused'] = True
    return equity


def portfolio_gate(state, symbol, tf, side, now_ms, cfg):
    update_risk_state(state, now_ms, cfg)
    if not state['enabled'] or (tf == '15m' and not state['scalp_enabled']):
        return 'manual_pause'
    if state['day_paused'] or state['dd_paused']:
        return 'account_loss_limit'
    if now_ms < state['tf_pause_until'].get(tf, 0):
        return 'timeframe_loss_streak'
    if now_ms < state['cooldowns'].get(f'{symbol}_{tf}', 0):
        return 'cooldown'
    positions = list(state['positions'].values())
    if any(p['symbol'] == symbol for p in positions):
        return 'symbol_already_open'
    if len(positions) >= cfg.max_positions:
        return 'position_limit'
    if sum(p['side'] == side for p in positions) >= cfg.max_same_direction:
        return 'direction_limit'
    return ''


def open_paper_position(state, pending, bar, cfg):
    symbol, tf, side = pending['symbol'], pending['tf'], pending['side']
    ts = int(bar['ts'])
    reason = portfolio_gate(state, symbol, tf, side, ts, cfg)
    if reason:
        return None, reason
    if abs(float(bar['open']) - pending['reference']) > cfg.max_entry_gap_atr * pending['atr']:
        return None, 'entry_gap'
    unit = make_position(symbol, tf, side, float(bar['open']), pending['atr'], ts, 1.0, cfg)
    equity = account_equity(state)
    used_risk = sum(p['planned_risk'] for p in state['positions'].values())
    used_notional = sum(p['remaining'] * p['mark'] for p in state['positions'].values())
    budget = min(equity * cfg.risk_fraction, equity * cfg.max_total_risk - used_risk)
    notional = min(equity * cfg.max_position_notional, equity * cfg.max_gross_notional - used_notional)
    if budget <= 0 or notional <= 0:
        return None, 'portfolio_budget'
    qty = min(budget / unit.planned_risk, notional / unit.entry)
    if qty * unit.entry < 10:  # paper-only dust floor, not an exchange filter
        return None, 'position_too_small'
    p = make_position(symbol, tf, side, float(bar['open']), pending['atr'], ts, qty, cfg)
    state['balance'] -= p.fees
    state['positions'][p.id] = dataclasses.asdict(p)
    return p, ''


def settle_bar(state, position_id, bar, funding, cfg, update_account_risk=True):
    p = Position(**state['positions'][position_id])
    before = p.net_pnl
    step_position(p, bar, funding)
    state['balance'] += p.net_pnl - before
    if p.remaining:
        state['positions'][p.id] = dataclasses.asdict(p)
        closed = None
    else:
        del state['positions'][p.id]
        state['closed_count'] += 1
        state['realized_net'] += p.net_pnl
        state['streaks'][p.tf] = state['streaks'].get(p.tf, 0) + 1 if p.net_pnl < 0 else 0
        state['cooldowns'][f'{p.symbol}_{p.tf}'] = int(bar['close_ts']) + TF_MS[p.tf] * (4 if p.net_pnl < 0 else 1)
        if state['streaks'][p.tf] >= cfg.loss_streak_limit:
            state['tf_pause_until'][p.tf] = int(bar['close_ts']) + 24 * TF_MS[p.tf]
            state['streaks'][p.tf] = 0
        closed = dataclasses.asdict(p)
        closed['net_pnl'] = p.net_pnl
        closed['net_r'] = p.net_pnl / p.planned_risk
    if update_account_risk:
        update_risk_state(state, int(bar['close_ts']), cfg)
    return closed


def reject(state, tf, reason):
    bucket = state['rejects'].setdefault(tf, {})
    bucket[reason] = bucket.get(reason, 0) + 1


class Store:
    """State and closed trades commit together; one process, SQLite + RLock."""
    def __init__(self, path, cfg):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO state VALUES (1, ?)', (json.dumps(new_state(cfg)),))
        self.db.commit()
        saved = self.snapshot()
        if saved.get('schema') != 2 or saved['config'] != dataclasses.asdict(cfg):
            self.db.close()
            raise ValueError('Config differs from database. Use a NEW DATA_DIR for a new experiment.')

    def snapshot(self):
        with self.lock:
            return json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])

    def transact(self, callback):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                state = self.snapshot()
                result, closed = callback(state)
                for trade in closed:
                    self.db.execute('INSERT INTO trades VALUES (?, ?)',
                                    (trade['id'], json.dumps(trade, allow_nan=False)))
                self.db.execute('UPDATE state SET body=? WHERE id=1',
                                (json.dumps(state, allow_nan=False),))
                self.db.commit()
                return result
            except Exception:
                self.db.rollback()
                raise

    def trades(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM trades ORDER BY rowid')]

    def close(self):
        self.db.close()


class MarketClient:
    def __init__(self, market='spot'):
        self.market = market
        self.base = 'https://data-api.binance.vision' if market == 'spot' else 'https://fapi.binance.com'
        self.prefix = '/api/v3' if market == 'spot' else '/fapi/v1'
        self.frames = {}
        self.clock_offset = 0
        self.clock_checked = 0

    def get(self, path, **params):
        url = self.base + path + '?' + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'PaperSignalBot/2'})
                with urllib.request.urlopen(req, timeout=15) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 418:
                    raise RuntimeError('Exchange IP restriction; pause and inspect') from None
                if error.code != 429 and error.code < 500:
                    raise
                delay = min(30, max(1, int(error.headers.get('Retry-After', '3'))))
                if attempt == 2:
                    raise
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        raise RuntimeError('Unreachable request state')

    def now_ms(self):
        if time.monotonic() - self.clock_checked > 60:
            before = time.time() * 1000
            server = int(self.get(self.prefix + '/time')['serverTime'])
            after = time.time() * 1000
            self.clock_offset = server - (before + after) / 2
            self.clock_checked = time.monotonic()
        return int(time.time() * 1000 + self.clock_offset) - 1000  # closure guard

    def klines_range(self, symbol, tf, start, end):
        rows = []
        cursor = int(start)
        while cursor <= end:
            batch = self.get(self.prefix + '/klines', symbol=symbol, interval=tf,
                             startTime=cursor, endTime=int(end), limit=1000)
            if not isinstance(batch, list):
                raise ValueError('Invalid klines response')
            if not batch:
                break
            rows.extend(batch)
            following = int(batch[-1][0]) + 1
            if following <= cursor:
                raise ValueError('Non-advancing candle pagination')
            cursor = following
            if len(batch) < 1000:
                break
        return frame_from_klines(rows, int(end) + 1)

    def history(self, symbol, tf, now, limit):
        key = (symbol, tf)
        if key not in self.frames:
            rows, end = [], int(now - 1)
            while len(rows) < limit + 1:
                batch = self.get(self.prefix + '/klines', symbol=symbol, interval=tf,
                                 limit=min(1000, limit + 1 - len(rows)), endTime=end)
                if not batch:
                    break
                rows = batch + rows
                new_end = int(batch[0][0]) - 1
                if new_end >= end:
                    raise ValueError('Non-advancing candle history')
                end = new_end
            self.frames[key] = frame_from_klines(rows, now).tail(limit).reset_index(drop=True)
        else:
            old = self.frames[key]
            if old.empty:
                del self.frames[key]
                return self.history(symbol, tf, now, limit)
            fresh = self.klines_range(symbol, tf, int(old.iloc[-1].ts) + TF_MS[tf], now - 1)
            if not fresh.empty:
                self.frames[key] = pd.concat([old, fresh], ignore_index=True).drop_duplicates('ts').reset_index(drop=True)
        result = self.frames[key]
        validate_bars(result, TF_MS[tf])
        return result

    def spread_bps(self, symbol):
        quote = self.get(self.prefix + '/ticker/bookTicker', symbol=symbol)
        bid, ask = float(quote['bidPrice']), float(quote['askPrice'])
        if not all(math.isfinite(x) for x in (bid, ask)) or not 0 < bid <= ask:
            raise ValueError('Invalid spread quote')
        return 10000 * (ask - bid) / ((ask + bid) / 2)

    def funding(self, symbol, start, end):
        if self.market == 'spot':
            return []
        events, cursor = [], int(start)
        while cursor <= end:
            batch = self.get('/fapi/v1/fundingRate', symbol=symbol, startTime=cursor,
                             endTime=int(end), limit=1000)
            if not isinstance(batch, list):
                raise ValueError('Invalid funding response')
            events.extend(batch)
            if not batch or len(batch) < 1000:
                break
            following = int(batch[-1]['fundingTime']) + 1
            if following <= cursor:
                raise ValueError('Non-advancing funding pagination')
            cursor = following
        return events


def summarize_trades(trades):
    pnl = [float(t['net_pnl']) for t in trades]
    profits, losses = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    return {'trades': len(pnl), 'wins': sum(x > 0 for x in pnl),
            'winrate_pct': 100 * sum(x > 0 for x in pnl) / len(pnl) if pnl else 0,
            'net_pnl': sum(pnl), 'profit_factor': profits / losses if losses else None,
            'average_r': float(np.mean([t['net_r'] for t in trades])) if trades else 0,
            'fees': sum(t['fees'] for t in trades),
            'funding_paid': sum(t['funding_paid'] for t in trades)}


def report(store, cfg):
    with store.lock:
        state = store.snapshot()
    return dashboard.overview(state, cfg, account_equity(state))


def keyboard():
    def button(text, action):
        return {'text': text, 'callback_data': action}
    return {'inline_keyboard': [
        [button('Обзор', 'SHOW_STATS'), button('Позиции', 'SHOW_LIVE_TRADES')],
        [button('Сегодня', 'AN_TODAY'), button('7 дней', 'AN_7D'), button('30 дней', 'AN_30D')],
        [button('Вся аналитика', 'AN_ALL'), button('Монеты и выходы', 'DETAILS')],
        [button('История', 'SHOW_HISTORY'), button('Скачать CSV', 'EXPORT_CSV')],
        [button('Состояние', 'STATUS'), button('Фильтры', 'SHOW_REJECTS')],
        [button('Настройки', 'SHOW_AI_MEMORY'), button('Управление', 'CONTROLS')]]}


class Telegram:
    def __init__(self):
        self.token = os.getenv('TELEGRAM_TOKEN', '')
        self.admins = {int(x.strip()) for x in os.getenv('ADMIN_CHAT_IDS', '').split(',') if x.strip()}
        self.channel = os.getenv('SIGNAL_CHANNEL_ID', '')
        if self.token and not self.admins:
            raise ValueError('Set ADMIN_CHAT_IDS; no public access is granted by default')

    def call(self, method, data):
        if not self.token:
            return None
        req = urllib.request.Request('https://api.telegram.org/bot' + self.token + '/' + method,
                                     data=json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=35) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError('Telegram API rejected request')
        return result.get('result')

    def send(self, chat, message, menu=False):
        # Plain text avoids broken Markdown/escaping on symbols and exception text.
        lines, chunk = message.splitlines(keepends=True), ''
        for line in lines + ['']:
            if len(chunk) + len(line) > 3800 or line == '':
                if chunk:
                    data = {'chat_id': chat, 'text': chunk}
                    if menu and line == '':
                        data['reply_markup'] = keyboard()
                    self.call('sendMessage', data)
                chunk = ''
            chunk += line[:3800]

    def document(self, chat, name, content):
        boundary = 'paperbot' + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                'Content-Type: text/csv\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request('https://api.telegram.org/bot'+self.token+'/sendDocument', data=body,
                                     headers={'Content-Type': 'multipart/form-data; boundary='+boundary})
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError('Telegram export rejected')

    def notify(self, message):
        for recipient in set(self.admins) | ({self.channel} if self.channel else set()):
            try:
                self.send(recipient, message)
            except Exception as error:
                # Do not log exception URLs: a Telegram URL contains the token.
                LOG.warning('Telegram notification failed (%s); trade still recorded', type(error).__name__)


class Runtime:
    def __init__(self, store, cfg, client=None, telegram=None):
        self.store, self.cfg = store, cfg
        self.client = client or MarketClient(cfg.market)
        self.telegram = telegram or Telegram()
        self.running = threading.Event()
        self.running.set()
        self.last_success = 0.0
        self.last_error = ''

    def monitor(self, now):
        state = self.store.snapshot()
        starts = {}
        for p in state['positions'].values():
            start = p['entry_ts'] if p['last_bar_ts'] < 0 else p['last_bar_ts'] + MINUTE
            starts[p['symbol']] = min(starts.get(p['symbol'], start), start)
        for p in state['pending'].values():
            start = p['entry_ts']
            starts[p['symbol']] = min(starts.get(p['symbol'], start), start)
        replay, funding_by_symbol = [], {}
        for symbol, start in starts.items():
            if start + MINUTE > now:
                continue
            bars = self.client.klines_range(symbol, '1m', start, now - 1)
            if bars.empty or int(bars.iloc[0].ts) != start:
                raise ValueError('Missing first unprocessed 1m candle')
            validate_bars(bars, MINUTE)
            if int(bars.iloc[-1].close_ts) != (now // MINUTE) * MINUTE - 1:
                raise ValueError('Stale minute feed; pause new entries')
            funding_by_symbol[symbol] = self.client.funding(symbol, start, int(bars.iloc[-1].close_ts))
            replay.extend((int(bar['ts']), symbol, bar) for bar in bars.to_dict('records'))
        # Replay a shared chronology, including after downtime. Processing all BTC
        # bars and then all ETH bars would leak BTC's future equity into ETH's past.
        replay.sort(key=lambda item: (item[0], item[1]))

        def apply(s):
            import itertools
            closed, notes = [], []
            for ts, group in itertools.groupby(replay, key=lambda item: item[0]):
                minute = list(group)
                update_risk_state(s, ts, self.cfg)
                for _, symbol, bar in minute:
                    for key, pending in list(s['pending'].items()):
                        if pending['symbol'] != symbol or pending['entry_ts'] > bar['ts']:
                            continue
                        del s['pending'][key]
                        if pending['entry_ts'] != bar['ts']:
                            reject(s, pending['tf'], 'missed_scheduled_entry')
                            continue
                        p, reason = open_paper_position(s, pending, bar, self.cfg)
                        if reason:
                            reject(s, pending['tf'], reason)
                        else:
                            notes.append(f'PAPER ВХОД {p.symbol} {p.tf} {p.side}\n'
                                         f'Модельный вход: {p.entry:.8g}; SL {p.stop:.8g}\n'
                                         f'TP1 {p.tp1:.8g} (50%); TP2 {p.tp2:.8g} (остаток)\n'
                                         f'Объём {p.qty:.8g}; расчётный риск {p.planned_risk:.2f} USDT\n'
                                         'Вход был назначен заранее; сообщение после закрытия минутной свечи.')
                for _, symbol, bar in minute:
                    for pid, raw in list(s['positions'].items()):
                        if raw['symbol'] != symbol:
                            continue
                        trade = settle_bar(s, pid, bar, funding_by_symbol[symbol], self.cfg,
                                           update_account_risk=False)
                        if trade:
                            closed.append(trade)
                            notes.append(f'PAPER ЗАКРЫТИЕ {trade["symbol"]} {trade["tf"]} '
                                         f'{trade["exit_reason"]}\nNet {trade["net_pnl"]:+.2f} USDT; '
                                         f'{trade["net_r"]:+.3f} R (комиссии/funding учтены).')
                update_risk_state(s, ts + MINUTE - 1, self.cfg)
            return notes, closed

        messages = self.store.transact(apply) if replay else []
        for message in messages:
            self.telegram.notify(message)

    def scan(self, now):
        for tf in TF_MS:
            for symbol in SYMBOLS:
                state = self.store.snapshot()
                key = f'{symbol}_{tf}'
                latest_close = (now // TF_MS[tf]) * TF_MS[tf] - 1
                if state['last_scanned'].get(key, -1) >= latest_close:
                    continue
                if not state['enabled'] or (tf == '15m' and not state['scalp_enabled']):
                    continue
                # Do not manufacture a stale signal after downtime; the historical
                # signal may be evaluated once, but only a fresh bar may schedule entry.
                if now - latest_close > 2 * MINUTE:
                    continue
                raw = self.client.history(symbol, tf, now, self.cfg.history_limit)
                if raw.empty or int(raw.iloc[-1].close_ts) != latest_close:
                    continue
                df = build_feature_frame(raw)
                signal, reason = predict_knn(df, len(df) - 1, tf, self.cfg)
                if signal:
                    if self.client.spread_bps(symbol) > self.cfg.max_spread_bps:
                        signal, reason = None, 'spread'
                decision_now = self.client.now_ms()

                def decide(s):
                    if s['last_scanned'].get(key, -1) >= latest_close:
                        return None, []
                    s['last_scanned'][key] = latest_close
                    s['evaluated'][tf] = s['evaluated'].get(tf, 0) + 1
                    why = reason
                    if signal:
                        why = portfolio_gate(s, symbol, tf, signal['side'], decision_now, self.cfg)
                        if any(p['symbol'] == symbol for p in s['pending'].values()):
                            why = 'symbol_pending'
                        if decision_now - latest_close > 2 * MINUTE:
                            why = 'stale_signal'
                        if not why:
                            # Account for the 1s closure guard, then schedule strictly
                            # AFTER actual exchange time; never a historical open.
                            entry_ts = ((decision_now + 1000) // MINUTE + 1) * MINUTE
                            s['pending'][key] = dict(signal, symbol=symbol, tf=tf, entry_ts=entry_ts)
                            return s['pending'][key], []
                    reject(s, tf, why or 'no_signal')
                    return None, []

                pending = self.store.transact(decide)
                if pending:
                    self.telegram.notify(f'PAPER СИГНАЛ {symbol} {tf} {pending["side"]}\n'
                                         f'Ориентир {pending["reference"]:.8g}; вход на следующей полной минуте.\n'
                                         'Вход отменяется при гэпе или превышении лимита риска.\n'
                                         f'Сходные прошлые исходы: {pending["neighbor_mean_r"]:+.2f} R. '
                                         'Это не вероятность успеха.')

    def work(self):
        while self.running.is_set():
            try:
                now = self.client.now_ms()
                # If an open position cannot be monitored, skip all new entries.
                self.monitor(now)
                self.scan(self.client.now_ms())
                self.last_success, self.last_error = time.time(), ''
            except Exception as error:
                self.last_error = type(error).__name__
                LOG.exception('Market cycle failed; no new scans until next successful monitor')
            self.running.wait(0)  # no background activity occurs at import time
            time.sleep(10)

    def handle_action(self, action):
        s = self.store.snapshot()
        if action.startswith('AN_') and action[3:] in dashboard.PERIODS:
            return dashboard.analytics(self.store.trades(), action[3:])
        if action == 'DETAILS':
            return dashboard.breakdown(self.store.trades())
        if action == 'STATUS':
            return (f'СОСТОЯНИЕ / v{dashboard.VERSION}\nМодельный счёт\n'
                    f'Последний успешный цикл: {dashboard.stamp(self.last_success*1000)}\n'
                    f'Ошибка цикла: {self.last_error or "нет"}\n'
                    f'Рынок: {self.cfg.market}\n'
                    'Статус цикла отсчитывается с запуска этого процесса.')
        if action in ('SHOW_STATS', 'MENU'):

            return report(self.store, self.cfg)
        if action == 'SHOW_LIVE_TRADES':
            rows = [f'{p["symbol"]} {p["tf"]} {p["side"]}: вход {p["entry"]:.8g}; '
                    f'последняя закрытая 1m {p["mark"]:.8g}; остаток {p["remaining"]:.8g}'
                    for p in s['positions'].values()]
            return 'PAPER, оценка по закрытым 1m свечам:\n' + ('\n'.join(rows) or 'Нет открытых позиций.')
        if action == 'SHOW_REJECTS':
            lines = ['Первая причина отказа на уникальную закрытую свечу.\n'
                     'Это не оценка эффективности фильтра.']
            for tf, reasons in s['rejects'].items():
                lines.append(f'\n{tf}: оценено свечей {s["evaluated"].get(tf, 0)}')
                lines.extend(f'{k}: {v}' for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
            return '\n'.join(lines)
        if action == 'SHOW_HISTORY':
            return '\n'.join(f'{t["symbol"]} {t["tf"]} {t["exit_reason"]}: {t["net_pnl"]:+.2f} USDT'
                             for t in self.store.trades()[-20:]) or 'История пуста. Все сделки сохраняются в SQLite.'
        if action == 'SHOW_AI_MEMORY':
            return (f'ADX фиксирован: {self.cfg.min_adx:g}; монотонная адаптация отключена.\n'
                    f'KNN: {self.cfg.neighbors} неперекрывающихся исторических исходов; '
                    'оценка не является вероятностью.\n'
                    f'Риск {self.cfg.risk_fraction*100:.2f}% на сделку; общий лимит '
                    f'{self.cfg.max_total_risk*100:.2f}%; максимум {self.cfg.max_positions} позиций.\n'
                    f'Паузы таймфреймов (UTC ms): {s["tf_pause_until"]}\n'
                    'Новые параметры — проверяемая гипотеза, доходность не подтверждена.')
        if action in ('TOGGLE_SIGNALS', 'TOGGLE_SCALP'):
            key = 'enabled' if action == 'TOGGLE_SIGNALS' else 'scalp_enabled'

            def toggle(state):
                state[key] = not state[key]
                if not state[key]:
                    state['pending'] = {k: p for k, p in state['pending'].items()
                                        if key == 'scalp_enabled' and p['tf'] != '15m'}
                return state[key], []

            enabled = self.store.transact(toggle)
            return f'{key}: {"ВКЛ" if enabled else "ПАУЗА"}. Контроль открытых позиций продолжается.'
        return 'PAPER-терминал. Выберите раздел. Реальные ордера не выставляются.'

    def telegram_work(self):
        while self.running.is_set() and self.telegram.token:
            try:
                offset = self.store.snapshot()['last_update_id']
                updates = self.telegram.call('getUpdates', {'offset': offset, 'timeout': 20,
                                                            'allowed_updates': ['message', 'callback_query']}) or []
                for update in updates:
                    # Persist receipt before sending a reply. A Telegram delivery
                    # failure must not toggle the same control again on retry.
                    def receive(state):
                        if int(update['update_id']) < state['last_update_id']:
                            return False, []
                        state['last_update_id'] = int(update['update_id']) + 1
                        return True, []

                    if not self.store.transact(receive):
                        continue
                    callback = update.get('callback_query')
                    msg = callback.get('message', {}) if callback else update.get('message', {})
                    sender = callback.get('from', {}) if callback else msg.get('from', {})
                    chat = msg.get('chat', {}).get('id')
                    # Authorize the actual sender, not a group chat's numeric id.
                    if sender.get('id') in self.telegram.admins and chat is not None:
                        action = callback.get('data', '') if callback else 'MENU'
                        if action == 'EXPORT_CSV':
                            if callback:
                                self.telegram.call('answerCallbackQuery', {'callback_query_id': callback['id']})
                            self.telegram.document(chat, 'trades.csv', dashboard.ledger_csv(self.store.trades()))
                            continue
                        if action == 'CONTROLS':
                            if callback:
                                self.telegram.call('answerCallbackQuery', {'callback_query_id': callback['id']})
                            state = self.store.snapshot()
                            self.telegram.call('sendMessage', {'chat_id': chat, 'text':
                                'УПРАВЛЕНИЕ\nПауза останавливает новые входы. Открытые позиции продолжают контролироваться.',
                                'reply_markup': {'inline_keyboard': [
                                    [{'text': 'Сигналы: '+('ВКЛ → пауза' if state['enabled'] else 'пауза → ВКЛ'), 'callback_data': 'TOGGLE_SIGNALS'}],
                                    [{'text': 'Скальпинг: '+('ВКЛ → пауза' if state['scalp_enabled'] else 'пауза → ВКЛ'), 'callback_data': 'TOGGLE_SCALP'}],
                                    [{'text': 'К обзору', 'callback_data': 'SHOW_STATS'}]]}})
                            continue
                        text = self.handle_action(action)
                        if callback:
                            self.telegram.call('answerCallbackQuery', {'callback_query_id': callback['id']})
                        self.telegram.send(chat, text, menu=True)

            except Exception as error:
                LOG.warning('Telegram polling error (%s)', type(error).__name__)
                time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='store_true', help='Print local paper stats; no network calls')
    parser.add_argument('--export', help='Export entire closed-trade ledger as JSON; no network calls')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = Config.from_env()
    data_dir = Path(os.getenv('DATA_DIR', 'data_v2'))
    store = Store(data_dir / 'paper.sqlite3', cfg)
    if args.report or args.export:
        if args.report:
            print(report(store, cfg))
        if args.export:
            Path(args.export).write_text(json.dumps(store.trades(), ensure_ascii=False, indent=2), encoding='utf-8')
        store.close()
        return
    # Prevent two scanners in one data directory (cross-platform exclusive lock).
    # A stale lock after a crash must be removed only after checking the old PID.
    lock_path = data_dir / 'running.lock'
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SystemExit('running.lock exists. Confirm no other process is running before removing a stale lock.')
    os.write(lock_fd, str(os.getpid()).encode())
    os.close(lock_fd)
    runtime = None
    try:
        runtime = Runtime(store, cfg)
        threading.Thread(target=runtime.work, daemon=True).start()
        threading.Thread(target=runtime.telegram_work, daemon=True).start()
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                fresh = time.time() - runtime.last_success < 180
                body = json.dumps({'mode': 'paper', 'healthy': fresh,
                                   'last_success': runtime.last_success,
                                   'error_type': runtime.last_error}).encode()
                self.send_response(200 if fresh else 503)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), Health)
        LOG.info('PAPER ONLY. Market=%s. No exchange orders. Ctrl+C to stop.', cfg.market)
        server.serve_forever()
    finally:
        if runtime:
            runtime.running.clear()
        lock_path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
