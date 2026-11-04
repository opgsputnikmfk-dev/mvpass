"""Abnormal completed-day return hypotheses, not a paper replication."""
import argparse,json
from pathlib import Path
import numpy as np
import pandas as pd
import research_futures as r
CANDIDATES=[('shock_momentum',1.5),('shock_momentum',2.),('shock_reversal',1.5),('shock_reversal',2.)]
PLAN={'round':4,'registered':'2026-11-04','candidates':CANDIDATES,'development':['2024-01-01','2024-04-01'],'selection':['2024-04-01','2024-07-01'],'holdout':[['2024-07-01','2024-10-01'],['2024-10-01','2024-12-01']],'warmup':'2023-12','source':'https://doi.org/10.1007/s11408-020-00357-1','hypothesis':'Trade continuation or reversal after a fully completed abnormal UTC day. Daily open-to-close log return, z-score relative to prior 20 daily returns (mean/std ddof=0, both shifted). Absolute z>=1.5 or >=2.0; side uses sign of actual daily return. One entry attempt at next day 00:01 UTC; stop 2 completed-hour ATR, no target, timeout 23h. Not paper replication; no symbol-specific sign rules.','selection_rule':'Require positive development AND selection, >=30 selection trades, selection PF>=1.15 and DD<8%; highest selection PF wins; listed order breaks ties. Otherwise reject without opening holdout.','admission':'Both independent periods positive, PF>=1.15 DD<8%; >=100 trades combined; both periods positive with adverse execution doubled to 9bps per side and both threshold neighbors +/-10%. No retuning or candidate switching. Prospective observation still required.','costs':r.PLAN['costs'],'risk':r.PLAN['risk'],'independence':'Jan-Jun2024 already inspected in round3: screening only. Jul-Nov2024 never loaded in prior rounds; reserved. Paper sample ends Sep2019. Dec2023 warmup.','limitations':['Original study spot assets/venues and different rule design; no transfer assumed.','BTC ETH SOL survivor universe, fixed slippage instead of bid/ask, minute stop-first and funding mark-open proxy.','Repeated searching, small event counts, no statistical significance inferred from PnL.']}
def bars(f,minutes):
    f=f[['open','high','low','close','volume']].copy();f.index.name='close_ts'
    f['bucket']=f.index//(minutes*r.MIN)*(minutes*r.MIN)
    h=f.groupby('bucket').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),n=('open','size'))
    h=h[h.n==minutes//15].drop(columns='n');h.index+=minutes*r.MIN-1
    return h

def make_signals(x,kind,threshold):
    d=bars(x['features'],1440);h=r.features(bars(x['features'],60))
    ret=np.log(d.close/d.open);past=ret.shift(1)
    z=(ret-past.rolling(20).mean())/past.rolling(20).std(ddof=0)
    if kind not in ('shock_momentum','shock_reversal'):raise ValueError(kind)
    out={}
    for t in d.index[np.isfinite(z)&(z.abs()>=threshold)&(ret!=0)]:
        if t not in h.index:continue
        atr=float(h.loc[t,'atr']);price=float(d.loc[t,'close'])
        if not np.isfinite(atr) or atr<=0:continue
        side=int(np.sign(ret.loc[t]))*(1 if kind=='shock_momentum' else -1)
        out[int(t+1+r.MIN)]={'side':side,'reference':price,'atr':atr,'stop':price-side*2*atr,'target':None}
    return out

def run(data,kind,param,period,cost=4.5):
    return r.simulate(data,kind,param,*period,execution_bps=cost,schedules_override={s:make_signals(x,kind,param) for s,x in data.items()},max_hold_hours=23,include_trades=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    assert json.loads((a.output/'protocol.json').read_text())==json.loads(json.dumps(PLAN))
    data,manifest=r.load_data(a.cache,['2023-12']+[f'2024-{m:02}' for m in range(1,7)])
    (a.output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    result={'protocol':PLAN,'accepted':False,'development':[],'selection':[]}
    for phase in ('development','selection'):
        for kind,param in CANDIDATES:
            v=run(data,kind,param,PLAN[phase]);result[phase].append(v)
            print(phase,kind,param,v['trades'],round(v['return_pct'],4),flush=True)
            (a.output/'results.json').write_text(json.dumps(result,indent=2))
    eligible=[v for i,v in enumerate(result['selection']) if result['development'][i]['net_usdt']>0 and v['net_usdt']>0 and v['trades']>=30 and (v['profit_factor'] or 0)>=1.15 and v['max_drawdown_pct']<8]
    result['selection_qualified']=bool(eligible)
    if eligible:
        v=max(eligible,key=lambda v:v['profit_factor']);result['chosen']={'strategy':v['strategy'],'parameter':v['parameter']};result['status']='locked_pending_independent_test'
        (a.output/'selection_locked.json').write_text(json.dumps(result,indent=2))
    else:result['status']='rejected_at_selection_holdout_unopened'
    (a.output/'results.json').write_text(json.dumps(result,indent=2));print(result['status'],flush=True)
if __name__=='__main__':main()
