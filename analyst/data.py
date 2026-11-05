"""Public Bybit v5 data, strict validation, no spot or alternative-host fallback."""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
import hashlib
import pandas as pd
import numpy as np
from .indicators import clean_candles,COLUMNS
from .models import MINUTE

class DataError(RuntimeError):pass

class Bybit:
    BASE="https://api.bybit.com"
    def get(self,path,params=None):
        url=self.BASE+path+("?"+urllib.parse.urlencode(params) if params else "")
        for attempt in range(3):
            try:
                req=urllib.request.Request(url,headers={"User-Agent":"AI-Crypto-Analyst/4.0"})
                with urllib.request.urlopen(req,timeout=15) as r:
                    raw=r.read(10_000_000)
                try:d=json.loads(raw)
                except (ValueError,UnicodeDecodeError):raise DataError("Биржа вернула HTML/не-JSON; данные недоступны")
                if d.get("retCode")!=0:raise DataError(f"Bybit retCode={d.get('retCode')}: {str(d.get('retMsg',''))[:120]}")
                return d
            except urllib.error.HTTPError as e:
                if e.code not in (429,500,502,503,504):raise DataError(f"Bybit HTTP {e.code}; проверьте доступность сервиса в регионе") from None
                if attempt==2:raise DataError(f"Bybit HTTP {e.code}") from None
            except (urllib.error.URLError,TimeoutError,OSError):
                if attempt==2:raise DataError("Нет соединения с Bybit") from None
            time.sleep(.5*2**attempt)
        raise DataError("Не удалось получить данные")

    def server_time(self):
        d=self.get("/v5/market/time");return int(d["time"])

    def live_quotes(self,symbols):
        if not symbols:return {}
        rows=self.get("/v5/market/tickers",{"category":"linear"})["result"]["list"]
        now=int(time.time()*1000);out={}
        for r in rows:
            if r.get("symbol") not in symbols:continue
            try:bid=float(r["bid1Price"]);ask=float(r["ask1Price"])
            except (KeyError,ValueError,TypeError):continue
            if np.isfinite([bid,ask]).all() and 0<bid<=ask:out[r["symbol"]]={"bid":bid,"ask":ask,"ts":now}
        return out

    def instrument(self,symbol):
        r=self.get("/v5/market/instruments-info",{"category":"linear","symbol":symbol})["result"]["list"]
        if len(r)!=1:raise DataError(f"Контракт {symbol} не найден")
        x=r[0]
        if x.get("contractType")!="LinearPerpetual" or x.get("settleCoin")!="USDT" or x.get("status")!="Trading":
            raise DataError("Нужен торгуемый USDT perpetual")
        q=x["lotSizeFilter"]
        return {"symbol":symbol,"tick_size":float(x["priceFilter"]["tickSize"]),"qty_step":float(q["qtyStep"]),
                "min_qty":float(q["minOrderQty"]),"max_qty":float(q["maxMktOrderQty"]),"min_notional":float(q.get("minNotionalValue",5)),
                "funding_interval_minutes":int(x["fundingInterval"]),"observed_ts":int(time.time()*1000)}

    def candles(self,symbol,start,end):
        """Inclusive open timestamps start..end, minute aligned, paginate backwards."""
        result={};cursor=int(end)
        while cursor>=start:
            rows=self.get("/v5/market/kline",{"category":"linear","symbol":symbol,"interval":"1","start":int(start),"end":cursor,"limit":1000})["result"]["list"]
            if not rows:break
            for row in rows:
                ts=int(row[0])
                if start<=ts<=end:result[ts]=[ts,*map(float,row[1:6])]
            oldest=min(int(x[0]) for x in rows)
            if oldest>cursor:raise DataError("Неверный порядок страниц свечей")
            cursor=oldest-MINUTE
            time.sleep(.03)
        if not result:raise DataError(f"Нет свечей {symbol}")
        d=clean_candles(pd.DataFrame(list(result.values()),columns=COLUMNS))
        if int(d.ts.iloc[0])!=start or int(d.ts.iloc[-1])!=end:raise DataError("Архив не покрывает запрошенный период")
        return d

    def snapshot(self,symbol):
        ticker=self.get("/v5/market/tickers",{"category":"linear","symbol":symbol})
        t=ticker["result"]["list"][0]
        book=self.get("/v5/market/orderbook",{"category":"linear","symbol":symbol,"limit":200})
        b=book["result"];bids=[[float(v) for v in r] for r in b["b"]];asks=[[float(v) for v in r] for r in b["a"]]
        if not bids or not asks:raise DataError("Пустой стакан")
        bid,ask=bids[0][0],asks[0][0];mid=(bid+ask)/2
        if bid<=0 or ask<bid:raise DataError("Некорректный стакан")
        try:oi=self.get("/v5/market/open-interest",{"category":"linear","symbol":symbol,"intervalTime":"5min","limit":2})["result"]["list"]
        except DataError:oi=[]
        oi=sorted(oi,key=lambda x:int(x["timestamp"]))
        now=int(time.time()*1000)
        if now-int(b["ts"])>120000 or int(b["ts"])>now+30000:raise DataError("Устаревший стакан / неверные часы")
        change=None
        if len(oi)==2 and now-int(oi[-1]["timestamp"])<900000 and float(oi[0]["openInterest"])>0:
            change=(float(oi[1]["openInterest"])/float(oi[0]["openInterest"])-1)*100
        return {"ts":now,"spread_bps":(ask-bid)/mid*10000,"depth_usdt":min(sum(p*q for p,q in bids if p>=mid*.999),sum(p*q for p,q in asks if p<=mid*1.001)),
                "bid":bid,"ask":ask,"mark_price":float(t["markPrice"]),"funding_rate":float(t["fundingRate"]),
                "next_funding_ts":int(t["nextFundingTime"]),"oi_change_pct":change,"open_interest":float(t["openInterest"])}

    def funding(self,symbol,start,end):
        rows={};cursor=int(end)
        while cursor>=start:
            page=self.get("/v5/market/funding/history",{"category":"linear","symbol":symbol,"startTime":int(start),"endTime":cursor,"limit":200})["result"]["list"]
            if not page:break
            for row in page:
                ts=int(row["fundingRateTimestamp"])
                if start<=ts<=end:rows[ts]=float(row["fundingRate"])
            earliest=min(int(r["fundingRateTimestamp"]) for r in page)
            if earliest>cursor:raise DataError("Неверная пагинация funding")
            cursor=earliest-1
            if len(page)<200:break
        result=[]
        for ts,rate in sorted(rows.items()):
            marks=self.get("/v5/market/mark-price-kline",{"category":"linear","symbol":symbol,"interval":"1","start":ts,"end":ts,"limit":1})["result"]["list"]
            if not marks or int(marks[0][0])!=ts:raise DataError("Нет mark price для funding")
            result.append({"ts":ts,"rate":rate,"mark_price":float(marks[0][1]),"mark_source":"1m_mark_open_approximation"})
        return result

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def download_dataset(path,symbols,start,end,progress=print):
    """end is exclusive. Does not invent unavailable historical microstructure."""
    dest=Path(path);dest.mkdir(parents=True,exist_ok=True);api=Bybit();meta={};files={}
    if start%MINUTE or end%MINUTE or end<=start:raise ValueError("Границы данных должны быть минутами UTC")
    if end>api.server_time()//MINUTE*MINUTE:raise ValueError("Период включает незакрытые свечи")
    for symbol in sorted(set(symbols)|{"BTCUSDT"}):
        progress(f"{symbol}: minute candles")
        meta[symbol]=api.instrument(symbol)
        d=api.candles(symbol,start,end-MINUTE);p=dest/f"{symbol}.csv";d.to_csv(p,index=False);files[p.name]=digest(p)
        events=api.funding(symbol,start,end-1);p=dest/f"{symbol}_funding.json";p.write_text(json.dumps(events));files[p.name]=digest(p)
    manifest={"source":"bybit","market":"linear_usdt_perpetual","synthetic":False,"start_ms":start,"end_ms":end,
              "metadata":meta,"files":files,"funding_coverage":"api_history_requested","funding_marks":"1m_open_approximation",
              "historical_microstructure":False,"specifications":"current_not_point_in_time","created_ms":int(time.time()*1000)}
    (dest/"manifest.json").write_text(json.dumps(manifest,indent=2))
    return manifest

def load_dataset(path):
    root=Path(path);m=json.loads((root/"manifest.json").read_text())
    if m.get("source") not in ("bybit","synthetic") or m.get("market")!="linear_usdt_perpetual":raise DataError("Несовместимый источник датасета")
    for file,sha in m["files"].items():
        if Path(file).name!=file or digest(root/file)!=sha:raise DataError("Контрольная сумма датасета не совпадает")
    candles={};funding={}
    for symbol in m["metadata"]:
        d=clean_candles(pd.read_csv(root/f"{symbol}.csv"))
        if int(d.ts.iloc[0])!=m["start_ms"] or int(d.ts.iloc[-1])+MINUTE!=m["end_ms"]:raise DataError("Неполное покрытие данных")
        candles[symbol]=d;events=json.loads((root/f"{symbol}_funding.json").read_text())
        seen=set()
        for e in events:
            if e["ts"] in seen or e["ts"]%MINUTE or not m["start_ms"]<=e["ts"]<m["end_ms"] or not np.isfinite([e["rate"],e["mark_price"]]).all() or e["mark_price"]<=0:raise DataError("Неверные события funding")
            seen.add(e["ts"])
        funding[symbol]=events
    return m,candles,funding

def load_snapshots(path,manifest):
    """Optional JSON records timestamped when actually AVAILABLE, not bar time.

    Files must also be checksummed in manifest.files. Values are observations,
    never interpolated from the future. Missing features stay None.
    """
    result={}
    for symbol,file in manifest.get("snapshot_files",{}).items():
        if symbol not in manifest["metadata"] or file not in manifest["files"]:raise DataError("Неверный файл снимков рынка")
        rows=json.loads((Path(path)/file).read_text());seen=set()
        for r in rows:
            if not isinstance(r.get("ts"),int) or r["ts"] in seen:raise DataError("Неверное время снимка")
            seen.add(r["ts"])
            for k in ("spread_bps","depth_usdt","funding_rate","oi_change_pct"):
                if r.get(k) is not None and not np.isfinite(r[k]):raise DataError("Неверные признаки снимка")
        result[symbol]=sorted(rows,key=lambda r:r["ts"])
    return result

def make_demo(path,days=20,seed=7):
    """Synthetic fixtures, never presented as a profitable market backtest."""
    root=Path(path);root.mkdir(parents=True,exist_ok=True);rng=np.random.default_rng(seed)
    n=int(days)*1440;start=1735689600000;timestamps=start+np.arange(n)*MINUTE
    common=rng.normal(0,.0006,n)+.00007*np.sin(np.arange(n)/500)
    meta={};files={}
    for symbol,base,step,tick in [("BTCUSDT",60000,.001,.1),("ETHUSDT",3000,.01,.01),("SOLUSDT",150,.1,.001)]:
        ret=common+rng.normal(0,.0004,n);close=base*np.exp(np.cumsum(ret));op=np.r_[base,close[:-1]]
        spread=rng.uniform(.0001,.0007,n)*close
        d=pd.DataFrame({"ts":timestamps,"open":op,"high":np.maximum(op,close)+spread,"low":np.minimum(op,close)-spread,"close":close,"volume":rng.lognormal(4, .5,n)})
        p=root/f"{symbol}.csv";d.to_csv(p,index=False);files[p.name]=digest(p)
        events=[{"ts":int(timestamps[i]),"rate":.0001,"mark_price":float(op[i]),"mark_source":"synthetic"} for i in range(0,n,480)]
        p=root/f"{symbol}_funding.json";p.write_text(json.dumps(events));files[p.name]=digest(p)
        meta[symbol]={"tick_size":tick,"qty_step":step,"min_qty":step,"min_notional":5,"max_qty":1e8,"funding_interval_minutes":480}
    m={"source":"synthetic","market":"linear_usdt_perpetual","synthetic":True,"start_ms":start,"end_ms":start+n*MINUTE,
       "metadata":meta,"files":files,"funding_coverage":"synthetic_complete","funding_marks":"synthetic","historical_microstructure":False,"specifications":"synthetic"}
    (root/"manifest.json").write_text(json.dumps(m,indent=2));return m
