"""Chronological out-of-sample replay for ONE symbol/timeframe using 1m CSV.

No parameter search. Training outcomes are fully in the past and purged.
KNN labels use conservative timeframe OHLC; portfolio replay uses 1m OHLC.
This is a single-series experiment, not a multi-symbol portfolio backtest.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

import numpy as np
import pandas as pd

from bot import (Config, MINUTE, TF_MS, build_feature_frame, exit_fill, new_state,
                 open_paper_position, predict_knn, reject, settle_bar, summarize_trades,
                 update_risk_state, validate_bars, Position, account_equity)


def load_minutes(path):
    frame = pd.read_csv(path)
    required = ['ts', 'open', 'high', 'low', 'close', 'volume', 'close_ts']
    if not set(required).issubset(frame.columns):
        raise ValueError('CSV needs ' + ', '.join(required) + '; times are UTC milliseconds')
    frame = frame[required].apply(pd.to_numeric, errors='raise')
    validate_bars(frame, MINUTE)
    if frame.empty or not (frame.ts % MINUTE == 0).all():
        raise ValueError('Empty or non-aligned minute data')
    return frame


def aggregate(minutes, tf):
    d = minutes.copy()
    d['bucket'] = (d.ts // TF_MS[tf]) * TF_MS[tf]
    bars = d.groupby('bucket', sort=True).agg(
        ts=('ts', 'first'), open=('open', 'first'), high=('high', 'max'),
        low=('low', 'min'), close=('close', 'last'), volume=('volume', 'sum'),
        close_ts=('close_ts', 'last'), count=('ts', 'count'))
    bars = bars.loc[(bars['count'] == TF_MS[tf] // MINUTE)
                    & (bars.ts == bars.index)
                    & (bars.close_ts == bars.index + TF_MS[tf] - 1)]
    bars = bars.drop(columns='count').reset_index(drop=True)
    validate_bars(bars, TF_MS[tf])
    return build_feature_frame(bars)


def run_backtest(minutes, tf, symbol, cfg, start_ms=None, funding=None,
                 entry_delay_minutes=1):
    if entry_delay_minutes < 0 or entry_delay_minutes > 10:
        raise ValueError('Invalid execution delay')
    if cfg.market == 'usdm' and funding is None:
        raise ValueError('USDM backtest requires funding history; zero funding is not assumed')
    features = aggregate(minutes, tf)
    minimum_index = cfg.warmup + cfg.horizon * cfg.neighbors + cfg.horizon
    if len(features) <= minimum_index + 1:
        raise ValueError(f'Need more than {minimum_index+1} complete {tf} candles INCLUDING warmup')
    default_start = int(features.iloc[minimum_index].close_ts)
    evaluation_start = max(default_start, int(start_ms or default_start))
    if evaluation_start >= int(minutes.iloc[-1].close_ts):
        raise ValueError('No evaluation period after warmup/start')
    state, ledger, curve = new_state(cfg), [], []
    close_to_index = {int(ts): i for i, ts in enumerate(features.close_ts)}
    records = minutes.loc[minutes.close_ts >= evaluation_start].to_dict('records')
    fund_events = sorted(funding or [], key=lambda e: int(e['fundingTime']))
    fund_index = 0
    for bar in records:
        ts, end = int(bar['ts']), int(bar['close_ts'])
        update_risk_state(state, ts, cfg)
        current_funding = []
        while fund_index < len(fund_events) and int(fund_events[fund_index]['fundingTime']) <= end:
            event = fund_events[fund_index]
            if int(event['fundingTime']) >= ts:
                current_funding.append(event)
            fund_index += 1
        for key, pending in list(state['pending'].items()):
            if pending['entry_ts'] <= ts:
                del state['pending'][key]
                _, why = open_paper_position(state, pending, bar, cfg)
                if why:
                    reject(state, tf, why)
        for pid in list(state['positions']):
            closed = settle_bar(state, pid, bar, current_funding, cfg)
            if closed:
                ledger.append(closed)
        equity = update_risk_state(state, end, cfg)
        curve.append({'ts': end, 'equity': equity, 'balance': state['balance']})
        i = close_to_index.get(end)
        if i is not None and i >= minimum_index:
            state['evaluated'][tf] = state['evaluated'].get(tf, 0) + 1
            signal, why = predict_knn(features, i, tf, cfg)
            if signal:
                if state['positions'] or state['pending']:
                    reject(state, tf, 'symbol_already_open_or_pending')
                else:
                    # 1 minute default = signal computed shortly after candle close,
                    # then committed for the following complete minute open.
                    state['pending'][symbol] = dict(signal, symbol=symbol, tf=tf,
                                                   entry_ts=end + 1 + entry_delay_minutes * MINUTE)
            else:
                reject(state, tf, why)
    # Do not quietly discard an open losing position at the sample boundary.
    if records:
        last = records[-1]
        for pid, raw in list(state['positions'].items()):
            p = Position(**raw)
            before = p.net_pnl
            exit_fill(p, float(last['close']), p.remaining, int(last['close_ts']), 'END_OF_DATA')
            state['balance'] += p.net_pnl - before
            del state['positions'][pid]
            row = dataclasses.asdict(p)
            row.update(net_pnl=p.net_pnl, net_r=p.net_pnl / p.planned_risk)
            ledger.append(row)
        curve[-1]['equity'] = update_risk_state(state, int(last['close_ts']), cfg)
        curve[-1]['balance'] = state['balance']
    stats = summarize_trades(ledger)
    stats.update(mode='paper_single_symbol_chronological', symbol=symbol, timeframe=tf,
                 evaluation_start_ms=evaluation_start, evaluation_end_ms=int(minutes.iloc[-1].close_ts),
                 initial_equity=cfg.initial_equity, final_equity=account_equity(state),
                 return_pct=100*(account_equity(state)/cfg.initial_equity-1),
                 max_observed_drawdown_pct=state['max_drawdown']*100,
                 ambiguous_bars=sum(t['ambiguous_bars'] for t in ledger),
                 config=dataclasses.asdict(cfg), first_rejections=state['rejects'],
                 entry_delay_minutes=entry_delay_minutes,
                 funding_in_knn_labels=False,
                 limitations=['Not a combined portfolio backtest',
                              'KNN labels use coarse OHLC; replay uses 1m OHLC',
                              'Historical spread/order queue/fill probability unavailable',
                              'Fixed adverse slippage; not exchange executions',
                              'Drawdown uses minute-close liquidation estimates, not intraminute lows',
                              'USDM KNN label scores omit funding; replay requires funding',
                              'First rejection counts do not measure filter benefit'])
    # Fixed chronological thirds reveal regime dependence without optimizing to them.
    edges = np.linspace(evaluation_start, int(minutes.iloc[-1].close_ts) + 1, 4)
    stats['chronological_thirds'] = []
    for left, right in zip(edges[:-1], edges[1:]):
        group = [t for t in ledger if left <= t['exit_ts'] < right]
        stats['chronological_thirds'].append(dict(start_ms=int(left), end_ms=int(right),
                                                  **summarize_trades(group)))
    return stats, ledger, curve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', required=True)
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--tf', choices=TF_MS, default='15m')
    parser.add_argument('--market', choices=('spot', 'usdm'), default='spot')
    parser.add_argument('--funding', help='JSON envelope produced by download_data.py')
    parser.add_argument('--start', help='First evaluation date, UTC; earlier CSV rows are training/warmup')
    parser.add_argument('--fee-bps', type=float, default=10)
    parser.add_argument('--slippage-bps', type=float, default=2)
    parser.add_argument('--entry-delay-minutes', type=int, default=1)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    cfg = Config(market=args.market, fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
    minutes = load_minutes(args.csv)
    # Metadata binds downloaded candles to their market/symbol; no accidental
    # backtest of futures against a similarly named spot file.
    meta_path = Path(args.csv + '.meta.json')
    if not meta_path.exists():
        raise ValueError('Missing CSV metadata; see README for the documented import format')
    metadata = json.loads(meta_path.read_text())
    if metadata.get('market') != args.market or metadata.get('symbol') != args.symbol:
        raise ValueError('Market/symbol mismatch in CSV metadata')
    events = None
    if args.market == 'usdm':
        if not args.funding:
            raise ValueError('--funding is mandatory for usdm')
        envelope = json.loads(Path(args.funding).read_text())
        if (envelope.get('market') != 'usdm' or envelope.get('symbol') != args.symbol
                or envelope['start_ms'] > int(minutes.iloc[0].ts)
                or envelope['end_ms'] < int(minutes.iloc[-1].close_ts)):
            raise ValueError('Funding metadata does not cover the candle period/market')
        events = envelope['events']
    start = int(pd.Timestamp(args.start, tz='UTC').timestamp() * 1000) if args.start else None
    stats, ledger, curve = run_backtest(minutes, args.tf, args.symbol, cfg, start, events,
                                       args.entry_delay_minutes)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'summary.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2, allow_nan=False))
    (out / 'trades.json').write_text(json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False))
    pd.DataFrame(curve).to_csv(out / 'equity.csv', index=False)
    print(json.dumps(stats, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
