from __future__ import annotations
import copy
import threading
import time
from .config import DEFAULT,validate,version
from .models import MINUTE
from .analysis import analyze,prepare
from .execution import Ledger
from .data import Bybit,DataError
from .indicators import clean_candles

def variants(config):
    base=copy.deepcopy(config);base["filters"]={k:"off" for k in base["filters"]}
    fib=copy.deepcopy(base)
    for k in ("ema","atr","fibonacci","room"):fib["filters"][k]="on"
    return {"active":copy.deepcopy(config),"baseline":base,"fibonacci":fib}

class Engine:
    def __init__(self,store,config=None,demo=False):
        self.store=store;self.lock=threading.RLock();self.stop=threading.Event();self.demo=demo
        self.config=validate(store.get("config",config or DEFAULT));self.store.save_config(self.config)
        from .spot import BinanceSpot
        self.api=BinanceSpot() if self.config["exchange"]=="binance" else Bybit();self.accounts={};self.market={};self.last_ts=0;self.last_analysis={};self.expected_funding={}
        self.status="DEMO" if demo else "STARTING";self.updated_at=0;self.frames={};self.metadata={};self.generation="";self.series_seq=0
        saved=store.get("engine")
        if saved:
            for k in ("market","last_ts","last_analysis","updated_at","expected_funding","series_seq"):setattr(self,k,saved.get(k,getattr(self,k)))
            self.generation=saved.get("generation",version(self.config))
            for key,s in saved.get("accounts",{}).items():self.accounts[key]=Ledger(s["config"],s["name"],s)
        self.activate()
        self.universe=store.get("universe_selection",{"mode":"manual","verified":False})
        self.manage_universe=False
        self.live_prices={};self.quote_error=None;self.notifications_enabled=True

    def refresh_quotes(self):
        with self.lock:symbols={p.symbol for a in self.accounts.values() for p in a.positions.values()}
        if not symbols:return
        quotes=self.api.live_quotes(symbols)
        with self.lock:
            self.live_prices=quotes;self.quote_error=None

    def quote_loop(self):
        while not self.stop.is_set():
            try:self.refresh_quotes()
            except Exception:
                with self.lock:self.quote_error="Свежие котировки временно недоступны; проверьте время цены"
            self.stop.wait(10)

    def enable_top20(self):
        """One-time migration of saved three-coin configs, without resetting filters."""
        from .universe import FALLBACK20
        self.manage_universe=True
        if self.universe.get("mode")!="top20":
            c=copy.deepcopy(self.config);c["symbols"]=list(FALLBACK20)
            self.update_config(c)
            self.universe={"mode":"top20","verified":False,"ranked_at":None,"next_attempt":0,
                           "label":"Резервный список из 20 монет; рейтинг биржи ещё не получен"}
            self.store.set("universe_selection",self.universe)

    def refresh_top20(self):
        from .universe import fetch_top20
        now=int(time.time()*1000)
        if not self.manage_universe or now<self.universe.get("next_attempt",0):return
        try:
            ranked,observed=fetch_top20(self.api,self.config["scanner"]["warmup_days"])
            if len(ranked)!=20:raise DataError("Для топ-20 не хватает подходящих торгуемых контрактов")
            if abs(observed-int(time.time()*1000))>120000:raise DataError("Биржа вернула устаревший рейтинг")
            symbols=[r["symbol"] for r in ranked]
            if set(symbols)!=set(self.config["symbols"]):
                c=copy.deepcopy(self.config);c["symbols"]=symbols
                self.update_config(c)
            self.universe={"mode":"top20","verified":True,"ranked_at":observed,
                           "next_attempt":now+86400000,"ranking":ranked,
                           "label":("Топ-20 Binance Spot по обороту за 24 часа · спотовые ориентиры" if getattr(self.api,"spot_reference",False) else "Топ-20 подходящих криптофьючерсов по обороту Bybit за 24 часа")}
        except (DataError,ValueError) as e:
            label=("Последний сохранённый список из 20 монет; актуальный рейтинг недоступен"
                   if self.universe.get("ranked_at") else "Резервный список из 20 монет; рейтинг биржи недоступен")
            self.universe={**self.universe,"verified":False,"label":label,"next_attempt":now+3600000,"last_error":str(e)[:200]}
            self.store.log("WARNING",f"Рейтинг топ-20 не обновлён: {e}")
        self.store.set("universe_selection",self.universe)

    def activate(self,new=False):
        if new or not self.generation:
            self.series_seq+=1;self.generation=version(self.config)+"-"+str(self.series_seq)
        for name,c in variants(self.config).items():
            key=self.generation+":"+name
            if key not in self.accounts:self.accounts[key]=Ledger(c,name)

    def dump(self):
        return {"accounts":{k:v.dump() for k,v in self.accounts.items()},"market":self.market,
                "last_ts":self.last_ts,"last_analysis":self.last_analysis,"updated_at":self.updated_at,
                "expected_funding":self.expected_funding,"generation":self.generation,"series_seq":self.series_seq}

    def update_config(self,c):
        c=validate(c)
        if (c["exchange"],c["market"])!=(self.config["exchange"],self.config["market"]):
            raise ValueError("Смена источника требует отдельного запуска и отдельной базы статистики")
        with self.lock:
            # Keep exchange/universe stable while older accounts have exposure.
            protected={p.symbol for a in self.accounts.values() for p in a.positions.values()}
            protected.update(item["candidate"]["symbol"] for a in self.accounts.values() for item in a.pending.values())
            if protected-set(c["symbols"]):
                raise ValueError("Измените список монет после закрытия текущих учебных сделок")
            # Cancel unfilled signals on ANY configuration change. Open trades
            # retain their immutable entry-time exit/risk rules.
            for account in self.accounts.values():
                for cid in list(account.pending):account.reject(cid,"Изменена конфигурация",int(time.time()*1000))
                account.pending.clear()
            self.config=c;self.activate(new=True)
            self.store.save_config_and_state(c,self.dump())
        return self.generation

    def state(self):
        with self.lock:
            from .dashboard import report
            from .valuation import position_view
            now=int(time.time()*1000)
            return {"app":"AI Crypto Analyst","version":"4.4.0","mode":"SYNTHETIC_DEMO" if self.demo else "PAPER",
                    "quote_error":self.quote_error,
                    "data_source":("Binance Spot · ориентиры LONG/SHORT" if self.config["exchange"]=="binance" else "Bybit USDT perpetual"),
                    "dashboard":report(self),
                    "universe":copy.deepcopy(self.universe),
                    "status":self.status,"updated_at":self.updated_at,"generation":self.generation,"config":copy.deepcopy(self.config),
                    "market":copy.deepcopy(self.market),"accounts":{k:{"stats":a.stats(),"positions":[position_view(p,a,self.live_prices,now) for p in a.positions.values()],
                    "trades":a.closed[-100:],"curve":a.equity_curve[-2000:],"rejections":a.rejections[-20:]} for k,a in self.accounts.items()},
                    "candidates":self.store.recent_candidates(40),"logs":self.store.logs()}

    def cycle(self):
        self.refresh_top20()
        with self.lock:generation=self.generation
        bundle=self.collect_market()
        self.apply_market(bundle,expected_generation=generation)

    def collect_market(self,symbols_override=None,last_override=None):
        # Network work is outside the engine lock; config captured and checked
        # before applying results, so UI requests stay responsive.
        with self.lock:cfg=copy.deepcopy(self.config);generation=self.generation;last=self.last_ts
        if last_override is not None:last=last_override
        end=self.api.server_time()//MINUTE*MINUTE
        if abs(end-int(time.time()*1000))>120000:raise DataError("Часы сервера и биржи расходятся")
        symbols=sorted(set(symbols_override or cfg["symbols"])|{"BTCUSDT"});candles={};snaps={};events={}
        metadata={};frames={}
        def load_symbol(symbol):
            cached=self.store.candles(symbol)
            start=(end-8*60*MINUTE if getattr(self.api,"spot_reference",False) else end-int(cfg["scanner"]["warmup_days"])*86400000) if cached.empty else int(cached.ts.iloc[-1])+MINUTE
            if start<end:
                fresh=self.api.candles(symbol,start,end-MINUTE);self.store.add_candles(symbol,fresh)
                cached=self.store.candles(symbol)
            cutoff=end-int(cfg["scanner"]["warmup_days"])*86400000
            if last and last<cutoff:raise DataError("Перерыв больше окна истории; нужен отдельный разбор пропуска")
            # Keep the same indicator seed across restarts and forward cycles.
            # Sliding the warmup boundary would silently change recursive EMAs.
            d=clean_candles(cached)
            if int(d.ts.iloc[-1])!=end-MINUTE:raise DataError("Не хватает свежих минутных свечей")
            candles[symbol]=d;frames[symbol]=self.api.analysis_frames(symbol,self.store,end) if hasattr(self.api,"analysis_frames") else prepare(d)
            metadata[symbol]=self.api.instrument(symbol)
            try:
                snaps[symbol]=self.api.snapshot(symbol);self.store.snapshot(symbol,snaps[symbol])
            except DataError as e:
                self.store.log("WARNING",f"{symbol}: дополнительные фильтры недоступны: {e}")
            if last:
                events[symbol]=self.api.funding(symbol,last+MINUTE,end-1)
                expected=self.expected_funding.get(symbol)
                if expected and last<expected<end and not any(e["ts"]==expected for e in events[symbol]):
                    raise DataError("Ожидаемый funding ещё не опубликован; повторим цикл")
            else:events[symbol]=[]
        if getattr(self.api,"spot_reference",False):
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(load_symbol,symbols))
        else:
            for symbol in symbols:load_symbol(symbol)
        now=int(time.time()*1000)
        if now-end>cfg["scanner"]["max_data_age_seconds"]*1000:raise DataError("Загрузка заняла слишком долго; данные устарели")
        return {"end":end,"now":now,"symbols":symbols,"candles":candles,"frames":frames,"metadata":metadata,"snaps":snaps,"events":events}

    def apply_market(self,bundle,expected_generation=None):
        end,now,symbols,candles,frames,metadata,snaps,events=(bundle[k] for k in ("end","now","symbols","candles","frames","metadata","snaps","events"))
        with self.lock:
            if expected_generation is not None and expected_generation!=self.generation:return
            cfg=copy.deepcopy(self.config)
            if not set(cfg["symbols"]).issubset(frames):raise DataError("Ожидание данных нового списка монет")
            if now-end>cfg["scanner"]["max_data_age_seconds"]*1000:raise DataError("Данные профиля устарели")
            before=self.dump();candidates=[];decisions=[];alerts=[]
            try:
                start=self.last_ts+MINUTE if self.last_ts else end-MINUTE
                indexed={s:d.set_index("ts") for s,d in candles.items()}
                fund_by={s:{e["ts"]:e for e in ee} for s,ee in events.items()}
                for ts in range(start,end,MINUTE):
                    bars={s:indexed[s].loc[ts].to_dict() for s in symbols}
                    funding={s:[fund_by[s][ts]] for s in symbols if ts in fund_by[s]}
                    for key,a in self.accounts.items():
                        a.funding_quality="not_applicable_spot_reference" if getattr(self.api,"spot_reference",False) else "historical_rates_mark_1m_approximation"
                        notes=a.step(ts,bars,metadata,funding)
                        if self.notifications_enabled and key.endswith(":active"):
                            alerts.extend({"kind":"execution","account":key,"event":note} for note in notes)
                    self.last_ts=ts
                for symbol in cfg["symbols"]:
                    bar_end=int(frames[symbol][5].close_ts.iloc[-1])
                    if self.last_analysis.get(symbol)==bar_end:continue
                    found,status=analyze(symbol,frames[symbol],now,cfg,snaps.get(symbol),frames["BTCUSDT"])
                    self.market[symbol]=status;self.last_analysis[symbol]=bar_end
                    for c in found:
                        candidates.append(c)
                        for name in variants(cfg):
                            key=self.generation+":"+name;a=self.accounts[key]
                            if cfg["paused"]:ok,reasons=False,["Сканер на паузе"]
                            else:ok,reasons=a.propose(c)
                            decisions.append((key,c.id,ok,reasons))
                            if self.notifications_enabled and ok and name=="active":alerts.append({"kind":"candidate","candidate":c.to_dict(),"version":self.generation})
                for s,snap in snaps.items():self.expected_funding[s]=snap["next_funding_ts"]
                self.updated_at=now;self.status="PAUSED" if cfg["paused"] else "RUNNING"
                self.store.commit_cycle(self.dump(),candidates,decisions,alerts)
                self.frames=frames;self.metadata=metadata
            except Exception:
                for key,s in before["accounts"].items():self.accounts[key]=Ledger(s["config"],s["name"],s)
                for key in ("market","last_ts","last_analysis","updated_at","expected_funding"):setattr(self,key,before[key])
                raise

    def run(self):
        while not self.stop.is_set():
            try:self.cycle()
            except Exception as e:
                with self.lock:self.status="DATA_ERROR"
                self.store.log("ERROR",f"{type(e).__name__}: {str(e)[:300]}")
            self.stop.wait(self.config["scanner"]["poll_seconds"])
