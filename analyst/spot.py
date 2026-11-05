"""Binance's documented, unauthenticated market-data-only API. No orders."""
import json
import math
import re
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import pandas as pd
from .data import DataError
from .models import MINUTE
from .indicators import clean_candles,COLUMNS,enrich
from .universe import STABLES

TOKENIZED_BASES={"SNDKB","SOXLB","SPCXB","CRCLB","COINB","MSTRB","HOODB","NVDAB","TSLAB","AAPLB","MSFTB","AMZNB","GOOGLB","METAB","PLTRB","SPYB","QQQB","AVGOB","AMDB"}

class BinanceSpot:
    BASE="https://data-api.binance.vision"
    spot_reference=True
    def __init__(self):
        self._info=None;self._info_ts=0;self._lock=threading.Lock()

    def get(self,path,params=None):
        if path not in {"/api/v3/time","/api/v3/exchangeInfo","/api/v3/klines","/api/v3/depth","/api/v3/ticker/24hr"}:
            raise DataError("Только публичные данные Binance Spot")
        url=self.BASE+path+("?"+urllib.parse.urlencode(params) if params else "")
        for attempt in range(3):
            try:
                request=urllib.request.Request(url,headers={"User-Agent":"AI-Crypto-Analyst/4.3"})
                with urllib.request.urlopen(request,timeout=15) as r:raw=r.read(20_000_000)
                try:d=json.loads(raw)
                except (ValueError,UnicodeDecodeError):raise DataError("Binance Spot вернул не-JSON")
                if isinstance(d,dict) and d.get("code",0)<0:raise DataError(f"Binance Spot code={d['code']}")
                return d
            except urllib.error.HTTPError as e:
                if e.code not in (429,500,502,503,504) or attempt==2:raise DataError(f"Binance Spot HTTP {e.code}") from None
                # Respect server backoff, but never hold a worker indefinitely.
                if e.code==429:
                    try:delay=float(e.headers.get("Retry-After","1"))
                    except ValueError:delay=1
                    time.sleep(min(15,max(1,delay)))
            except (urllib.error.URLError,TimeoutError,OSError):
                if attempt==2:raise DataError("Нет соединения с Binance Spot") from None
            time.sleep(.5*2**attempt)
        raise DataError("Не удалось получить Binance Spot")

    def server_time(self):return int(self.get("/api/v3/time")["serverTime"])

    def info(self):
        with self._lock:
            if self._info is None or time.time()-self._info_ts>3600:
                self._info=self.get("/api/v3/exchangeInfo");self._info_ts=time.time()
            return self._info

    def instrument(self,symbol):
        x=next((x for x in self.info()["symbols"] if x["symbol"]==symbol),None)
        if not x or x.get("status")!="TRADING" or x.get("quoteAsset")!="USDT" or not x.get("isSpotTradingAllowed",False):
            raise DataError(f"Нет активной спотовой пары {symbol}")
        filters={f["filterType"]:f for f in x["filters"]};q=filters["LOT_SIZE"];market=filters.get("MARKET_LOT_SIZE",{})
        step=float(market.get("stepSize",0)) or float(q["stepSize"])
        return {"symbol":symbol,"tick_size":float(filters["PRICE_FILTER"]["tickSize"]),"qty_step":step,
                "min_qty":max(float(q["minQty"]),float(market.get("minQty",0))),
                "max_qty":float(market.get("maxQty",0)) or float(q["maxQty"]),
                "min_notional":float(filters.get("NOTIONAL",filters.get("MIN_NOTIONAL",{})).get("minNotional",5)),
                "funding_interval_minutes":0,"observed_ts":int(time.time()*1000),"price_source":"binance_spot_reference"}

    def candles(self,symbol,start,end):
        rows=[];cursor=int(start)
        while cursor<=end:
            page=self.get("/api/v3/klines",{"symbol":symbol,"interval":"1m","startTime":cursor,"endTime":int(end),"limit":1000})
            if not page:break
            if int(page[0][0])!=cursor:raise DataError(f"Пропуск минутных свечей Binance Spot {symbol}")
            rows.extend([int(r[0]),*map(float,r[1:6])] for r in page)
            nxt=int(page[-1][0])+MINUTE
            if nxt<=cursor:raise DataError("Неверная пагинация спотовых свечей")
            cursor=nxt;time.sleep(.03)
        if not rows:raise DataError(f"Нет спотовых свечей {symbol}")
        d=clean_candles(pd.DataFrame(rows,columns=COLUMNS))
        if int(d.ts.iloc[0])!=start or int(d.ts.iloc[-1])!=end:raise DataError("Неполный спотовый архив")
        return d

    def snapshot(self,symbol):
        # Timestamp is availability time, never a made-up exchange event time.
        b=self.get("/api/v3/depth",{"symbol":symbol,"limit":100})
        bids=[[float(v) for v in r] for r in b["bids"]];asks=[[float(v) for v in r] for r in b["asks"]]
        if not bids or not asks:raise DataError("Пустой спотовый стакан")
        bid,ask=bids[0][0],asks[0][0];mid=(bid+ask)/2
        if not math.isfinite(mid) or bid<=0 or ask<bid:raise DataError("Неверные цены спотового стакана")
        return {"ts":int(time.time()*1000),"bid":bid,"ask":ask,"spread_bps":(ask-bid)/mid*10000,
                "depth_usdt":min(sum(p*q for p,q in bids if p>=mid*.999),sum(p*q for p,q in asks if p<=mid*1.001)),
                "mark_price":None,"funding_rate":None,"next_funding_ts":None,"oi_change_pct":None,"open_interest":None}

    def funding(self,*_):return []  # not applicable; never fabricated

    def analysis_frames(self,symbol,store,end):
        """Closed exchange candles, persistent EMA seeds, bounded cold start.

        Minute bars are used for execution. Analysis warmup is fetched directly
        on each timeframe, rather than downloading 16 days of minutes per coin.
        """
        result={}
        for minutes in (5,15,60,240):
            duration=minutes*MINUTE;closed_end=end//duration*duration
            key=f"binance_spot_htf:{symbol}:{minutes}"
            rows=store.get(key,[])
            warmup=96 if minutes==240 else 320
            start=int(rows[-1]["ts"])+duration if rows else closed_end-warmup*duration
            cursor=start
            interval={5:"5m",15:"15m",60:"1h",240:"4h"}[minutes]
            while cursor<closed_end:
                page=self.get("/api/v3/klines",{"symbol":symbol,"interval":interval,"startTime":cursor,"endTime":closed_end-1,"limit":1000})
                if not page or int(page[0][0])!=cursor:raise DataError(f"Неполный прогрев {symbol} {interval}")
                for r in page:
                    ts=int(r[0])
                    if ts+duration>closed_end:continue
                    rows.append(dict(zip(COLUMNS,[ts,*map(float,r[1:6])])))
                nxt=int(page[-1][0])+duration
                if nxt<=cursor:raise DataError("Неверный порядок старших свечей")
                cursor=nxt
            d=clean_candles(pd.DataFrame(rows),continuous=False)
            if (d.ts%duration!=0).any() or not (d.ts.diff().dropna()==duration).all() or len(d)<warmup or int(d.ts.iloc[-1])+duration!=closed_end:
                raise DataError(f"Пропуск свечей {symbol} {interval}")
            store.set(key,d.to_dict("records"));d["close_ts"]=d.ts+duration;result[minutes]=enrich(d)
        return result

    def top20(self,warmup_days):
        eligible={x["symbol"]:x for x in self.info()["symbols"] if x.get("status")=="TRADING" and x.get("quoteAsset")=="USDT"
                  and x.get("isSpotTradingAllowed",False) and x.get("baseAsset") not in STABLES|TOKENIZED_BASES|{"PAXG","XAUT","XAU","XAG"}
                  and re.fullmatch(r"[A-Z0-9]{2,20}USDT",x["symbol"]) and not re.search(r"[235](?:L|S)$",x.get("baseAsset",""))}
        now=self.server_time();cutoff=now//MINUTE*MINUTE-int(warmup_days)*86400000
        candidates=[]
        for t in self.get("/api/v3/ticker/24hr"):
            if t.get("symbol") not in eligible:continue
            try:volume=float(t["quoteVolume"]);bid=float(t["bidPrice"]);ask=float(t["askPrice"])
            except (KeyError,TypeError,ValueError):continue
            if not all(math.isfinite(v) for v in (volume,bid,ask)) or volume<=0 or bid<=0 or ask<bid:continue
            candidates.append({"symbol":t["symbol"],"turnover24h":volume,"spread_bps":(ask-bid)/((ask+bid)/2)*10000})
        from concurrent.futures import ThreadPoolExecutor
        ranked=[];ordered=sorted(candidates,key=lambda c:(-c["turnover24h"],c["symbol"]))
        def has_history(c):
            first=self.get("/api/v3/klines",{"symbol":c["symbol"],"interval":"1m","startTime":cutoff,"endTime":cutoff+MINUTE-1,"limit":1})
            return bool(first and int(first[0][0])==cutoff)
        # Bounded batches preserve turnover ordering without hundreds of calls.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for offset in range(0,min(80,len(ordered)),10):
                batch=ordered[offset:offset+10]
                valid=list(pool.map(has_history,batch))
                ranked.extend(c for c,ok in zip(batch,valid) if ok)
                if len(ranked)>=20:break
        ranked=ranked[:20]
        return ranked,self.server_time()
