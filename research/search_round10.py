"""Round 10 cross-contract funding dispersion.
Run from research/: python search_round5.py --cache /tmp/round10-binance
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research_futures import MIN, SYMBOLS, archive, load_data, ms, read_zip

MONTHS = ["2023-12"] + [f"2024-{m:02}" for m in range(1, 7)]
PERIODS = {"development": ("2024-01-01", "2024-04-01"),
           "selection": ("2024-04-01", "2024-07-01")}
DAY = 1440
FEE = 0.00055


FINAL = {"holdout1": ("2024-07-01", "2024-10-01"),
         "holdout2": ("2024-11-01", "2024-12-01")}


def ranking(events, cutoff, threshold):
    scores = []
    for symbol in SYMBOLS:
        ts, rates = events[symbol]
        n = np.searchsorted(ts, cutoff, side="left")
        if n < 3:
            raise ValueError("Incomplete funding warmup")
        scores.append(rates[n-3:n].mean())
    order = sorted(range(len(SYMBOLS)), key=lambda i: (scores[i], SYMBOLS[i]))
    low, high = order[0], order[-1]
    spread = scores[high]-scores[low]
    return ([low, high], float(spread)) if spread >= threshold else (None, float(spread))


def pair(entry, exit_, marks, fund_cash_per_unit, capital, slip):
    """Column order is long, short. Funding per unit is rate times mark."""
    sign = np.array([1., -1.])
    entry_fill = entry * (1 + sign * slip)
    units = sign * (capital * 0.1) / entry_fill
    entry_fee = np.abs(units) @ entry_fill * FEE
    exit_fill = exit_ * (1 - sign * slip)
    exit_fee = np.abs(units) @ exit_fill * FEE
    fund = -(fund_cash_per_unit @ units)
    liquidation_fill = marks * (1 - sign * slip)
    curve = (capital - entry_fee + (liquidation_fill - entry_fill) @ units
             - liquidation_fill @ np.abs(units) * FEE + fund[:-1])
    net = (exit_fill - entry_fill) @ units - entry_fee - exit_fee + fund[-1]
    return net, curve, fund[-1], entry_fee + exit_fee


def run(data, marks, period, threshold, slip=.00045):
    candle = np.stack([data[s]["minute"] for s in SYMBOLS], axis=1)
    t0 = int(candle[0, 0, 0])
    start, end = [(ms(x) - t0) // MIN for x in period]
    cashflow = np.zeros_like(marks)
    events_by_symbol = {}
    for i, s in enumerate(SYMBOLS):
        series = []
        for events in data[s]["funding"].values():
            for ts, rate, mark in events:
                # First minute-open at or after the real event. Exact exit
                # timestamps belong to the old position; entry is excluded.
                series.append((ts, rate))
                ix = (ts - t0 + MIN - 1) // MIN
                if ix < len(cashflow):
                    cashflow[ix, i] += rate * mark
        ts, rate = zip(*sorted(series))
        events_by_symbol[s] = (np.array(ts), np.array(rate))

    trades, equity = [], [np.array([10000.])]
    capital = 10000.
    for boundary in range(start + 15, end - DAY - 1, DAY):
        a, z = boundary + 1, boundary + 1 + DAY
        cols, spread = ranking(events_by_symbol, t0+boundary*MIN, threshold)
        if cols is None:
            continue
        flows = cashflow[a:z+1, cols].copy()
        flows[0] = 0
        net, curve, funding, fee = pair(
            candle[a, cols, 1], candle[z, cols, 1], marks[a:z, cols],
            flows.cumsum(axis=0), capital, slip)
        capital += net
        equity.extend([curve, np.array([capital])])
        trades.append(dict(entry_ms=t0 + a * MIN, exit_ms=t0 + z * MIN,
                           long=SYMBOLS[cols[0]], short=SYMBOLS[cols[1]],
                           funding_signal=spread, net=float(net), funding=float(funding), fee=float(fee)))
    values = np.concatenate(equity)
    pnl = np.array([x["net"] for x in trades])
    assert np.isclose(capital - 10000, pnl.sum())
    losses = -pnl[pnl < 0].sum()
    return dict(pairs=len(trades), net_usdt=float(capital - 10000),
                return_pct=float((capital / 10000 - 1) * 100),
                max_drawdown_pct=float((1 - values / np.maximum.accumulate(values)).max() * 100),
                profit_factor=float(pnl[pnl > 0].sum() / losses) if losses else None,
                trades=trades)


def load(cache, months):
    data, manifest = load_data(cache, months)
    if any("purpose" in x for x in manifest):
        raise ValueError("Round 10 rejects incomplete monthly candle histories")
    marks = np.column_stack([
        pd.concat([read_zip(archive(cache, s, m, "markPriceKlines"))["open"]
                   for m in months]).to_numpy()
        for s in SYMBOLS])
    return data, marks, manifest


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--final", type=Path, help="immutable preliminary lock")
    args = p.parse_args()
    if args.final:
        report=json.loads(args.final.read_text())
        if not report["eligible"]:
            raise ValueError("Holdout locked")
        report["final"],report["final_manifest"]={},{}
        for (key, period), months in zip(FINAL.items(), (["2024-06","2024-07","2024-08","2024-09"],
                                                         ["2024-10","2024-11"])):
            data, marks, manifest=load(args.cache, months)
            report["final_manifest"][key]=manifest
            report["final"][key]={label:run(data, marks, period, report["selected"], slip)
                                  for label, slip in (("base",.00045),("stress",.0009))}
        report["accepted"]=(sum(x["base"]["pairs"] for x in report["final"].values())>=50
            and all(v["pairs"]>=10 and v["net_usdt"]>0 and v["profit_factor"] is not None
                    and v["profit_factor"]>=1.15 and v["max_drawdown_pct"]<8
                    for x in report["final"].values() for v in x.values()))
    else:
        data, marks, manifest = load(args.cache, MONTHS)
        variants = {h: {k: run(data, marks, period, h) for k, period in PERIODS.items()}
                    for h in (.0001, .0002)}
        eligible = [h for h, windows in variants.items()
                    if all(v["net_usdt"] > 0 and v["pairs"] >= 30 for v in windows.values())]
        selected = max(eligible, key=lambda h: (variants[h]["selection"]["net_usdt"], -h),
                       default=None)
        neighbors = {} if selected is None else {
            round(h,6): run(data, marks, PERIODS["selection"], h)
            for h in (selected-.000025,selected+.000025)}
        report = dict(periods={**PERIODS,**FINAL}, variants=variants, selected=selected,
                      neighbors=neighbors, eligible=bool(neighbors) and
                      all(x["net_usdt"] > 0 for x in neighbors.values()),manifest=manifest)
    args.output.write_text(json.dumps(report, allow_nan=False, separators=(",",":")))


if __name__ == "__main__":
    main()
