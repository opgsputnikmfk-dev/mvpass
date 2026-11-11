"""Preregistered New York session momentum proxy; offline Binance archives only."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import research_futures as rf

from search_round6 import load as load_base

def load(cache, months):
    data, manifest = load_base(cache, months)
    close = pd.concat([rf.read_zip(rf.archive(cache, "BTCUSDT", m, "klines"))["close"] for m in months]).to_numpy()
    return (data, close), manifest

FEE = .00055
PRE = {"development": ("2024-01-01", "2024-04-01"),
       "selection": ("2024-04-01", "2024-07-01")}
FINAL = {"holdout1": ("2024-07-01", "2024-10-01"),
         "holdout2": ("2024-11-01", "2024-12-01")}


def session_minutes(start, end):
    for d in pd.date_range(start, end, inclusive="left"):
        if d.weekday() >= 5:
            continue
        yield tuple(int((d+pd.Timedelta(minutes=m)).tz_localize("America/New_York").timestamp()*1000)
                    for m in (570, 599, 931, 960))


def run(dataset, period, threshold, slip=.00045):
    data, data_close = dataset
    t0, o, mark, _, _, fund = data
    start, end = [(rf.ms(t)-t0)//rf.MIN for t in period]
    if start<0 or end>len(o):
        raise ValueError("Incomplete period")
    balance = 10000.
    curve = [np.array([balance])]
    trades = []
    for ts in session_minutes(*period):
        first, last, a, z = [(t-t0)//rf.MIN for t in ts]
        if first<start or z>=end:
            continue
        change = data_close[last]/o[first]-1
        if not change or abs(change)<threshold:
            continue
        side = 1 if change>0 else -1
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
        trades.append([t0+a*rf.MIN,t0+z*rf.MIN,side,change,entry,exit_,
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
        for (k,w), months in zip(FINAL.items(), (["2024-07","2024-08","2024-09"], ["2024-11"])):
            data, manifest=load(args.cache,months)
            report["final_manifest"][k]=manifest
            report["final"][k]={label:run(data,w,report["selected"],slip)
                                for label,slip in [("base",.00045),("stress",.0009)]}
        report["accepted"]=(sum(x["base"]["count"] for x in report["final"].values())>=65
            and all(v["count"]>=15 and v["net_usdt"]>0 and v["profit_factor"] is not None
                    and v["profit_factor"]>=1.15 and v["max_drawdown_pct"]<8
                    for x in report["final"].values() for v in x.values()))
    else:
        data,manifest=load(args.cache,[f"2024-{m:02}" for m in range(1,7)])
        results={t:{k:run(data,w,t) for k,w in PRE.items()} for t in (0.,.001)}
        selected=max((t for t,windows in results.items() if
                      all(x["count"]>=35 and x["net_usdt"]>0 for x in windows.values())),
                     default=None,key=lambda t:(results[t]["selection"]["net_usdt"],-t))
        neighbors={} if selected is None else {round(t,5):run(data,PRE["selection"],t)
                                                for t in ((.00025,.0005) if selected==0 else (.00075,.00125))}
        report=dict(variants=results,selected=selected,neighbors=neighbors,
                    eligible=bool(neighbors) and all(x["net_usdt"]>0 for x in neighbors.values()),
                    manifest=manifest,periods={**PRE,**FINAL},
                    trade_columns=["entry_ms","exit_ms","side","opening_return","entry_fill","exit_fill",
                                   "net_usdt","funding_cashflow","fees"])
    args.output.write_text(json.dumps(report,allow_nan=False,separators=(",",":")))


if __name__=="__main__":
    main()
