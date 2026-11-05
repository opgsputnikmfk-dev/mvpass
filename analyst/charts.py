from __future__ import annotations
from pathlib import Path
import io
import threading
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
from .indicators import aggregate

_lock=threading.Lock()

def price_chart(candles,candidate=None,synthetic=False,source="PAPER"):
    with _lock,plt.rc_context({"font.family":"DejaVu Sans","font.size":10}):
        d=aggregate(candles,5)
        if candidate and not candidate.get("live_position"):d=d[d.close_ts<=candidate["ts"]]
        if candidate and candidate.get("live_position"):
            start=candidate["entry_ts"]-30*5*60000
            d=d[d.close_ts>=start]
        d=d.tail(600 if candidate and candidate.get("live_position") else 100).reset_index(drop=True)
        if d.empty:raise ValueError("Нет свечей для графика")
        fig,(ax,vol)=plt.subplots(2,1,figsize=(12,7),sharex=True,gridspec_kw={"height_ratios":[4,1]},facecolor="#111315")
        for axis in (ax,vol):
            axis.set_facecolor("#111315");axis.tick_params(colors="#b2b8bb");axis.grid(alpha=.12)
            for spine in axis.spines.values():spine.set_color("#34383b")
        for i,r in enumerate(d.itertuples()):
            color="#8aefbb" if r.close>=r.open else "#ff7b85"
            ax.vlines(i,r.low,r.high,color=color,linewidth=.8)
            ax.add_patch(Rectangle((i-.32,min(r.open,r.close)),.64,max(abs(r.close-r.open),r.close*.00001),facecolor=color))
            vol.bar(i,r.volume,color=color,width=.64)
        if candidate:
            for key,col,label in [("reference","#b2b8bb","Entry / TVH"),("stop","#ff7b85","SL / Stop"),("target","#8aefbb","TP / Target")]:
                ax.axhline(candidate[key],color=col,linewidth=1,label=f"{label} {candidate[key]:.7g}")
            if candidate.get("live_position"):
                price=candidate.get("current_price")
                if price:ax.axhline(price,color="#80caff",ls="--",label=f"Current {price:.7g}")
                idx=np.searchsorted(d.close_ts.to_numpy(),candidate["entry_ts"])
                if idx<len(d):ax.axvline(idx,color="#b2b8bb",ls=":",label="Entry time")
            for key in ("support","resistance"):
                v=candidate["levels"].get(key)
                if v:ax.axhline(v,color="#c3c8cb",alpha=.65,ls="--",label=key.title())
            for label,v in candidate["levels"].get("fibonacci",{}).items():
                if label in ("38.2","50.0","61.8"):ax.axhline(v,color="#efc77c",ls=":",alpha=.7,label=f"Fib {label}%")
            title=f"{candidate['symbol']} · {'LONG' if candidate['side']==1 else 'SHORT'} · {candidate['setup']}"
            ax.legend(facecolor="#1c2022",labelcolor="#eff2f3",loc="upper left",fontsize=8)
        else:title="Market overview · 5m"
        ax.set_title(title+(" · SYNTHETIC DEMO" if synthetic else " · "+source),color="#eff2f3",loc="left",pad=15)
        ticks=np.linspace(0,len(d)-1,min(6,len(d))).astype(int)
        vol.set_xticks(ticks);vol.set_xticklabels([pd.to_datetime(d.iloc[i].ts,unit="ms",utc=True).strftime("%d %b %H:%M") for i in ticks])
        vol.set_xlabel("UTC",color="#b2b8bb");ax.set_ylabel("USDT",color="#b2b8bb")
        fig.tight_layout();out=io.BytesIO();fig.savefig(out,format="png",dpi=140,facecolor=fig.get_facecolor());plt.close(fig)
        return out.getvalue()

def equity_chart(accounts,path,synthetic=False):
    with _lock:
        fig,ax=plt.subplots(figsize=(12,6))
        # Keep chart readable; full comparison remains in CSV/JSON.
        for name,a in list(accounts.items())[:8]:
            if a.equity_curve:
                x,y=zip(*a.equity_curve);ax.plot(pd.to_datetime(x,unit="ms",utc=True),y,label=name,linewidth=1)
        ax.set_title("SYNTHETIC DEMO — NOT MARKET PERFORMANCE" if synthetic else "Paper portfolio equity — costs included")
        ax.set_ylabel("USDT");ax.grid(alpha=.2);ax.legend(fontsize=8);fig.autofmt_xdate();fig.tight_layout()
        fig.savefig(path,dpi=140);plt.close(fig)
