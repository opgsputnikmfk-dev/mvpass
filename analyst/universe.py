"""Point-in-time live universe. Never used to select historical backtest coins."""
import math
import re

FALLBACK20 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
              "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
              "SUIUSDT", "NEARUSDT", "LTCUSDT", "BCHUSDT", "UNIUSDT",
              "AAVEUSDT", "ATOMUSDT", "FILUSDT", "ARBUSDT", "OPUSDT"]
STABLES = {"USDT", "USDC", "USDE", "USDD", "DAI", "TUSD", "FDUSD", "PYUSD", "USD1", "EUR", "EURC", "EURS", "USDP", "USDS", "RLUSD"}

def rank_contracts(instruments, tickers, now, warmup_days=16, count=20):
    eligible={}
    for i in instruments:
        symbol=i.get("symbol", "");base=i.get("baseCoin", "")
        try:launch=int(i["launchTime"])
        except (KeyError,TypeError,ValueError):continue
        # Whitelist crypto regions; unknown/new product categories fail closed.
        if (i.get("status")!="Trading" or i.get("contractType")!="LinearPerpetual"
            or i.get("settleCoin")!="USDT" or i.get("quoteCoin")!="USDT"
            or i.get("symbolType", "") not in ("", "crypto", "innovation", "adventure")
            or i.get("isPreListing",False) or base in STABLES
            or base in {"XAU","XAG","PAXG","XAUT","USOIL","UKOIL"}
            or not re.fullmatch(r"[A-Z0-9]{2,20}USDT",symbol)
            or re.search(r"[235](?:L|S)$",base)
            or launch>now-warmup_days*86400000):continue
        eligible[symbol]=i
    result={}
    for t in tickers:
        symbol=t.get("symbol")
        if symbol not in eligible:continue
        try:
            turnover=float(t["turnover24h"]);bid=float(t["bid1Price"]);ask=float(t["ask1Price"])
        except (ValueError,KeyError,TypeError):continue
        if not all(math.isfinite(v) for v in (turnover,bid,ask)) or turnover<=0 or bid<=0 or ask<bid:continue
        result[symbol]={"symbol":symbol,"turnover24h":turnover,"spread_bps":(ask-bid)/((ask+bid)/2)*10000}
    return sorted(result.values(),key=lambda x:(-x["turnover24h"],x["symbol"]))[:count]

def fetch_top20(api, warmup_days):
    instruments=[];cursor=None;seen=set()
    while True:
        params={"category":"linear","status":"Trading","limit":1000}
        if cursor:params["cursor"]=cursor
        page=api.get("/v5/market/instruments-info",params)["result"]
        instruments.extend(page["list"]);cursor=page.get("nextPageCursor")
        if not cursor:break
        if cursor in seen:raise ValueError("Повторная страница списка контрактов")
        seen.add(cursor)
    raw=api.get("/v5/market/tickers",{"category":"linear"})
    now=int(raw["time"])
    return rank_contracts(instruments,raw["result"]["list"],now,warmup_days),now
