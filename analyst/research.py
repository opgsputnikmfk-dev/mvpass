"""Deterministic portfolio replay and registered development/selection/holdout."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import numpy as np
import pandas as pd
from .config import FILTERS,validate,version
from .analysis import prepare,analyze
from .execution import Ledger
from .data import load_dataset,digest,load_snapshots
from .models import MINUTE

def experiment_configs(config):
    base=copy.deepcopy(config);base["paused"]=False;base["filters"]={k:"off" for k in FILTERS}
    variants={"baseline":base}
    for k in FILTERS:
        c=copy.deepcopy(base);c["filters"][k]="on";variants["only_"+k]=c
    variants["selected"]=copy.deepcopy(config)
    trend=copy.deepcopy(base)
    for k in ("ema","atr","volume","room"):trend["filters"][k]="on"
    variants["trend_volume"]=trend
    return variants

def daily_bootstrap(trades,seed=11):
    """Day-cluster bootstrap of mean R; descriptive, not multiplicity-adjusted."""
    if len(trades)<2:return None
    groups={}
    for t in trades:groups.setdefault(t["entry_ts"]//86400000,[]).append(t["net_r"])
    if len(groups)<5:return None
    arr=list(groups.values());rng=np.random.default_rng(seed);means=[]
    for _ in range(500):
        sample=[r for i in rng.integers(0,len(arr),len(arr)) for r in arr[i]]
        means.append(float(np.mean(sample)))
    return [float(np.quantile(means,.025)),float(np.quantile(means,.975))]

def replay(dataset,configs,start,end,store=None,progress=None):
    manifest,candles,funding=load_dataset(dataset)
    snapshots=load_snapshots(dataset,manifest);snap_index={s:0 for s in snapshots};latest_snap={}
    if start%MINUTE or end%MINUTE or start<manifest["start_ms"]+210*60*MINUTE or end>manifest["end_ms"] or end<=start:
        raise ValueError("Недостаточный прогрев / период за пределами датасета")
    for c in configs.values():
        validate(c)
        if not set(c["symbols"]).issubset(candles):raise ValueError("Не хватает монет")
    # Candidates computed once only for identical setup/risk/parameter universes.
    ref=next(iter(configs.values()))
    for c in configs.values():
        for k in ("symbols","params","setups","events"):
            if c[k]!=ref[k]:raise ValueError("Разные параметры требуют отдельного replay")
        for k in c["risk"]:
            if k not in ("fee_rate","slippage_bps") and c["risk"][k]!=ref["risk"][k]:raise ValueError("Разные правила выхода требуют отдельного replay")
    frames={s:prepare(d) for s,d in candles.items()}
    indexes={s:d.set_index("ts") for s,d in candles.items()};events={s:{e["ts"]:e for e in ee} for s,ee in funding.items()}
    accounts={name:Ledger(c,name) for name,c in configs.items()}
    for a in accounts.values():a.funding_quality=manifest.get("funding_marks","unknown")
    observed={name:{"candidates":0,"accepted":0,"missing":{}} for name in accounts}
    filter_groups={k:{"pass":[],"fail":[],"missing":[]} for k in FILTERS}
    candidate_by_id={};decisions=[];last_status={}
    # List iterators avoid per-symbol dataframe lookups for every simulated minute.
    ts_list=range(start,end,MINUTE)
    matrices={s:d[(d.ts>=start)&(d.ts<end)].to_dict("records") for s,d in candles.items()}
    for idx,ts in enumerate(ts_list):
        bars={s:rows[idx] for s,rows in matrices.items()}
        fs={s:[ev[ts]] for s,ev in events.items() if ts in ev}
        for a in accounts.values():a.step(ts,bars,manifest["metadata"],fs)
        if (ts+MINUTE)%(5*MINUTE):continue
        cutoff=ts+MINUTE
        for symbol in ref["symbols"]:
            # Real historical microstructure must be supplied with availability
            # timestamps. No present-day snapshot is allowed in past decisions.
            if symbol in snapshots:
                j=snap_index[symbol];ss=snapshots[symbol]
                while j<len(ss) and ss[j]["ts"]<=cutoff:latest_snap[symbol]=ss[j];j+=1
                snap_index[symbol]=j
            snap=latest_snap.get(symbol)
            if manifest["synthetic"]:
                snap={"ts":cutoff,"spread_bps":2.,"depth_usdt":1e6,"funding_rate":.0001,"oi_change_pct":.1}
            found,status=analyze(symbol,frames[symbol],cutoff,ref,snap,frames.get("BTCUSDT"));last_status[symbol]=status
            for c in found:
                candidate_by_id[c.id]=c
                for name,a in accounts.items():
                    ok,reasons=a.propose(c);observed[name]["candidates"]+=1;observed[name]["accepted"]+=int(ok)
                    for k,mode in a.config["filters"].items():
                        if mode=="on" and c.checks[k].passed is None:observed[name]["missing"][k]=observed[name]["missing"].get(k,0)+1
                    decisions.append((name,c.id,ok,reasons))
        if progress and idx%1440<5:progress(f"Replay {idx//1440+1} day(s)")
    for a in accounts.values():a.finish(end)
    # Descriptive attribution of executed baseline trades, labelled explicitly.
    baseline=accounts.get("baseline")
    if baseline:
        for t in baseline.closed:
            c=candidate_by_id[t["candidate_id"]]
            for k,ch in c.checks.items():filter_groups[k]["missing" if ch.passed is None else "pass" if ch.passed else "fail"].append(t["net_r"])
    summaries={}
    for name,a in accounts.items():
        stat=a.stats();stat.update(observed[name]);stat["mean_r_ci95_day_bootstrap"]=daily_bootstrap(a.closed)
        stat["synthetic"]=manifest["synthetic"]
        stat["assessment"]="DEMO_ONLY" if manifest["synthetic"] else "UNAVAILABLE_FEATURES" if stat["missing"] else "RESEARCH_ONLY"
        stat["by_symbol"]={s:{"trades":len(tt),"net_pnl":sum(t["net_pnl"] for t in tt)} for s in ref["symbols"] if (tt:=[t for t in a.closed if t["symbol"]==s])}
        summaries[name]=stat
    attribution={k:{group:{"n":len(v),"mean_r":float(np.mean(v)) if v else None} for group,v in groups.items()} for k,groups in filter_groups.items()}
    if store:
        generation=version(ref)
        state={"accounts":{generation+":"+name:a.dump() for name,a in accounts.items()},"market":last_status,
               "last_ts":end-MINUTE,"last_analysis":{},"updated_at":end,"expected_funding":{}}
        mapped=[(generation+":"+name,cid,ok,reasons) for name,cid,ok,reasons in decisions]
        store.commit_cycle(state,list(candidate_by_id.values()),mapped,[])
        for s,d in candles.items():store.add_candles(s,d.tail(20000))
    return {"manifest":manifest,"start_ms":start,"end_ms":end,"summaries":summaries,
            "attribution":attribution,"attribution_note":"Группировка только исполненных baseline-сделок; не причинная оценка и не независимый портфель",
            "accounts":accounts,"candidates":candidate_by_id}

def save_report(result,path):
    root=Path(path);root.mkdir(parents=True,exist_ok=True)
    public={k:v for k,v in result.items() if k not in ("accounts","candidates")}
    (root/"report.json").write_text(json.dumps(public,ensure_ascii=False,indent=2,allow_nan=False))
    rows=[]
    for name,a in result["accounts"].items():
        trades=pd.DataFrame(a.closed)
        if not trades.empty:
            for k in ("entry_ts","exit_ts"):trades[k]=pd.to_datetime(trades[k],unit="ms",utc=True).dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            trades.to_csv(root/(name+"_trades.csv"),index=False,encoding="utf-8-sig")
        pd.DataFrame(a.equity_curve,columns=["timestamp_ms","equity_usdt"]).to_csv(root/(name+"_equity.csv"),index=False)
        s=result["summaries"][name];rows.append({k:v for k,v in s.items() if not isinstance(v,(dict,list))})
    pd.DataFrame(rows).to_csv(root/"comparison.csv",index=False,encoding="utf-8-sig")
    # Every candidate, including rejected ones, with its contemporaneous features.
    with (root/"candidates.jsonl").open("w") as f:
        for c in result["candidates"].values():f.write(json.dumps(c.to_dict(),ensure_ascii=False,allow_nan=False)+"\n")
    from .charts import equity_chart
    equity_chart(result["accounts"],root/"equity.png",synthetic=result["manifest"]["synthetic"])

def register(dataset,config,out):
    m,_,_=load_dataset(dataset);start=m["start_ms"]+14*86400000;end=m["end_ms"]
    if end-start<6*86400000:raise ValueError("Нужно ≥20 дней данных: 14 прогрев и ≥6 проверка; для выводов значительно больше")
    length=end-start;b=start+int(length*.5)//MINUTE*MINUTE;c=start+int(length*.75)//MINUTE*MINUTE
    root=Path(out);root.mkdir(parents=True,exist_ok=True)
    protocol={"dataset_manifest_sha256":digest(Path(dataset)/"manifest.json"),"config":validate(config),
              "periods":{"develop":[start,b],"select":[b,c],"holdout":[c,end]},
              "selection":{"min_trades":30,"profit_factor":1.2,"mean_r":0.0,"max_dd_pct":8.0},
              "warning":"Порог отбора не доказывает преимущество. CI описательный, без поправки на перебор. Результаты на demo не проходят отбор."}
    with (root/"protocol.json").open("x") as f:json.dump(protocol,f,ensure_ascii=False,indent=2)
    (root/"protocol.sha256").write_text(digest(root/"protocol.json"))
    return protocol

def run_stage(dataset,out,stage,progress=print):
    root=Path(out);pp=root/"protocol.json"
    if digest(pp)!=(root/"protocol.sha256").read_text():raise ValueError("Протокол изменён после регистрации")
    p=json.loads(pp.read_text())
    if digest(Path(dataset)/"manifest.json")!=p["dataset_manifest_sha256"]:raise ValueError("Изменён датасет")
    if stage not in ("develop","select","holdout"):raise ValueError("Неизвестный этап")
    # Claim BEFORE reading the period; a failed run is still an exposure, not a
    # fresh holdout on a retry. Create a new protocol and disclose reuse to retry.
    prereq={"select":"develop","holdout":"select"}.get(stage)
    if prereq and not (root/prereq/"report.json").exists():raise ValueError("Сначала завершите "+prereq)
    cs=experiment_configs(p["config"])
    if stage=="holdout":
        selection=json.loads((root/"select"/"report.json").read_text())["summaries"]
        rules=p["selection"]
        valid=[n for n,s in selection.items() if not s["synthetic"] and not s["missing"] and s["trades"]>=rules["min_trades"] and (s["profit_factor"] or 0)>=rules["profit_factor"] and (s["mean_r"] or 0)>rules["mean_r"] and s["max_drawdown_pct"]<=rules["max_dd_pct"]]
        if not valid:raise ValueError("Ни один вариант не прошёл отбор; holdout не открывался")
        best=max(valid,key=lambda n:(selection[n]["mean_r"],n));chosen=cs[best]
        stressed=copy.deepcopy(chosen);stressed["risk"]["fee_rate"]*=1.5;stressed["risk"]["slippage_bps"]*=2
        cs={best:chosen,best+"_cost_stress":stressed}
    claim=root/(stage+".exposed")
    with claim.open("x") as f:f.write("Period opened; do not relabel it as independent after changes.\n")
    result=replay(dataset,cs,*p["periods"][stage],progress=progress)
    save_report(result,root/stage)
    return result
