"""Generate audit charts from stored results and verified cached futures candles."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import research_futures as r
root=Path(__file__).parent/'round2';results=json.loads((root/'results.json').read_text())
plt.rcParams.update({'font.family':'DejaVu Sans','axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.18})
fig,axes=plt.subplots(3,1,figsize=(13,12),constrained_layout=True)
chosen=results['chosen'];fig.suptitle(f'AI Crypto · независимые проверки\n{chosen["strategy"]}, параметр {chosen["parameter"]} · допуск: {"пройден" if results["accepted"] else "не пройден"}',fontsize=16)
for ax,base,stress in zip(axes[:2],results['holdout'],results['stress']):
    for item,label,color in [(base,'Базовые расходы','#2563eb'),(stress,'Повышенные расходы','#dc2626')]:
        points=[{'ts':r.ms(item['period'][0]),'equity':10000}]+item['daily_equity']
        ax.plot(pd.to_datetime([p['ts'] for p in points],unit='ms',utc=True),[p['equity'] for p in points],label=label,color=color,lw=1.7)
    ax.axhline(10000,color='#64748b',lw=.8,ls='--');ax.set_ylabel('Учебный счёт, USDT');ax.legend(loc='best')
    ax.set_title(f'{base["period"][0]} — {base["period"][1]} · {base["trades"]} сделок · {base["return_pct"]:+.2f}% · просадка {base["max_drawdown_pct"]:.2f}%')
cache=Path('/tmp/ai_crypto_futures');minute=r.read_zip(r.archive(cache,'BTCUSDT','2026-10','klines'))
frame=r.aggregate(minute,240)
frame=frame.loc[frame.index<r.ms('2026-10-24')]
ax=axes[2];ax.plot(pd.to_datetime(frame.index,unit='ms',utc=True),frame.close,lw=1,color='#334155',label='BTCUSDT Futures · 4ч')
trades=[t for t in results['holdout'][1]['trade_records'] if t['symbol']=='BTCUSDT']
lookup=minute.set_index('open_time')
for side,color,marker,label in [(1,'#16a34a','^','Вход LONG'),(-1,'#dc2626','v','Вход SHORT')]:
    group=[t for t in trades if t['side']==side]
    if group:ax.scatter(pd.to_datetime([t['entry_ts'] for t in group],unit='ms',utc=True),[lookup.loc[t['entry_ts'],'open'] for t in group],color=color,marker=marker,s=50,label=label,zorder=3)
if trades:ax.scatter(pd.to_datetime([t['exit_ts'] for t in trades],unit='ms',utc=True),[lookup.loc[t['exit_ts']//r.MIN*r.MIN,'close'] for t in trades],color='#7c3aed',marker='x',s=35,label='Закрытие',zorder=3)
ax.set_title('Время входов и закрытий · отметки по свечам, не точные цены исполнения');ax.set_ylabel('Цена, USDT');ax.legend(loc='best')
fig.savefig(root/'charts.png',dpi=140)
