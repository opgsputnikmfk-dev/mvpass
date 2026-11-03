"""Frozen funding-pressure hypotheses; not a replication of BIS cash-and-carry."""
import argparse,json
from pathlib import Path
import numpy as np
import pandas as pd
import research_futures as r
CANDIDATES=[('funding_fade',1.),('funding_fade',3.),('funding_fade_confirmed',1.),('funding_fade_confirmed',3.)]
PLAN={'round':3,'registered':'2026-11-03','candidates':CANDIDATES,'development':['2024-01-01','2024-04-01'],'selection':['2024-04-01','2024-07-01'],'holdout':[['2024-07-01','2024-10-01'],['2024-10-01','2024-12-01']], 'warmup':'2023-12', 'source':'https://www.bis.org/publ/work1087.pdf','hypothesis':'Published carry/positioning mechanism motivates an original directional perpetual-funding fade, NOT cash-and-carry replication. Oppose completed funding rate of >=1 or >=3 bps per 8h; confirmed version requires preceding 24h return in proposed direction. Rate normalized using elapsed interval; first event skipped. Signal waits 15 min after settlement, uses last closed hourly candle, entry one further minute later. Stop 2 hourly ATR; no target, 24h timeout; one signal per funding event.', 'selection_rule':'Require positive development AND selection, >=30 selection trades, selection PF>=1.15 and DD<8%; highest selection PF wins (listed order breaks ties). If none, reject all WITHOUT opening holdout.', 'admission':'Locked candidate: both holdouts positive PF>=1.15 DD<8%, combined >=100 trades; both holdouts positive at 9bps adverse execution per side and for each threshold +/-10%; no retuning. Paper observation still required.', 'costs':r.PLAN['costs'],'risk':r.PLAN['risk'],'independence':'2024 Jan-Nov absent from prior recorded experiments; warmup 2024 December from round2 excluded. Conditional on complete repository record; paper development may overlap historical holdout, so prospective verification remains necessary.', 'limitations':['Funding is not futures basis; directional adaptation may have no edge.','Survivor universe BTC ETH SOL; no full exchange universe.','Fixed spread/slippage proxy, no historical bid/ask.','Minute OHLC stop-first, funding mark-minute-open approximation.','Repeated family searches; no claim of statistical significance from positive PnL alone.']}
def make_signals(x,kind,threshold):
    f=x['features'][['open','high','low','close','volume']].copy()
    f['bucket']=f.index//(60*r.MIN)*(60*r.MIN)
    h=f.groupby('bucket').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),n=('open','size'))
    h=h[h.n==4].drop(columns='n');h.index+=60*r.MIN-1;h=r.features(h)
    h['ret24']=h.close/h.close.shift(24)-1
    events=sorted(e for events in x['funding'].values() for e in events);out={}
    for prev,ev in zip(events,events[1:]):
        t,rate,_=ev;hours=(t-prev[0])/3600000
        if hours<=0:raise ValueError('Nonpositive funding interval')
        rate8=rate*8/hours
        if abs(rate8)<threshold/10000:continue
        side=-1 if rate8>0 else 1
        decision=t+15*r.MIN
        j=h.index.searchsorted(decision,side='right')-1
        if j<0:continue
        row=h.iloc[j]
        if not np.isfinite(row.atr) or not np.isfinite(row.ret24) or row.atr<=0:continue
        if kind=='funding_fade_confirmed' and side*row.ret24<=0:continue
        if kind not in ('funding_fade','funding_fade_confirmed'):raise ValueError(kind)
        out[decision+r.MIN]={'side':side,'reference':float(row.close),'atr':float(row.atr),'stop':float(row.close-side*2*row.atr),'target':None}
    return out

def run(data,kind,param,period,cost=4.5):
    schedules={s:make_signals(x,kind,param) for s,x in data.items()}
    return r.simulate(data,kind,param,*period,execution_bps=cost,schedules_override=schedules,max_hold_hours=24,include_trades=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    # The externally committed protocol must match before any market data are loaded.
    assert json.loads((a.output/'protocol.json').read_text())==json.loads(json.dumps(PLAN))
    months=['2023-12']+[f'2024-{m:02}' for m in range(1,7)]
    data,manifest=r.load_data(a.cache,months)
    result={'protocol':PLAN,'accepted':False,'development':[],'selection':[]}
    (a.output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    for phase in ('development','selection'):
        for kind,param in CANDIDATES:
            v=run(data,kind,param,PLAN[phase]);result[phase].append(v)
            print(phase,kind,param,v['trades'],round(v['return_pct'],4),flush=True)
            (a.output/'results.json').write_text(json.dumps(result,indent=2))
    eligible=[v for i,v in enumerate(result['selection']) if result['development'][i]['net_usdt']>0 and v['net_usdt']>0 and v['trades']>=30 and (v['profit_factor'] or 0)>=1.15 and v['max_drawdown_pct']<8]
    result['selection_qualified']=bool(eligible)
    if not eligible:
        result['status']='rejected_at_selection_holdout_unopened'
        (a.output/'results.json').write_text(json.dumps(result,indent=2));print(result['status'],flush=True);return
    chosen=max(eligible,key=lambda v:v['profit_factor']);kind,param=chosen['strategy'],chosen['parameter']
    result['chosen']={'strategy':kind,'parameter':param};result['status']='locked_pending_independent_test'
    (a.output/'selection_locked.json').write_text(json.dumps(result,indent=2));(a.output/'results.json').write_text(json.dumps(result,indent=2))
    # Deliberate separate invocation/review before independent data access.
    print(result['status'],flush=True)
if __name__=='__main__':main()
