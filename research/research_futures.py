"""Frozen futures research protocol. Public historical data only; no order APIs.
Run: python research_futures.py --cache /tmp/ai_crypto_futures --output results.json
All timestamps UTC. Dependencies: numpy, pandas (same as bot).
"""
from pathlib import Path
import concurrent.futures as cf
import hashlib, io, json, math, time, urllib.request, zipfile, argparse
import numpy as np
import pandas as pd

MIN=60000
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
MONTHS=['2025-12']+[f'2026-{m:02}' for m in range(1,10)]
VARIANTS=[('pullback',1.5),('pullback',2.),('breakout',20),('breakout',40),('reversion',2.),('reversion',2.5)]
PERIODS={'development':('2026-01-01','2026-05-01'), 'selection':('2026-05-01','2026-07-01'), 'holdout':('2026-07-01','2026-10-01')}
PLAN={'symbols':SYMBOLS,'months':MONTHS,'variants':VARIANTS,'periods':PERIODS,
 'selection_rule':'Among validation variants with >=30 trades and positive net choose highest profit factor; if none, choose highest net only as diagnostic, without qualification.',
 'acceptance':{'validation_positive':True,'holdout_trades_min':50,'holdout_profit_factor_min':1.15,'holdout_positive_months_min':2,'holdout_max_drawdown':.08,'double_execution_cost_stress_positive':True},
 'costs':{'fee_per_side_bps':5.5,'adverse_execution_per_side_bps':4.5,'stress_execution_bps':9.},
 'risk':{'initial_equity':10000,'risk_per_trade':.0025,'total_risk':.0075,'same_direction_risk':.0035,'max_positions':3,'max_same_direction':2,'position_notional_fraction':.25,'gross_notional_fraction':1.,'daily_loss_stop':.02,'drawdown_stop':.08},
 'execution':'Decision on completed 15m bar, entry one minute after bar boundary. Minute OHLC, stop-first on ambiguous bars, adverse gaps. No partial take profits. 12h time exit. 30m cooldown. No leverage.',
 'funding':'Actual published rate; event uses mark-price minute open as proxy for exact settlement mark. Charged before intraminute exits; timestamp ordering within a minute is approximate.',
 'limitations':['Three established survivor assets; not the full exchange universe.','Historical bid/ask and exact fills unavailable; fixed spread/slippage scenario.','No liquidation simulation because gross notional capped at equity.','One untouched chronological holdout; not a full repeated walk-forward study.','No parameter tuning after holdout inspection.']}

def ms(date):return int(pd.Timestamp(date,tz='UTC').timestamp()*1000)
def request(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(url,timeout=25) as r:return r.read()
        except Exception:
            if i==2:raise
            time.sleep(i+1)

def archive(cache,symbol,month,kind):
    stem=f'{symbol}-fundingRate-{month}' if kind=='fundingRate' else f'{symbol}-1m-{month}'
    suffix=f'{kind}/{symbol}/'+('' if kind=='fundingRate' else '1m/')+stem+'.zip'
    url='https://data.binance.vision/data/futures/um/'+('daily' if len(month)==10 else 'monthly')+'/'+suffix
    path=cache/kind/(stem+'.zip');path.parent.mkdir(parents=True,exist_ok=True)
    checksum=path.with_suffix('.sha256')
    if not checksum.exists():checksum.write_bytes(request(url+'.CHECKSUM'))
    expected=checksum.read_text().split()[0]
    if not path.exists():
        content=request(url)
        if hashlib.sha256(content).hexdigest()!=expected:raise ValueError('Download checksum mismatch')
        path.write_bytes(content)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('Cached checksum mismatch')
    return path

def read_zip(path):
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        if len(names)!=1:raise ValueError('Unexpected archive')
        with z.open(names[0]) as f:return pd.read_csv(f)

def validate_minutes(d):
    fields=['open','high','low','close','volume']
    if not np.isfinite(d[fields]).all().all() or (d[fields[:4]]<=0).any().any():raise ValueError('Invalid prices')
    if (d.high<d[['open','close','low']].max(axis=1)).any() or (d.low>d[['open','close','high']].min(axis=1)).any():raise ValueError('Invalid OHLC')
    if not (d.open_time.diff().dropna()==MIN).all() or not (d.close_time==d.open_time+MIN-1).all():raise ValueError('Missing/duplicate/misaligned minute')

def aggregate(d,minutes):
    x=d.copy();x['bucket']=x.open_time//(minutes*MIN)*(minutes*MIN)
    r=x.groupby('bucket').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),n=('open_time','size'))
    r=r[r.n==minutes].drop(columns='n');r.index=r.index+minutes*MIN-1
    return r

def features(d):
    r=d.copy();c=r.close
    for span in (20,50,200):r[f'ema{span}']=c.ewm(span=span,adjust=False).mean()
    tr=pd.concat([r.high-r.low,(r.high-c.shift()).abs(),(r.low-c.shift()).abs()],axis=1).max(axis=1)
    r['atr']=tr.ewm(alpha=1/14,adjust=False).mean()
    r['mean']=c.rolling(20).mean();r['std']=c.rolling(20).std(ddof=0)
    r['vol_ratio']=r.volume/r.volume.shift().rolling(20).mean()
    r['slope']=(r.ema50-r.ema50.shift(3))/r.atr
    return r

def load_data(cache, months=None, end_exclusive=None):
    months = MONTHS if months is None else months
    jobs=[(s,m,k) for s in SYMBOLS for m in months for k in ('klines','markPriceKlines','fundingRate')]
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(archive,cache,*job) for job in jobs]
        for i,f in enumerate(cf.as_completed(futures),1):
            f.result()
            if i%15==0:print('Downloaded/verified',i,'/',len(jobs),flush=True)
    data={};manifest=[]
    for symbol in SYMBOLS:
        parts={}
        for kind in ('klines','markPriceKlines','fundingRate'):
            paths=[archive(cache,symbol,m,kind) for m in months]
            parts[kind]=pd.concat([read_zip(p) for p in paths],ignore_index=True)
            manifest += [{'file':str(p.relative_to(cache)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
        d=parts['klines'];mark=parts['markPriceKlines'];fund=parts['fundingRate']
        period_start=ms(months[0]+'-01')
        period_end=int((pd.Timestamp(months[-1]+'-01',tz='UTC')+pd.offsets.MonthBegin(1)).timestamp()*1000)
        if end_exclusive is not None:
            period_end=min(period_end,ms(end_exclusive))
            d=d.loc[d.close_time<period_end].reset_index(drop=True)
            mark=mark.loc[mark.close_time<period_end].reset_index(drop=True)
            fund=fund.loc[fund.calc_time<period_end].reset_index(drop=True)
        missing_trade=pd.Index(np.arange(period_start,period_end,MIN)).difference(d.open_time)
        if len(missing_trade):
            days=sorted({pd.Timestamp(t,unit='ms',tz='UTC').strftime('%Y-%m-%d') for t in missing_trade})
            repairs=[]
            for date in days:
                path=archive(cache,symbol,date,'klines');daily=read_zip(path)
                repairs.append(daily.loc[daily.open_time.isin(missing_trade)])
                manifest.append({'file':str(path.relative_to(cache)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'purpose':'fill missing monthly trade candles'})
            d=pd.concat([d]+repairs,ignore_index=True).sort_values('open_time').reset_index(drop=True)
            print('Repaired authoritative daily trading data',symbol,days,flush=True)
        validate_minutes(d)
        missing=set(d.open_time)-set(mark.open_time)
        if missing:
            days=sorted({pd.Timestamp(t,unit='ms',tz='UTC').strftime('%Y-%m-%d') for t in missing})
            repaired=[]
            for date in days:
                path=archive(cache,symbol,date,'markPriceKlines')
                daily=read_zip(path)
                repaired.append(daily.loc[daily.open_time.isin(missing)])
                manifest.append({'file':str(path.relative_to(cache)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'purpose':'fill missing monthly mark-price minutes'})
            mark=pd.concat([mark]+repaired,ignore_index=True).sort_values('open_time').reset_index(drop=True)
            print('Repaired authoritative daily mark data',symbol,days,flush=True)
        validate_minutes(mark)
        if int(d.open_time.iloc[0])!=period_start or int(d.close_time.iloc[-1])+1!=period_end:raise ValueError('Incomplete period')
        if not np.array_equal(d.open_time,mark.open_time):raise ValueError('Mark price dates mismatch')
        if not fund.calc_time.is_monotonic_increasing or fund.calc_time.duplicated().any():raise ValueError('Invalid funding chronology')
        if not np.isfinite(fund.last_funding_rate).all():raise ValueError('Invalid funding rate')
        gaps=fund.calc_time.diff().iloc[1:].to_numpy()/3600000
        if np.any(np.abs(gaps-fund.funding_interval_hours.iloc[1:].to_numpy())>.01):raise ValueError('Missing funding events')
        if fund.calc_time.iloc[0]-d.open_time.iloc[0]>9*3600000 or d.close_time.iloc[-1]-fund.calc_time.iloc[-1]>9*3600000:raise ValueError('Incomplete funding endpoints')
        minute=d[['open_time','open','high','low','close']].to_numpy()
        funding={}
        for row in fund.itertuples():
            idx=int((row.calc_time-int(d.open_time.iloc[0]))//MIN)
            funding.setdefault(idx,[]).append((int(row.calc_time),float(row.last_funding_rate),float(mark.open.iloc[idx])))
        f15=features(aggregate(d,15));hour=features(aggregate(d,60))
        context=hour[['ema50','ema200','slope','close','atr']].reindex(f15.index,method='ffill').add_prefix('h_')
        data[symbol]={'minute':minute,'funding':funding,'features':f15.join(context)}
        print('Validated',symbol,len(d),'minutes',len(fund),'funding events',flush=True)
    return data,manifest

def signals(f,kind,param):
    sign=pd.Series(0,index=f.index)
    trend_up=(f.h_close>f.h_ema200)&(f.h_ema50>f.h_ema200)&(f.h_slope>.05)
    trend_down=(f.h_close<f.h_ema200)&(f.h_ema50<f.h_ema200)&(f.h_slope<-.05)
    if kind=='pullback':
        up=trend_up&(f.low.shift()<=f.ema20.shift())&(f.close>f.high.shift())&(f.close>f.ema20)
        down=trend_down&(f.high.shift()>=f.ema20.shift())&(f.close<f.low.shift())&(f.close<f.ema20)
        sign[up]=1;sign[down]=-1
        stop=np.where(sign==1,f.low.rolling(4).min()-.15*f.atr,f.high.rolling(4).max()+.15*f.atr)
        target=f.close+sign*float(param)*abs(f.close-stop)
    elif kind=='breakout':
        n=int(param)
        sign[trend_up&(f.close>f.high.shift().rolling(n).max())&(f.vol_ratio>1.2)]=1
        sign[trend_down&(f.close<f.low.shift().rolling(n).min())&(f.vol_ratio>1.2)]=-1
        stop=f.close-sign*1.5*f.atr;target=f.close+sign*3*f.atr
    elif kind=='reversion':
        quiet=f.h_slope.abs()<.5
        lower=f['mean']-param*f['std'];upper=f['mean']+param*f['std']
        sign[quiet&(f.close.shift()<lower.shift())&(f.close>lower)]=1
        sign[quiet&(f.close.shift()>upper.shift())&(f.close<upper)]=-1
        stop=f.close-sign*1.5*f.atr;target=f['mean']
    else:raise ValueError('Unknown strategy')
    result=pd.DataFrame({'side':sign,'reference':f.close,'atr':f.atr,'stop':stop,'target':target})
    result=result[(sign!=0)&np.isfinite(result).all(axis=1)]
    # Knowledge time = candle close. Execute one full minute after its boundary.
    return {int(t+1+MIN):row.to_dict() for t,row in result.iterrows()}

def simulate(data,kind,param,start,end,execution_bps=4.5,schedules_override=None,max_hold_hours=12,trailing_multiple=None,include_trades=False):
    a,b=ms(start),ms(end);slip=execution_bps/10000;fee=.00055
    first=int(next(iter(data.values()))['minute'][0,0]);lo=(a-first)//MIN;hi=(b-first)//MIN
    schedules=schedules_override if schedules_override is not None else {s:signals(x['features'],kind,param) for s,x in data.items()}
    curve=[]
    balance=10000.;peak=balance;dd=0.;positions={};cooldowns={};trades=[];monthly={};day=None;day_start=balance;paused=False;dd_paused=False
    def equity():
        return balance+sum(p['qty']*(p['side']*(p['mark']*(1-p['side']*slip)-p['entry'])-fee*p['mark']*(1-p['side']*slip)) for p in positions.values())
    def close(symbol,raw,ts,reason):
        nonlocal balance
        p=positions.pop(symbol);exit_price=raw*(1-p['side']*slip)
        gross=p['qty']*p['side']*(exit_price-p['entry']);exit_fee=fee*p['qty']*exit_price
        balance+=gross-exit_fee
        net=gross-exit_fee-p['entry_fee']-p['funding']
        trades.append({'symbol':symbol,'side':p['side'],'entry_ts':p['ts'],'exit_ts':ts,'net':net,'risk':p['risk'],'r':net/p['risk'],'reason':reason,'funding':p['funding']})
        cooldowns[symbol]=ts+30*MIN
    for i in range(int(lo),int(hi)):
        ts=first+i*MIN
        if ts//86400000!=day:day=ts//86400000;day_start=equity();paused=False
        # Entries use only preceding closes/equity. Simultaneous entries use fixed symbol order.
        for symbol,x in data.items():
            sig=schedules[symbol].get(ts)
            if not sig or symbol in positions or paused or dd_paused or cooldowns.get(symbol,0)>ts:continue
            side=int(sig['side']);o=x['minute'][i,1]
            if len(positions)>=3 or sum(p['side']==side for p in positions.values())>=2:continue
            if abs(o-sig['reference'])>.5*sig['atr']:continue
            entry=o*(1+side*slip);stop=sig['stop'];target=sig['target'];distance=side*(entry-stop)
            if distance<=0 or (target is not None and side*(target-entry)<=0):continue
            risk_unit=side*(entry-stop*(1-side*slip))+fee*(entry+stop*(1-side*slip))
            net_reward=side*(target*(1-side*slip)-entry)-fee*(entry+target*(1-side*slip)) if target is not None else float('inf')
            if net_reward/risk_unit<.75:continue
            eq=equity();risk=min(eq*.0025,eq*.0075-sum(p['risk'] for p in positions.values()),eq*.0035-sum(p['risk'] for p in positions.values() if p['side']==side))
            notional=min(eq*.25,eq-sum(p['qty']*p['mark'] for p in positions.values()))
            if risk<=0 or notional<=0:continue
            qty=min(risk/risk_unit,notional/entry)
            if qty*entry<10:continue
            charge=qty*entry*fee;balance-=charge
            positions[symbol]={'entry':entry,'stop':stop,'target':target,'qty':qty,'side':side,'ts':ts,'mark':o,'risk':qty*risk_unit,'entry_fee':charge,'funding':0.}
        for symbol in list(positions):
            p=positions[symbol];row=data[symbol]['minute'][i];_,o,h,l,c=row;side=p['side']
            for ft,rate,mark in data[symbol]['funding'].get(i,[]):
                if ft>p['ts']:
                    charge=side*p['qty']*mark*rate;balance-=charge;p['funding']+=charge
            stop_hit=l<=p['stop'] if side==1 else h>=p['stop']
            target_hit=False if p['target'] is None else (h>=p['target'] if side==1 else l<=p['target'])
            if stop_hit:close(symbol,min(o,p['stop']) if side==1 else max(o,p['stop']),ts+MIN-1,'STOP_BOTH' if target_hit else 'STOP')
            elif target_hit:close(symbol,p['target'],ts+MIN-1,'TARGET')
            elif ts+MIN-p['ts']>=max_hold_hours*60*MIN or i==hi-1:close(symbol,c,ts+MIN-1,'TIME' if i!=hi-1 else 'PERIOD_END')
            else:p['mark']=c
            # Trail only after this CLOSED bar has been processed; next minute activation.
            trail=data[symbol].get('trailing',{}).get(ts+MIN-1)
            if symbol in positions and trailing_multiple is not None and trail is not None:
                new_stop=c-side*trailing_multiple*trail
                p['stop']=max(p['stop'],new_stop) if side==1 else min(p['stop'],new_stop)
        eq=equity();peak=max(peak,eq);dd=max(dd,1-eq/peak)
        if eq<day_start*.98:paused=True
        if eq<peak*.92:dd_paused=True
        if (ts+MIN)%86400000==0:
            monthly[pd.Timestamp(ts,unit='ms',tz='UTC').strftime('%Y-%m')]=eq
            curve.append({'ts':ts+MIN,'equity':eq})
    gains=sum(max(t['net'],0) for t in trades);losses=-sum(min(t['net'],0) for t in trades)
    monthly_net={};prev=10000.
    for m,e in monthly.items():monthly_net[m]=e-prev;prev=e
    # This invariant catches double-charged fees/funding and ledger drift.
    if not math.isclose(balance-10000,sum(t['net'] for t in trades),abs_tol=1e-6):raise AssertionError('Ledger mismatch')
    return {'daily_equity':curve,**({'trade_records':trades} if include_trades else {}),'strategy':kind,'parameter':param,'period':[start,end],'trades':len(trades),'net_usdt':balance-10000,'return_pct':(balance/10000-1)*100,'win_rate_pct':100*sum(t['net']>0 for t in trades)/len(trades) if trades else 0,'profit_factor':gains/losses if losses else None,'max_drawdown_pct':dd*100,'monthly_net':monthly_net,'funding_net_paid':sum(t['funding'] for t in trades),'by_symbol':{s:sum(t['net'] for t in trades if t['symbol']==s) for s in data},'execution_bps':execution_bps,'drawdown_halt':dd_paused}

def main():
    p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.with_name('protocol.json').write_text(json.dumps(PLAN,indent=2)+'\n')
    data,manifest=load_data(args.cache)
    args.output.with_name('data_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    results={'protocol':PLAN,'development':[],'selection':[]}
    for name in ('development','selection'):
        for kind,param in VARIANTS:
            r=simulate(data,kind,param,*PERIODS[name]);results[name].append(r);print(name,json.dumps(r),flush=True)
    eligible=[r for r in results['selection'] if r['trades']>=30 and r['net_usdt']>0]
    selected=max(eligible,key=lambda r:r['profit_factor'] if r['profit_factor'] is not None else float('inf')) if eligible else max(results['selection'],key=lambda r:r['net_usdt'])
    results['selected_before_holdout']={'strategy':selected['strategy'],'parameter':selected['parameter'],'validation_qualified':bool(eligible)}
    results['selection_parameter_sensitivity']=[]
    for factor in (.9,1.1):
        parameter=selected['parameter']*factor
        if selected['strategy']=='breakout':parameter=max(2,round(parameter))
        check=simulate(data,selected['strategy'],parameter,*PERIODS['selection'])
        results['selection_parameter_sensitivity'].append(check)
    args.output.with_name('selection_locked.json').write_text(json.dumps(results,indent=2)+'\n')
    for name,cost in [('holdout',4.5),('holdout_stress',9.)]:
        r=simulate(data,selected['strategy'],selected['parameter'],*PERIODS['holdout'],execution_bps=cost);results[name]=r;print(name,json.dumps(r),flush=True)
    test=results['holdout'];pf=test['profit_factor']
    results['accepted']=bool(eligible and test['trades']>=50 and (pf is None or pf>=1.15) and test['net_usdt']>0 and test['max_drawdown_pct']<8 and sum(x>0 for x in test['monthly_net'].values())>=2 and results['holdout_stress']['net_usdt']>0)
    args.output.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print('ACCEPTED',results['accepted'],flush=True)
if __name__=='__main__':main()
