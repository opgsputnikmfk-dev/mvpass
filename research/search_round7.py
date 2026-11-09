"""Preregistered BTCUSDT quarter-hour opening-flow proxy; offline Binance archives only."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import research_futures as rf

SYMBOL = "BTCUSDT"
FEE = .00055
PRE = {"development": ("2024-01-01", "2024-04-01"),
       "selection": ("2024-04-01", "2024-07-01")}
FINAL = {"holdout1": ("2024-11-01", "2024-12-01"),
         "holdout2": ("2025-12-01", "2026-01-01")}


def flow(buy, volume, i):
    opens = slice(i-60, i, 15)
    v = volume[opens].sum()
    return 2 * buy[opens].sum() / v - 1 if v > 0 else 0.


def load(cache, months):
    rf.SYMBOLS = (SYMBOL,)
    data, manifest = rf.load_data(cache, months)
    if any("purpose" in a for a in manifest):
        raise ValueError("Incomplete monthly coverage")
    data = data[SYMBOL]
    def frames(kind):
        return pd.concat([rf.read_zip(rf.archive(cache, SYMBOL, m, kind))
                          for m in months], ignore_index=True)
    d = frames("klines")
    mark = frames("markPriceKlines").open.to_numpy()
    buy, vol = (d[k].to_numpy() for k in ("taker_buy_quote_volume", "quote_volume"))
    if not (np.isfinite(buy).all() and np.isfinite(vol).all()
            and np.all((buy >= 0) & (vol >= buy))):
        raise ValueError("Invalid volume data")
    o = d.open.to_numpy()
    t0 = int(d.open_time.iloc[0])
    fund = np.zeros(len(d))
    for events in data["funding"].values():
        for t, rate, price in events:
            i = (t-t0+rf.MIN-1)//rf.MIN
            if i<len(fund):
                fund[i] += rate*price
    return (t0, o, mark, buy, vol, fund), manifest


def run(data, period, threshold, slip=.00045):
    t0, o, mark, buy, vol, fund = data
    start, end = [(rf.ms(t)-t0)//rf.MIN for t in period]
    if start<60 or end>len(o):
        raise ValueError("Incomplete period or warmup")
    balance = 10000.
    curve = [np.array([balance])]
    trades = []
    for i in range(start, end-241, 240):
        imbalance = flow(buy, vol, i)
        if abs(imbalance)<threshold:
            continue
        a, z = i+1, i+241
        side = 1 if imbalance>0 else -1
        entry, exit_ = o[a]*(1+side*slip), o[z]*(1-side*slip)
        units = balance*.1/entry
        entry_fee = entry*units*FEE
        exit_fee = exit_*units*FEE
        funding = -side*units*np.r_[0., fund[a+1:z+1]].cumsum()
        liquidation = mark[a:z]*(1-side*slip)
        curve.append(balance+units*side*(liquidation-entry)
                     -entry_fee-units*FEE*liquidation+funding[:-1])
        net = units*side*(exit_-entry)-entry_fee-exit_fee+funding[-1]
        balance += net
        curve.append(np.array([balance]))
        trades.append([t0+a*rf.MIN,t0+z*rf.MIN,side,imbalance,entry,exit_,
                       float(net),float(funding[-1]),float(entry_fee+exit_fee)])
    eq = np.concatenate(curve)
    wins = sum(t[6] for t in trades if t[6]>0)
    losses = -sum(t[6] for t in trades if t[6]<0)
    assert np.isclose(balance-10000,wins-losses)
    return dict(count=len(trades),net_usdt=balance-10000,return_pct=(balance/10000-1)*100,
                max_drawdown_pct=float((1-eq/np.maximum.accumulate(eq)).max()*100),
                profit_factor=wins/losses if losses else None,trades=trades)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--cache",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--final",type=Path,help="saved preliminary lock; run only when eligible")
    args=p.parse_args()
    if args.final:
        report=json.loads(args.final.read_text())
        if not report["eligible"]:
            raise ValueError("Holdout is locked")
        report["final_manifest"], report["final"] = {}, {}
        for (k,w), months in zip(FINAL.items(), (["2024-10","2024-11"], ["2025-11","2025-12"])):
            data, manifest=load(args.cache,months)
            report["final_manifest"][k]=manifest
            report["final"][k]={label:run(data,w,report["selected"],slip)
                                for label,slip in [("base",.00045),("stress",.0009)]}
        report["accepted"]=(sum(x["base"]["count"] for x in report["final"].values())>=100
            and all(v["net_usdt"]>0 and v["profit_factor"] is not None
                    and v["profit_factor"]>=1.15 and v["max_drawdown_pct"]<8
                    for x in report["final"].values() for v in x.values()))
    else:
        data,manifest=load(args.cache,["2023-12"]+[f"2024-{m:02}" for m in range(1,7)])
        results={t:{k:run(data,w,t) for k,w in PRE.items()} for t in (.1,.2)}
        selected=max((t for t,windows in results.items() if
                      all(x["count"]>=60 and x["net_usdt"]>0 for x in windows.values())),
                     default=None,key=lambda t:(results[t]["selection"]["net_usdt"],-t))
        neighbors={} if selected is None else {round(t,3):run(data,PRE["selection"],t)
                                                for t in (selected-.025,selected+.025)}
        report=dict(variants=results,selected=selected,neighbors=neighbors,
                    eligible=bool(neighbors) and all(x["net_usdt"]>0 for x in neighbors.values()),
                    manifest=manifest,periods={**PRE,**FINAL},
                    trade_columns=["entry_ms","exit_ms","side","imbalance","entry_fill","exit_fill",
                                   "net_usdt","funding_cashflow","fees"])
    args.output.write_text(json.dumps(report,allow_nan=False,separators=(",",":")))


if __name__=="__main__":
    main()
