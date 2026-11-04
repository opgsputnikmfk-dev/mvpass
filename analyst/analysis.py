from __future__ import annotations
import hashlib
import math
import numpy as np
from .models import Candidate, Check
from .config import FILTERS
from .indicators import aggregate, enrich, pivots, profile, finite

def prepare(candles):
    return {m: enrich(aggregate(candles, m)) for m in (5,15,60,240)}

def closed_views(frames, ts):
    return {m: f.iloc[:int(f.close_ts.searchsorted(ts, side="right"))].tail(240).reset_index(drop=True)
            for m, f in frames.items()}

def allowed(candidate, config):
    reasons=[]
    if not candidate.structural_ok: reasons.append("Стоп или структура сценария недопустимы")
    if candidate.setup not in config["setups"]: reasons.append("Сценарий выключен")
    for key, mode in config["filters"].items():
        if mode == "on":
            check=candidate.checks[key]
            if check.passed is not True:
                reasons.append(FILTERS[key]+(": нет данных" if check.passed is None else ": не пройден"))
    return not reasons, reasons

def _direction(h):
    if len(h)<201: return 0
    x=h.iloc[-1]
    if not np.isfinite(x.ema200): return 0
    return 1 if x.close>x.ema200 and x.ema50>h.iloc[-4].ema50 else (-1 if x.close<x.ema200 and x.ema50<h.iloc[-4].ema50 else 0)

def _checks(side, setup, f, ps, level_info, entry, stop, config, snapshot, ts, btc_frames):
    p=config["params"]; a=f[5]; b=f[15]; h=f[60]; x=a.iloc[-1]; y=b.iloc[-1]; z=h.iloc[-1]
    atr=float(y.atr); checks={}
    def put(k, condition, value, detail):
        checks[k]=Check(None if condition is None else bool(condition),value,detail)
    def compare(k,value,pred,detail):
        v=finite(value); put(k,None if v is None else pred(v),v,detail)
    put("ema",_direction(h)==side,_direction(h),"Цена против EMA200 и наклон EMA50, 1ч")
    highs=[q["price"] for q in ps[60] if q["kind"]=="high"]
    lows=[q["price"] for q in ps[60] if q["kind"]=="low"]
    structure=None if min(len(highs),len(lows))<2 else side*(highs[-1]-highs[-2])>0 and side*(lows[-1]-lows[-2])>0
    put("structure",structure,{"highs":highs[-2:],"lows":lows[-2:]},"Только подтверждённые экстремумы: 3 свечи справа")
    trend_setup=setup in ("pullback","breakout")
    compare("regime",z.adx,lambda v: v>=p["adx_min"] if trend_setup else v<p["adx_min"],"Трендовые / диапазонные сценарии разделены по ADX")
    compare("adx",z.adx,lambda v:v>=p["adx_min"],"Высокий ADX подтверждает тренд, без направления")
    if btc_frames is None: put("btc",None,None,"Нет синхронных данных BTC")
    else:
        direction=_direction(closed_views(btc_frames,ts)[60]);put("btc",direction==side,direction,"Часовое направление BTC")
    compare("rsi",y.rsi,lambda v:p["rsi_long_min"]<=v<=p["rsi_long_max"] if side==1 else p["rsi_short_min"]<=v<=p["rsi_short_max"],"Допустимый диапазон импульса, 15м")
    compare("macd",y.macd_hist,lambda v:side*v>0,"Знак гистограммы MACD 12/26/9, 15м")
    compare("atr",atr/entry*100,lambda v:p["atr_min_pct"]<=v<=p["atr_max_pct"],"ATR14 / цена, %")
    compare("bollinger",y.bb_width,lambda v:v>float(b.iloc[-2].bb_width),"Ширина Bollinger(20,2) расширяется")
    compare("volume",x.rel_volume,lambda v:v>=p["volume_min"],"Объём / средний объём 20 предыдущих свечей")
    compare("vwap",x.vwap,lambda v:side*(entry-v)>0,"Выше / ниже VWAP с 00:00 UTC")
    poc=profile(b);put("profile",side*(entry-poc)>0,poc,"POC приближённого профиля последних 96 свечей 15м")
    fib=level_info["fibonacci"]
    put("fibonacci",None if not fib else min(fib["38.2"],fib["61.8"])-p["fib_tolerance_atr"]*atr<=entry<=max(fib["38.2"],fib["61.8"])+p["fib_tolerance_atr"]*atr,fib,"Зона 38,2–61,8% последнего подтверждённого импульса")
    obstacle=level_info["resistance"] if side==1 else level_info["support"]
    room=None if obstacle is None else abs(obstacle-entry)/abs(entry-stop)
    put("room",True if room is None else room>=max(p["room_r"],config["risk"]["target_r"]),room,"Нет найденного уровня либо он за целью; это не гарантия отсутствия препятствий")
    prev=a.iloc[-2];body=abs(x.close-x.open);span=max(x.high-x.low,1e-12)
    engulf=side*(x.close-x.open)>0 and side*(prev.close-prev.open)<0 and min(x.close,x.open)<=min(prev.close,prev.open) and max(x.close,x.open)>=max(prev.close,prev.open)
    wick=(min(x.open,x.close)-x.low) if side==1 else (x.high-max(x.open,x.close))
    pin=wick>=2*max(body,span*.1) and side*(x.close-(x.high+x.low)/2)>0
    put("candle",engulf or pin,{"engulfing":bool(engulf),"rejection":bool(pin)},"Поглощение тела или отбойная свеча")
    pp=[q for q in ps[15] if q["kind"]==("low" if side==1 else "high")]
    div=None
    if len(pp)>=2:
        q,r=pp[-2:];rv=finite(b.iloc[r["index"]].rsi);qv=finite(b.iloc[q["index"]].rsi)
        if rv is not None and qv is not None: div=side*(r["price"]-q["price"])<0 and side*(rv-qv)>0
    put("divergence",div,None,"RSI и цена на двух последних подтверждённых экстремумах 15м")
    boundary=finite(x.don_high if side==1 else x.don_low)
    put("channel",None if boundary is None else side*(entry-boundary)>0,boundary,"Закрытие за каналом 20 предыдущих свечей")
    double=None
    if len(pp)>=2:
        q,r=pp[-2:];between=b.iloc[q["index"]:r["index"]+1]
        neckline=float(between.high.max() if side==1 else between.low.min())
        double=abs(q["price"]-r["price"])<=.5*atr and side*(entry-neckline)>0
    put("double_extreme",double,None,"Экстремумы в пределах 0,5 ATR и пробой линии между ними")
    zones=[];blocks=[]
    for i in range(max(2,len(b)-40),len(b)-1):
        q=b.iloc[i-2];r=b.iloc[i];following=b.iloc[i+1:-1]
        if side==1 and r.low>q.high and (following.empty or following.low.min()>q.high): zones.append((float(q.high),float(r.low)))
        if side==-1 and r.high<q.low and (following.empty or following.high.max()<q.low): zones.append((float(r.high),float(q.low)))
        before=b.iloc[i-1]
        if side*(before.close-before.open)<0 and side*(r.close-r.open)>1.5*float(r.atr) and (following.empty or (following.low.min()>before.low if side==1 else following.high.max()<before.high)):
            blocks.append((float(before.low),float(before.high)))
    def touched(zones): return any(float(y.low)<=hi and float(y.high)>=lo and side*(y.close-y.open)>0 for lo,hi in zones)
    put("fvg",touched(zones),zones[-3:],"Пересечение текущей свечой незакрытого трёхсвечного разрыва")
    put("order_block",touched(blocks),blocks[-3:],"Свеча перед импульсом >1,5 ATR; количественная условная модель")
    snap=snapshot or {}
    # Never backfill historical features with a newer snapshot.
    if snap.get("ts",ts+1)>ts or ts-snap.get("ts",0)>120000: snap={}
    compare("oi",snap.get("oi_change_pct"),lambda v:v>=p["oi_change_min_pct"],"Изменение OI; само по себе не определяет сторону")
    compare("funding",None if snap.get("funding_rate") is None else side*snap["funding_rate"],lambda v:v<=p["max_funding_cost"],"Положительное значение — ожидаемый расход стороны, не факт будущего платежа")
    compare("spread",snap.get("spread_bps"),lambda v:v<=p["max_spread_bps"],"Спред в базисных пунктах")
    compare("depth",snap.get("depth_usdt"),lambda v:v>=p["min_depth_usdt"],"Минимальная глубина двух сторон в ±0,1% цены")
    active=[e["title"] for e in config["events"] if e["start_ms"]<=ts<=e["end_ms"]]
    put("events",not active,active,"Только заданный календарь; автоматического новостного потока нет")
    assert set(checks)==set(FILTERS)
    return checks

def analyze(symbol,frames,ts,config,snapshot=None,btc_frames=None):
    f=closed_views(frames,ts);a,b,h=f[5],f[15],f[60]
    if len(a)<60 or len(b)<60 or len(h)<210: return [], {"status":"WARMUP","detail":"Нужно ≥210 полных часовых свечей"}
    x=a.iloc[-1];atr=finite(b.iloc[-1].atr)
    if atr is None or atr<=0: return [],{"status":"NO_ATR"}
    if ts-int(x.close_ts)>300000: return [],{"status":"STALE"}
    ps={m:pivots(f[m]) for m in (15,60)}
    price=float(x.close);direction=_direction(h);prev=a.iloc[-2]
    trigger_up=price>float(a.iloc[-4:-1].high.max());trigger_down=price<float(a.iloc[-4:-1].low.min())
    trend_zone=b.iloc[-5:]
    touched=bool(((trend_zone.low<=trend_zone.ema20+.25*trend_zone.atr)&(trend_zone.high>=trend_zone.ema20-.25*trend_zone.atr)).any())
    signals=[]
    if direction and touched and (trigger_up if direction==1 else trigger_down): signals.append(("pullback",direction))
    for side in (1,-1):
        boundary=float(x.don_high if side==1 else x.don_low)
        old=float(prev.don_high if side==1 else prev.don_low)
        if side*(price-boundary)>0 and side*(float(prev.close)-old)<=0: signals.append(("breakout",side))
        opposite=float(x.don_low if side==1 else x.don_high)
        extreme=float(x.low if side==1 else x.high)
        if side*(extreme-opposite)<0 and side*(price-opposite)>0: signals.append(("false_break",side))
        yy=b.iloc[-1]
        edge=float(yy.bb_lower if side==1 else yy.bb_upper)
        if side*(float(b.iloc[-2].close)-float(b.iloc[-2].bb_lower if side==1 else b.iloc[-2].bb_upper))<0 and side*(float(yy.close)-edge)>0 and (trigger_up if side==1 else trigger_down): signals.append(("range_reversal",side))
    alllevels=sorted(set(q["price"] for m in ps for q in ps[m]))
    support=max((v for v in alllevels if v<price),default=None);resistance=min((v for v in alllevels if v>price),default=None)
    result=[]
    for setup,side in signals:
        anchor=float(b.tail(5).low.min() if side==1 else b.tail(5).high.max())
        stop=anchor-side*config["risk"]["stop_atr_buffer"]*atr
        distance=side*(price-stop)
        if distance<=0: continue
        target=price+side*distance*config["risk"]["target_r"]
        fib={};swings=ps[15]
        ends=[q for q in swings if q["kind"]==("high" if side==1 else "low")]
        if ends:
            end=ends[-1];starts=[q for q in swings if q["kind"]!=(end["kind"]) and q["index"]<end["index"]]
            if starts:
                start=starts[-1];move=end["price"]-start["price"]
                if side*move>0: fib={str(k):end["price"]-move*k/100 for k in (23.6,38.2,50.0,61.8,78.6)}
        levels={"support":support,"resistance":resistance,"fibonacci":fib,"pivots":swings[-12:]}
        checks=_checks(side,setup,f,ps,levels,price,stop,config,snapshot,ts,btc_frames)
        cid=hashlib.sha256(f"{symbol}:{int(x.close_ts)}:{setup}:{side}".encode()).hexdigest()[:24]
        ok=config["risk"]["min_stop_pct"]<=distance/price<=config["risk"]["max_stop_pct"]
        description=f"{setup}: {'LONG' if side==1 else 'SHORT'}; подтверждение по закрытым свечам 5м, стоп за экстремумом 15м."
        result.append(Candidate(cid,symbol,int(ts),setup,side,price,stop,target,atr,checks,levels,description,ok))
    return result,{"status":"CANDIDATES" if result else "WAIT", "price":price,"direction":direction,
                   "adx":finite(h.iloc[-1].adx),"support":support,"resistance":resistance,
                   "trend_4h":_direction(f[240]) if len(f[240])>=201 else (1 if len(f[240])>=50 and f[240].iloc[-1].ema20>f[240].iloc[-1].ema50 else -1 if len(f[240])>=50 else 0),
                   "closed_at":int(x.close_ts),"detail":"Ожидание отката / пробоя / возврата в диапазон"}
