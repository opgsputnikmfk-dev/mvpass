"""Shared event-driven PAPER ledger for live observation and historical replay.

No private exchange endpoints, credentials or real order placement exist here.
"""
from __future__ import annotations
from dataclasses import asdict
from decimal import Decimal, ROUND_FLOOR
import copy
import math
from .models import Position, Candidate, MINUTE
from .config import version
from .analysis import allowed

def floor_step(value, step):
    return float((Decimal(str(value))/Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)*Decimal(str(step)))

class Ledger:
    def __init__(self, config, name="active", saved=None):
        self.config=copy.deepcopy(config); self.name=name; self.version=version(config)
        self.cash=float(config["risk"]["initial_equity"]); self.peak=self.cash
        self.day=None;self.day_start=self.cash;self.halted=False
        self.positions={};self.pending={};self.closed=[];self.last_entry={};self.last_prices={}
        self.last_ts=0;self.equity_curve=[];self.rejections=[]
        self.max_dd=0.0;self.funding_quality="unknown"
        if saved:
            for k,v in saved.items():
                if k not in ("config","positions"): setattr(self,k,v)
            self.positions={k:Position(**v) for k,v in saved.get("positions",{}).items()}

    def dump(self):
        return {**self.__dict__,"positions":{k:asdict(v) for k,v in self.positions.items()}}

    def equity(self):
        return self.cash+sum(p.side*(self.last_prices.get(p.symbol,p.entry)-p.entry)*p.qty for p in self.positions.values())

    def reject(self,cid,reason,ts):
        self.rejections.append({"candidate_id":cid,"reason":reason,"ts":int(ts)})
        self.rejections=self.rejections[-1000:]

    def propose(self,candidate):
        if any(p.symbol==candidate.symbol for p in self.positions.values()):
            return False,["По монете уже открыта сделка"]
        if any(item["candidate"]["symbol"]==candidate.symbol for item in self.pending.values()):
            return False,["По монете уже ожидается вход"]
        ok,reasons=allowed(candidate,self.config)
        if not ok: return False,reasons
        if candidate.id in self.pending or any(p.candidate_id==candidate.id for p in self.positions.values()):
            return False,["Повторный сигнал"]
        # At least one full minute after the decision; never fill the signal candle.
        due=math.ceil(candidate.ts/MINUTE)*MINUTE+int(self.config["risk"]["entry_delay_minutes"])*MINUTE
        self.pending[candidate.id]={"candidate":candidate.to_dict(),"due":due}
        return True,[]

    def _open(self,c,b,meta,ts):
        r=self.config["risk"];eq=self.equity()
        if eq<=self.peak*(1-r["max_drawdown_pct"]):self.halted=True
        if self.config["paused"] or self.halted: return "Пауза / лимит просадки"
        if eq<=self.day_start*(1-r["daily_loss_pct"]): return "Лимит дневного убытка"
        if len(self.positions)>=r["max_positions"] or any(p.symbol==c.symbol for p in self.positions.values()): return "Лимит позиций / символ занят"
        if ts-self.last_entry.get(c.symbol,-10**18)<r["cooldown_minutes"]*MINUTE: return "Повторный вход в период ожидания"
        slip=r["slippage_bps"]/10000
        entry=float(b["open"])*(1+c.side*slip)
        if abs(entry-c.reference)>r["max_entry_drift_atr"]*c.atr: return "Цена ушла от сигнала"
        tick=meta.get("tick_size",0);step=meta.get("qty_step",0)
        if tick<=0 or step<=0: return "Нет спецификации контракта"
        # Round stop away from entry and target towards entry, conservatively.
        stop=floor_step(c.stop,tick) if c.side==1 else -floor_step(-c.stop,tick)
        dist=c.side*(entry-stop)
        if dist<=0 or not r["min_stop_pct"]<=dist/entry<=r["max_stop_pct"]: return "Стоп недопустим после исполнения"
        rawtarget=entry+c.side*dist*r["target_r"]
        target=floor_step(rawtarget,tick) if c.side==1 else -floor_step(-rawtarget,tick)
        obstacle=c.levels.get("resistance" if c.side==1 else "support")
        if self.config["filters"]["room"]=="on" and obstacle is not None and c.side*(target-obstacle)>0: return "После смещения входа цель за встречным уровнем"
        stopfill=stop*(1-c.side*slip)
        unit_loss=c.side*(entry-stopfill)+r["fee_rate"]*(entry+stopfill)
        budget=eq*r["risk_pct"]
        total_risk=sum(p.planned_risk for p in self.positions.values())
        same_risk=sum(p.planned_risk for p in self.positions.values() if p.side==c.side)
        available=min(budget,eq*r["max_open_risk_pct"]-total_risk,eq*r["max_side_risk_pct"]-same_risk)
        used=sum(p.qty*self.last_prices.get(p.symbol,p.entry) for p in self.positions.values())
        notional_cap=max(0,eq*r["max_notional_equity"]-used)
        qty=floor_step(min(max(0,available)/unit_loss,notional_cap/entry,meta.get("max_qty",1e20)),step)
        if qty<meta.get("min_qty",step) or qty*entry<meta.get("min_notional",5) or qty<=0: return "Недостаточно свободного риска / минимальный объём"
        fee=qty*entry*r["fee_rate"];self.cash-=fee
        p=Position(c.id,c.id,c.symbol,c.side,int(ts),entry,stop,stop,target,qty,qty*unit_loss,fee,self.version,copy.deepcopy(r),last_bar=ts-MINUTE,setup=c.setup)
        self.positions[p.id]=p;self.last_entry[c.symbol]=int(ts)
        return None

    def close(self,p,price,ts,reason):
        slip=p.rules["slippage_bps"]/10000
        fill=price*(1-p.side*slip);fee=fill*p.qty*p.rules["fee_rate"]
        gross=p.side*(fill-p.entry)*p.qty
        self.cash+=gross-fee
        p.exit_ts=int(ts);p.exit_price=fill;p.net_pnl=gross-p.entry_fee-fee-p.funding_paid;p.reason=reason
        record=asdict(p);record["net_r"]=p.net_pnl/p.planned_risk;record["fees"]=p.entry_fee+fee
        self.closed.append(record);del self.positions[p.id]
        return record

    def step(self,ts,bars,metadata,funding=None):
        """One chronological minute for ALL symbols, not one symbol's full history.

        Funding events apply to positions held BEFORE that minute's open. Stop
        wins if stop and target are both hit. Trailing changes only for next bar.
        """
        if ts<=self.last_ts: return []
        notifications=[];funding=funding or {}
        for symbol,b in bars.items(): self.last_prices[symbol]=float(b["open"])
        day=int(ts)//86400000
        if day!=self.day: self.day=day;self.day_start=self.equity()
        for p in list(self.positions.values()):
            for event in funding.get(p.symbol,[]):
                if event["ts"]==ts and p.entry_ts<ts:
                    payment=p.side*p.qty*event["mark_price"]*event["rate"]
                    p.funding_paid+=payment;self.cash-=payment
        # Size entries using information at the OPEN only. Intrabar exits must
        # not free capital for an entry made earlier in that same minute.
        def inspect(p):
            if p.symbol not in bars or ts<=p.last_bar:return
            b=bars[p.symbol];o,hi,lo,cl=map(float,(b["open"],b["high"],b["low"],b["close"]))
            stophit=lo<=p.stop if p.side==1 else hi>=p.stop
            targethit=hi>=p.target if p.side==1 else lo<=p.target
            if stophit and targethit:p.ambiguous_bars+=1
            if p.side*(o-p.stop)<=0: notifications.append(self.close(p,o,ts,"SL_GAP"))
            elif stophit: notifications.append(self.close(p,p.stop,ts+MINUTE-1,"SL"))
            elif targethit: notifications.append(self.close(p,p.target,ts+MINUTE-1,"TP"))
            elif ts+MINUTE>=p.entry_ts+p.rules["max_hold_minutes"]*MINUTE: notifications.append(self.close(p,cl,ts+MINUTE-1,"TIMEOUT"))
            else:
                p.last_bar=int(ts)
                if p.rules["trailing_atr"]>0:
                    # ATR fixed at entry candidate, trailing activated after +1R.
                    initial_dist=abs(p.entry-p.initial_stop)
                    if p.side*(cl-p.entry)>=initial_dist:
                        atr=self._atr_for(p)
                        proposed=cl-p.side*atr*p.rules["trailing_atr"]
                        p.stop=max(p.stop,proposed) if p.side==1 else min(p.stop,proposed)
        # Entries sized on open prices only, not this minute's closes/highs/lows.
        for cid,item in sorted(list(self.pending.items()),key=lambda kv:(kv[1]["due"],kv[1]["candidate"]["symbol"],kv[0])):
            if item["due"]>ts:continue
            c=Candidate.from_dict(item["candidate"])
            if c.symbol not in bars: continue
            del self.pending[cid]
            if ts!=item["due"]: self.reject(cid,"Пропущено время входа",ts);continue
            reason=self._open(c,bars[c.symbol],metadata.get(c.symbol,{}),ts)
            if reason:self.reject(cid,reason,ts)
            else:
                self.positions[cid].rules["entry_atr"]=c.atr
                notifications.append({**self.positions[cid].to_dict(),"reason":"ENTRY"})
        for p in list(self.positions.values()):inspect(p)
        for symbol,b in bars.items():self.last_prices[symbol]=float(b["close"])
        eq=self.equity();self.peak=max(self.peak,eq);dd=1-eq/self.peak
        self.max_dd=max(self.max_dd,dd)
        if dd>=self.config["risk"]["max_drawdown_pct"]:self.halted=True
        if not self.equity_curve or ts-self.equity_curve[-1][0]>=15*MINUTE:
            self.equity_curve.append([int(ts+MINUTE),eq])
        self.last_ts=int(ts)
        return notifications

    @staticmethod
    def _atr_for(p):return p.rules["entry_atr"]

    def finish(self,ts):
        for p in list(self.positions.values()):self.close(p,self.last_prices[p.symbol],ts,"END_OF_TEST")
        self.pending.clear()
        self.peak=max(self.peak,self.cash);self.max_dd=max(self.max_dd,1-self.cash/self.peak)
        self.equity_curve.append([int(ts),self.cash])

    def stats(self):
        pnls=[p["net_pnl"] for p in self.closed];profit=sum(max(0,p) for p in pnls);loss=-sum(min(0,p) for p in pnls)
        return {"name":self.name,"version":self.version,"equity":self.equity(),"cash":self.cash,
                "return_pct":(self.equity()/self.config["risk"]["initial_equity"]-1)*100,
                "trades":len(pnls),"wins":sum(p>0 for p in pnls),"win_rate":100*sum(p>0 for p in pnls)/len(pnls) if pnls else None,
                "profit_factor":profit/loss if loss else None,"mean_r":sum(p["net_r"] for p in self.closed)/len(pnls) if pnls else None,
                "max_drawdown_pct":100*self.max_dd,"open":len(self.positions),"pending":len(self.pending),
                "fees":sum(p["fees"] for p in self.closed),"funding_paid":sum(p["funding_paid"] for p in self.closed),
                "halted":self.halted,"funding_quality":self.funding_quality}
