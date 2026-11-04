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

def price_chart(candles,candidate=None,synthetic=False):
    with _lock,plt.rc_context({"font.family":"DejaVu Sans","font.size":10}):
        d=aggregate(candles,5)
        if candidate:d=d[d.close_ts<=candidate["ts"]]
        d=d.tail(100).reset_index(drop=True)
        if d.empty:raise ValueError("Нет свечей для графика")
        fig,(ax,vol)=plt.subplots(2,1,figsize=(12,7),sharex=True,gridspec_kw={"height_ratios":[4,1]},facecolor="#101722")
        for axis in (ax,vol):
            axis.set_facecolor("#101722");axis.tick_params(colors="#aebbd0");axis.grid(alpha=.12)
            for spine in axis.spines.values():spine.set_color("#334155")
        for i,r in enumerate(d.itertuples()):
            color="#48d8ad" if r.close>=r.open else "#ff7b85"
            ax.vlines(i,r.low,r.high,color=color,linewidth=.8)
            ax.add_patch(Rectangle((i-.32,min(r.open,r.close)),.64,max(abs(r.close-r.open),r.close*.00001),facecolor=color))
            vol.bar(i,r.volume,color=color,width=.64)
        if candidate:
            for key,col,label in [("reference","#aebbd0","Signal reference"),("stop","#ff7b85","Stop"),("target","#48d8ad","Target")]:
                ax.axhline(candidate[key],color=col,linewidth=1,label=f"{label} {candidate[key]:.7g}")
            for key in ("support","resistance"):
                v=candidate["levels"].get(key)
                if v:ax.axhline(v,color="#a7a1ff",alpha=.65,ls="--",label=key.title())
            for label,v in candidate["levels"].get("fibonacci",{}).items():
                if label in ("38.2","50.0","61.8"):ax.axhline(v,color="#efc77c",ls=":",alpha=.7,label=f"Fib {label}%")
            title=f"{candidate['symbol']} · {'LONG' if candidate['side']==1 else 'SHORT'} · {candidate['setup']}"
            ax.legend(facecolor="#182332",labelcolor="#e4eafa",loc="upper left",fontsize=8)
        else:title="Market overview · 5m"
        ax.set_title(title+(" · SYNTHETIC DEMO" if synthetic else " · PAPER"),color="#e4eafa",loc="left",pad=15)
        ticks=np.linspace(0,len(d)-1,min(6,len(d))).astype(int)
        vol.set_xticks(ticks);vol.set_xticklabels([pd.to_datetime(d.iloc[i].ts,unit="ms",utc=True).strftime("%d %b %H:%M") for i in ticks])
        vol.set_xlabel("UTC",color="#aebbd0");ax.set_ylabel("USDT",color="#aebbd0")
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
