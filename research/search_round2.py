"""Second frozen search: slower trends with next-bar trailing stops.
Published trend research motivates a hypothesis, not a claim it transfers to crypto.
"""
import json,argparse
from pathlib import Path
import numpy as np
import pandas as pd
import research_futures as r

CANDIDATES=[('channel_4h',20),('channel_4h',55),('ema_4h',12),('ema_4h',24),('compression_1h',20),('compression_1h',40)]
MONTHS=['2024-12']+[f'2025-{m:02}' for m in range(1,13)]+[f'2026-{m:02}' for m in range(1,11)]
PLAN={'round':2,'candidates':CANDIDATES,'development':['2025-01-01','2025-07-01'],'selection':['2025-07-01','2025-10-01'],'unseen_periods':[['2025-10-01','2025-12-01'],['2026-10-01','2026-10-24']],
 'already_inspected_not_holdout':'2026-01 through 2026-09; 2025-12 was already used as indicator warmup; 2026-10-24 through 2026-10-30 inspected in v3 baseline',
 'risk_costs':r.PLAN['risk'],'costs':r.PLAN['costs'],'max_hold_hours':168,'trailing_atr_multiple':3,
 'selection_rule':'Highest PF among selection candidates with >=20 trades and net>0; if none select highest net for diagnosis only. Lock before holdout. No final-period retuning.',
 'admission':'Selection positive; both unseen periods positive net and PF>=1.15; combined unseen >=50 trades; DD<8% each; stress positive in both; selection neighbor parameter +/-10% both positive.',
 'sources':['https://www.aqr.com/insights/research/journal-article/time-series-momentum','https://www.aqr.com/insights/research/journal-article/which-trend-is-your-friend'],
 'caveats':['Prior paper covers other assets and longer horizons; these are new unproven crypto implementations.','Repeated searching increases overfitting risk; keep all rejected results.','New 2026-10 window is short; candidate still needs prospective observation.']}

def make_signals(base,kind,param):
    hours=1 if kind=='compression_1h' else 4
    raw=base[['open','high','low','close','volume']].copy()
    raw.index.name='close_ts'
    raw['bucket']=(raw.index//(hours*60*r.MIN))*(hours*60*r.MIN)
    f=raw.groupby('bucket').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),n=('open','size'))
    f=f[f.n==hours*4].drop(columns='n');f.index=f.index+hours*60*r.MIN-1
    f=r.features(f);sign=pd.Series(0,index=f.index)
    if kind=='channel_4h':
        n=int(param);sign[f.close>f.high.shift().rolling(n).max()]=1;sign[f.close<f.low.shift().rolling(n).min()]=-1
    elif kind=='ema_4h':
        fast=f.close.ewm(span=int(param),adjust=False).mean();slow=f.close.ewm(span=int(param)*4,adjust=False).mean()
        sign[(fast>slow)&(fast.shift()<=slow.shift())]=1;sign[(fast<slow)&(fast.shift()>=slow.shift())]=-1
    elif kind=='compression_1h':
        n=int(param);width=f.high.shift().rolling(n).max()-f.low.shift().rolling(n).min()
        contraction=width<width.shift().rolling(120).median()
        sign[contraction&(f.close>f.high.shift().rolling(n).max())&(f.close>f.ema200)]=1
        sign[contraction&(f.close<f.low.shift().rolling(n).min())&(f.close<f.ema200)]=-1
    else:raise ValueError(kind)
    out={}
    for t in f.index[sign!=0]:
        row=f.loc[t];side=int(sign.loc[t]);atr=float(row.atr)
        if not np.isfinite(atr) or atr<=0:continue
        out[int(t+1+r.MIN)]={'side':side,'reference':float(row.close),'atr':atr,'stop':float(row.close-side*3*atr),'target':None}
    return out,{int(t):float(v) for t,v in f.atr.items() if np.isfinite(v)}

def run(data,kind,param,period,cost=4.5,records=False):
    schedules={}
    for s,x in data.items():
        schedules[s],x['trailing']=make_signals(x['features'],kind,param)
    return r.simulate(data,kind,param,*period,execution_bps=cost,schedules_override=schedules,max_hold_hours=168,trailing_multiple=3,include_trades=records)

def main():
    p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True);(a.output/'protocol.json').write_text(json.dumps(PLAN,indent=2))
    data,manifest=r.load_data(a.cache,MONTHS,end_exclusive='2026-10-24');(a.output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    results={'protocol':PLAN}
    for phase in ('development','selection'):
        results[phase]=[]
        for kind,param in CANDIDATES:
            result=run(data,kind,param,PLAN[phase]);result.pop('daily_equity',None);results[phase].append(result)
            print(phase,kind,param,result['trades'],round(result['return_pct'],3),flush=True)
    qualified=[v for v in results['selection'] if v['trades']>=20 and v['net_usdt']>0]
    winner=max(qualified,key=lambda v:v['profit_factor'] or 1e9) if qualified else max(results['selection'],key=lambda v:v['net_usdt'])
    kind,param=winner['strategy'],winner['parameter'];results['chosen']={'strategy':kind,'parameter':param,'selection_qualified':bool(qualified)}
    results['sensitivity']=[run(data,kind,max(2,round(param*factor)),PLAN['selection']) for factor in (.9,1.1)]
    for check in results['sensitivity']:check.pop('daily_equity',None)
    (a.output/'selection_locked.json').write_text(json.dumps(results,indent=2))
    results['holdout']=[];results['stress']=[]
    for period in PLAN['unseen_periods']:
        for phase,cost in [('holdout',4.5),('stress',9.)]:
            result=run(data,kind,param,period,cost,records=True);results[phase].append(result)
            print(phase,period,result['trades'],round(result['return_pct'],3),flush=True)
    checks=[bool(qualified),sum(v['trades'] for v in results['holdout'])>=50,all(v['net_usdt']>0 for v in results['sensitivity'])]
    checks += [v['net_usdt']>0 and (v['profit_factor'] or 1e9)>=1.15 and v['max_drawdown_pct']<8 for v in results['holdout']]
    checks += [v['net_usdt']>0 for v in results['stress']]
    results['accepted']=all(checks);(a.output/'results.json').write_text(json.dumps(results,indent=2))
    print('ACCEPTED',results['accepted'],flush=True)
if __name__=='__main__':main()
