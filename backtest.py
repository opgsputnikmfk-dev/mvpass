"""Chronological replay using the SAME Runtime.monitor/scan_one and execution code.

Example (fixed symbols; never today's volume ranking):
python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --start 2026-10-24 --end 2026-10-31 --data-dir /tmp/replay --download --output validation.json

Public downloads only. No Telegram token is read and no orders/messages are sent.
The fixed historical spread is an assumption: historical bid/ask and futures basis
are unavailable in these candles. Funding and true execution remain unmodelled.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import pandas as pd
import bot


def date_ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()*1000)


def aggregate(minute, tf):
    dt=bot.TF_MS[tf]
    raw=minute.copy()
    raw['bucket']=(raw.ts//dt)*dt
    groups=raw.groupby('bucket',sort=True)
    data=groups.agg(open=('open','first'),high=('high','max'),low=('low','min'),
                    close=('close','last'),volume=('volume','sum'),n=('ts','count')).reset_index()
    data=data.loc[data.n == dt//bot.MINUTE].rename(columns={'bucket':'ts'}).drop(columns='n')
    data['close_ts']=data.ts+dt-1
    return data.reset_index(drop=True)


def download(symbol, start, end, root):
    client=bot.MarketClient()
    for tf, warmup in [('1m',350*bot.TF_MS['5m']),('15m',350*bot.TF_MS['15m']),('1h',350*bot.TF_MS['1h'])]:
        path=root/f'{symbol}_{tf}.csv'
        if path.exists():
            continue
        frame=client.klines_range(symbol,tf,start-warmup,end-1)
        bot.validate_bars(frame,bot.MINUTE if tf=='1m' else bot.TF_MS[tf])
        frame.to_csv(path,index=False)
    print('Downloaded',symbol,flush=True)


class HistoricalClient:
    def __init__(self, data, now, spread_bps):
        self.data,self.current,self.spread=data,now,spread_bps
        self.frames={}

    def now_ms(self):return self.current

    def history(self,symbol,tf,now,limit):
        raw=self.data[symbol][tf]
        idx=int(raw.close_ts.searchsorted(now,side='left'))
        result=raw.iloc[max(0,idx-limit):idx].copy().reset_index(drop=True)
        self.frames[symbol,tf]=result
        return result

    def klines_range(self,symbol,tf,start,end):
        raw=self.data[symbol][tf]
        a=int(raw.ts.searchsorted(start,side='left'))
        b=int(raw.close_ts.searchsorted(end,side='right'))
        return raw.iloc[a:b].copy().reset_index(drop=True)

    def funding(self,*args):return []
    def spread_bps(self,symbol):return self.spread


class NoTelegram:
    def __init__(self):self.messages=0
    def notify(self,message):self.messages+=1


def run_replay(data,start,end,cfg=None,spread_bps=5.):
    cfg=cfg or bot.Config()
    symbols=sorted(data)
    for symbol in symbols:
        for tf in ('1m',)+bot.STRATEGY_TFS:
            frame=data[symbol][tf]
            bot.validate_bars(frame,bot.MINUTE if tf=='1m' else bot.TF_MS[tf])
            if frame.empty or frame.iloc[-1].close_ts < end-1:
                raise ValueError(f'Incomplete replay data: {symbol} {tf}')
        minute=data[symbol]['1m']
        selected=minute.loc[(minute.ts>=start)&(minute.close_ts<end)]
        if len(selected)!=(end-start)//bot.MINUTE or int(selected.iloc[0].ts)!=start:
            raise ValueError('Missing execution minutes')
    store=bot.Store(':memory:',cfg)
    client=HistoricalClient(data,start,spread_bps)
    tg=NoTelegram();runtime=bot.Runtime(store,cfg,client,tg)
    days=[];daily_start=cfg.initial_equity
    try:
        for ts in range(start,end,bot.MINUTE):
            # Decision time is two seconds after a completed minute. The real
            # scheduler then chooses a future full minute, never this past open.
            now=ts+bot.MINUTE+2000
            client.current=now
            runtime.monitor(now)
            if (ts+bot.MINUTE)%bot.TF_MS['5m']==0:
                for symbol in symbols:
                    runtime.scan_one(symbol,'5m',now)
            if (ts+bot.MINUTE)%86400000==0:
                s=store.snapshot();equity=bot.account_equity(s)
                days.append({'date':datetime.fromtimestamp(ts/1000,timezone.utc).date().isoformat(),
                             'equity':round(equity,4),'net_change':round(equity-daily_start,4),
                             'closed_count':s['closed_count']})
                daily_start=equity
                print('Replay day',days[-1],flush=True)
        state=store.snapshot();trades=store.trades()
        summary=bot.summarize_trades(trades)
        summary.update(strategy=cfg.strategy_id,version=bot.BOT_VERSION,
                       start=datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),
                       end_exclusive=datetime.fromtimestamp(end/1000,timezone.utc).isoformat(),symbols=symbols,
                       candles_evaluated=state['evaluated'].get('5m',0),signals=state['scheduled'].get('5m',0),
                       open_at_end=len(state['positions']),pending_at_end=len(state['pending']),
                       final_equity=bot.account_equity(state),max_observed_drawdown=state['max_drawdown'],
                       rejection_counts=state['rejects'].get('5m',{}),daily=days,
                       assumptions={'fixed_spread_bps':spread_bps,'fee_bps':cfg.fee_bps,'slippage_bps':cfg.slippage_bps,
                                    'funding':'not included','prices':'Binance Spot; not futures',
                                    'selection':'fixed symbols chosen before this replay; not current volume ranking',
                                    'intrabar':'1m OHLC; stop first if ambiguous; adverse gap fills',
                                    'timing':'decision 2s after bar close; scheduled next full minute',
                                    'open_positions':'marked to adverse exit price, not forced closed'},
                       warning='A short retrospective check, not validated profitability or a walk-forward study.')
        return summary
    finally:
        store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols',nargs='+',required=True)
    parser.add_argument('--start',required=True);parser.add_argument('--end',required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--download',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('validation.json'))
    parser.add_argument('--fee-bps',type=float,default=bot.Config().fee_bps)
    args=parser.parse_args()
    start,end=date_ms(args.start),date_ms(args.end)
    if not start<end or end>int(time.time()*1000):raise SystemExit('Use a completed historical period')
    args.data_dir.mkdir(parents=True,exist_ok=True)
    if args.download:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda symbol:download(symbol,start,end,args.data_dir),args.symbols))
    data={}
    for symbol in args.symbols:
        data[symbol]={tf:pd.read_csv(args.data_dir/f'{symbol}_{tf}.csv') for tf in ('1m','15m','1h')}
        data[symbol]['5m']=aggregate(data[symbol]['1m'],'5m')
    result=run_replay(data,start,end,bot.Config(fee_bps=args.fee_bps))
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('daily','assumptions','rejection_counts')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
