"""Private strategy engines sharing only public market observations."""
import time

class EngineProfiles:
    def __init__(self,profiles):
        self.profiles=profiles;self.admin=profiles["Admin"];self.stop=self.admin.stop
        for name,engine in profiles.items():
            engine.stop=self.stop;engine.notifications_enabled=name=="Admin"
    def cycle(self,due=None):
        self.admin.refresh_top20()
        engines=list(due or self.profiles.values())
        generations={e:e.generation for e in engines}
        symbols=set().union(*(set(e.config["symbols"]) for e in engines))
        timestamps=[e.last_ts for e in engines if e.last_ts]
        bundle=self.admin.collect_market(symbols,min(timestamps) if timestamps else 0)
        for engine in engines:
            try:engine.apply_market(bundle,generations[engine])
            except Exception as error:
                engine.status="DATA_ERROR";engine.store.log("ERROR",f"{type(error).__name__}: {str(error)[:300]}")
    def run(self):
        deadlines={e:0 for e in self.profiles.values()}
        while not self.stop.is_set():
            due=[e for e,d in deadlines.items() if time.monotonic()>=d]
            if due:
                try:self.cycle(due)
                except Exception as error:
                    for e in due:
                        e.status="DATA_ERROR";e.store.log("ERROR",f"{type(error).__name__}: {str(error)[:300]}")
                for e in due:deadlines[e]=time.monotonic()+e.config["scanner"]["poll_seconds"]
            self.stop.wait(1)
    def quote_loop(self):
        while not self.stop.is_set():
            symbols=set()
            for e in self.profiles.values():
                with e.lock:symbols.update(p.symbol for a in e.accounts.values() for p in a.positions.values())
            try:
                quotes=self.admin.api.live_quotes(symbols) if symbols else {}
                for e in self.profiles.values():
                    with e.lock:e.live_prices=quotes;e.quote_error=None
            except Exception:
                for e in self.profiles.values():
                    with e.lock:e.quote_error="Свежие котировки временно недоступны; проверьте время цены"
            self.stop.wait(10)
