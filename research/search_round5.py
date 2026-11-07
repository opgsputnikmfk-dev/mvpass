"""Round 5 preliminary paired momentum; holdout downloads are intentionally absent.
Run from research/: python search_round5.py --cache /tmp/round5-binance
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


def ranking(close, boundary, horizon):
    score = close[boundary - 1] / close[boundary - 1 - horizon * DAY] - 1
    order = sorted(range(len(SYMBOLS)), key=lambda i: (-score[i], SYMBOLS[i]))
    return order[0], order[-1]


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


def run(data, marks, period, horizon, slip=.00045):
    candle = np.stack([data[s]["minute"] for s in SYMBOLS], axis=1)
    t0 = int(candle[0, 0, 0])
    start, end = [(ms(x) - t0) // MIN for x in period]
    cashflow = np.zeros_like(marks)
    for i, s in enumerate(SYMBOLS):
        for events in data[s]["funding"].values():
            for ts, rate, mark in events:
                # First minute-open at or after the real event. Exact exit
                # timestamps belong to the old position; entry is excluded.
                ix = (ts - t0 + MIN - 1) // MIN
                if ix < len(cashflow):
                    cashflow[ix, i] += rate * mark

    trades, equity = [], [np.array([10000.])]
    capital = 10000.
    for boundary in range(start, end - DAY - 1, DAY):
        a, z = boundary + 1, boundary + 1 + DAY
        cols = list(ranking(candle[:, :, 4], boundary, horizon))
        flows = cashflow[a:z+1, cols].copy()
        flows[0] = 0
        net, curve, funding, fee = pair(
            candle[a, cols, 1], candle[z, cols, 1], marks[a:z, cols],
            flows.cumsum(axis=0), capital, slip)
        capital += net
        equity.extend([curve, np.array([capital])])
        trades.append(dict(entry_ms=t0 + a * MIN, exit_ms=t0 + z * MIN,
                           long=SYMBOLS[cols[0]], short=SYMBOLS[cols[1]],
                           net=float(net), funding=float(funding), fee=float(fee)))
    values = np.concatenate(equity)
    pnl = np.array([x["net"] for x in trades])
    assert np.isclose(capital - 10000, pnl.sum())
    losses = -pnl[pnl < 0].sum()
    return dict(pairs=len(trades), net_usdt=float(capital - 10000),
                return_pct=float((capital / 10000 - 1) * 100),
                max_drawdown_pct=float((1 - values / np.maximum.accumulate(values)).max() * 100),
                profit_factor=float(pnl[pnl > 0].sum() / losses) if losses else None,
                trades=trades)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("round5_results.json"))
    args = p.parse_args()
    data, manifest = load_data(args.cache, MONTHS)
    if any("purpose" in x for x in manifest):
        raise ValueError("Round 5 rejects incomplete monthly candle histories")
    marks = np.column_stack([
        pd.concat([read_zip(archive(args.cache, s, m, "markPriceKlines"))["open"]
                   for m in MONTHS]).to_numpy()
        for s in SYMBOLS])
    variants = {h: {k: run(data, marks, period, h) for k, period in PERIODS.items()}
                for h in (7, 14)}
    eligible = [h for h, windows in variants.items()
                if all(v["net_usdt"] > 0 and v["pairs"] >= 60 for v in windows.values())]
    selected = max(eligible, key=lambda h: (variants[h]["selection"]["net_usdt"], -h),
                   default=None)
    neighbors = {} if selected is None else {
        h: run(data, marks, PERIODS["selection"], h)
        for h in ((6, 8) if selected == 7 else (12, 16))}
    report = dict(periods=PERIODS, variants=variants, selected=selected,
                  neighbors=neighbors, holdout_eligible=bool(neighbors) and
                  all(x["net_usdt"] > 0 for x in neighbors.values()),
                  holdout_opened=False, manifest=manifest)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False))
    for h, windows in variants.items():
        print(h, {k: {a: b for a,b in v.items() if a != "trades"}
                  for k,v in windows.items()})
    print("Selected:", selected, "holdout eligible:", report["holdout_eligible"])


if __name__ == "__main__":
    main()
