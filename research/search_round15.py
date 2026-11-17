"""Preregistered BTC premium reversal; offline Binance archives only."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import research_futures as rf

from search_round6 import load as load_base
FEE=.00055
from search_round8 import PRE, FINAL

def load(cache, months):
    data, manifest = load_base(cache, months)
    t0,o,mark,_,_,fund=data
    frames=[]
    import hashlib
    for m in months:
        rel=f"premiumIndexKlines/BTCUSDT/1m/BTCUSDT-1m-{m}.zip"
        url="https://data.binance.vision/data/futures/um/monthly/"+rel
        p=cache/rel
        p.parent.mkdir(parents=True,exist_ok=True)
        expected=rf.request(url+".CHECKSUM").decode().split()[0]
        raw=p.read_bytes() if p.exists() else rf.request(url)
        if hashlib.sha256(raw).hexdigest()!=expected:
            raise ValueError("Premium checksum mismatch")
        p.write_bytes(raw)
        frames.append(rf.read_zip(p))
        manifest.append(dict(file=rel,sha256=expected))
    f=pd.concat(frames,ignore_index=True)
    if not (np.array_equal(f.open_time,t0+np.arange(len(o))*rf.MIN)
            and np.array_equal(f.close_time,f.open_time+rf.MIN-1)
            and np.isfinite(f.close).all()):
        raise ValueError("Incomplete premium coverage")
    return (t0,o,mark,f.close.to_numpy(),None,fund),manifest


def run(data, period, threshold, slip=.00045):
    t0, o, mark, close, vol, fund = data
    start, end = [(rf.ms(t)-t0)//rf.MIN for t in period]
    if start<1500 or end>len(o):
        raise ValueError("Incomplete period or warmup")
    balance = 10000.
    curve = [np.array([balance])]
    trades = []
    for i in range(start, end-61, 60):
        imbalance = close[i-60:i].mean()
        if abs(imbalance)<threshold:
            continue
        a, z = i+1, i+61
        side = -1 if imbalance>0 else 1
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
        for (k,w), months in zip(FINAL.items(), (["2024-06","2024-07","2024-08","2024-09"], ["2024-10","2024-11"])):
            data, manifest=load(args.cache,months)
            report["final_manifest"][k]=manifest
            report["final"][k]={label:run(data,w,report["selected"],slip)
                                for label,slip in [("base",.00045),("stress",.0009)]}
        report["accepted"]=(sum(x["base"]["count"] for x in report["final"].values())>=100
            and all(v["count"]>=20 and v["net_usdt"]>0 and v["profit_factor"] is not None
                    and v["profit_factor"]>=1.15 and v["max_drawdown_pct"]<8
                    for x in report["final"].values() for v in x.values()))
    else:
        data,manifest=load(args.cache,["2023-12"]+[f"2024-{m:02}" for m in range(1,7)])
        results={t:{k:run(data,w,t) for k,w in PRE.items()} for t in (.0005,.001)}
        selected=max((t for t,windows in results.items() if
                      all(x["count"]>=60 and x["net_usdt"]>0 for x in windows.values())),
                     default=None,key=lambda t:(results[t]["selection"]["net_usdt"],-t))
        neighbors={} if selected is None else {round(t,5):run(data,PRE["selection"],t)
                                                for t in (selected-.0001,selected+.0001)}
        report=dict(variants=results,selected=selected,neighbors=neighbors,
                    eligible=bool(neighbors) and all(x["net_usdt"]>0 for x in neighbors.values()),
                    manifest=manifest,periods={**PRE,**FINAL},
                    trade_columns=["entry_ms","exit_ms","side","mean_premium","entry_fill","exit_fill",
                                   "net_usdt","funding_cashflow","fees"])
    args.output.write_text(json.dumps(report,allow_nan=False,separators=(",",":")))


if __name__=="__main__":
    main()
