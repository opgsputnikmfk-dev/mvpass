"""Causal indicators. Pivots become visible only after `right` bars close."""
from __future__ import annotations
import numpy as np
import pandas as pd

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

def clean_candles(df, continuous=True):
    if not set(COLUMNS).issubset(df.columns):
        raise ValueError("CSV: ts,open,high,low,close,volume required; ts in UTC milliseconds")
    d = df[COLUMNS].copy().apply(pd.to_numeric, errors="raise").sort_values("ts").reset_index(drop=True)
    if d.empty or not np.isfinite(d.to_numpy()).all():
        raise ValueError("Пустые / нечисловые свечи")
    if (d.ts % 60000 != 0).any() or d.ts.duplicated().any():
        raise ValueError("Повторные свечи или неверная минутная сетка")
    if continuous and not (d.ts.diff().dropna() == 60000).all():
        raise ValueError("Пропуск минутных свечей; расчёт остановлен")
    if (d[["open", "high", "low", "close"]] <= 0).any().any() or (d.volume < 0).any():
        raise ValueError("Неверные цены / объёмы")
    if (d.high < d[["open", "close", "low"]].max(axis=1)).any() or (d.low > d[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("Неверные OHLC")
    d.ts = d.ts.astype("int64")
    return d

def aggregate(df, minutes):
    """Only complete UTC-aligned groups survive, with end-exclusive close_ts."""
    n = minutes * 60000
    g = df.assign(bucket=(df.ts // n) * n).groupby("bucket", sort=True)
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"), count=("ts", "size"))
    out = out[out["count"] == minutes].drop(columns="count").reset_index().rename(columns={"bucket": "ts"})
    out["close_ts"] = out.ts + n
    return out

def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()

def rma(s, n=14):
    # Explicit Wilder seed (arithmetic mean), then recursive smoothing.
    a = s.to_numpy(dtype=float); out = np.full(len(a), np.nan)
    valid = np.flatnonzero(np.isfinite(a))
    if len(valid) >= n:
        start = int(valid[0]); end = start+n-1
        if end < len(a) and np.isfinite(a[start:end+1]).all():
            out[end] = a[start:end+1].mean()
            for i in range(end+1, len(a)):
                if np.isfinite(a[i]): out[i] = (out[i-1]*(n-1)+a[i])/n
    return pd.Series(out, index=s.index)

def enrich(d):
    d = d.copy()
    c = d.close
    for n in (20, 50, 200): d[f"ema{n}"] = ema(c, n)
    tr = pd.concat([d.high-d.low, (d.high-c.shift()).abs(), (d.low-c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = rma(tr)
    delta = c.diff(); gain = rma(delta.clip(lower=0)); loss = rma(-delta.clip(upper=0))
    d["rsi"] = (100-100/(1+gain/loss.replace(0, np.nan))).where(loss != 0, 100).where((gain != 0) | (loss != 0), 50)
    up = d.high.diff(); down = -d.low.diff()
    plus = rma(up.where((up > down) & (up > 0), 0))/d.atr*100
    minus = rma(down.where((down > up) & (down > 0), 0))/d.atr*100
    dx = (100*(plus-minus).abs()/(plus+minus).replace(0, np.nan)).where((plus+minus) != 0, 0)
    d["adx"] = rma(dx)
    macd = ema(c,12)-ema(c,26); d["macd_hist"] = macd-ema(macd,9)
    mid = c.rolling(20).mean(); std = c.rolling(20).std(ddof=0)
    d["bb_upper"] = mid+2*std; d["bb_lower"] = mid-2*std; d["bb_width"] = 4*std/mid
    d["rel_volume"] = d.volume/d.volume.shift().rolling(20).mean().replace(0,np.nan)
    day = d.ts//86400000; typical = (d.high+d.low+d.close)/3
    d["vwap"] = (typical*d.volume).groupby(day).cumsum()/d.volume.groupby(day).cumsum().replace(0,np.nan)
    d["don_high"] = d.high.shift().rolling(20).max(); d["don_low"] = d.low.shift().rolling(20).min()
    return d

def pivots(d, right=3, left=3):
    result = []
    if len(d)<left+right+1:return result
    times=d.ts.to_numpy();closes=d.close_ts.to_numpy()
    for kind in ("high","low"):
        values=d[kind].to_numpy();windows=np.lib.stride_tricks.sliding_window_view(values,left+right+1)
        centers=windows[:,left]
        mask=(centers==(windows.max(axis=1) if kind=="high" else windows.min(axis=1)))&((windows==centers[:,None]).sum(axis=1)==1)
        for i in np.flatnonzero(mask)+left:
            result.append({"kind":kind,"price":float(values[i]),"ts":int(times[i]),"confirmed_ts":int(closes[i+right]),"index":int(i)})
    return sorted(result,key=lambda x:(x["index"],x["kind"]))

def profile(d, bins=24):
    """OHLCV approximation, distributes bar volume uniformly over its range."""
    d = d.tail(96); lo=float(d.low.min()); hi=float(d.high.max())
    if hi <= lo: return lo
    edges=np.linspace(lo,hi,bins+1); weights=np.zeros(bins)
    for r in d.itertuples():
        overlap=np.maximum(0, np.minimum(edges[1:],r.high)-np.maximum(edges[:-1],r.low))
        if overlap.sum()>0: weights += overlap/overlap.sum()*r.volume
        else: weights[min(bins-1,max(0,int(np.searchsorted(edges,r.close)-1)))] += r.volume
    j=int(weights.argmax()); return float((edges[j]+edges[j+1])/2)

def finite(x):
    try: return float(x) if np.isfinite(x) else None
    except (TypeError,ValueError): return None
