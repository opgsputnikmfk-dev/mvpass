"""Single-position research accounting; no exchange, signal or download code."""
import math
from research_equity import EquityRecorder


class Account:
    def __init__(self, start, end, initial=10000., fee=.00055, slip=.00045):
        if not all(math.isfinite(x) and 0<=x<1 for x in (fee,slip)):
            raise ValueError("Invalid costs")
        self.cash=self.initial=float(initial)
        self.fee,self.slip=fee,slip
        self.position=None
        self.closed=[]
        self.recorder=EquityRecorder(start,end,initial)

    @staticmethod
    def price(x):
        if not math.isfinite(x) or x<=0:
            raise ValueError("Invalid price")
        return x

    def _close(self, price, ts):
        p=self.position
        fill=self.price(price)*(1-p["side"]*self.slip)
        exit_fee=abs(p["units"])*fill*self.fee
        gross=p["units"]*(fill-p["entry"])
        self.cash+=gross-exit_fee
        net=gross-p["fee"]-exit_fee-p["funding"]
        self.closed.append(dict(entry_ts=p["ts"],exit_ts=ts,net=net))
        self.position=None

    def step(self, ts, price, mark, target=None, fraction=.1, funding_rate=None):
        """Caller supplies a causal target (-1,0,1), or None to hold.

        Funding on the old position, close/open at trading open, then record
        liquidation equity at mark open. Same-target means hold. No intrabar
        stops; this primitive must not be used as an OHLC stop simulator.
        """
        if ts!=self.recorder.expected or ts>=self.recorder.end or self.recorder.finished:
            raise ValueError("Minute out of order")
        self.price(price);self.price(mark)
        if target not in (None,-1,0,1) or not math.isfinite(fraction) or not 0<fraction<=1:
            raise ValueError("Invalid target or fraction")
        if funding_rate is not None and not math.isfinite(funding_rate):
            raise ValueError("Invalid funding")
        p=self.position
        if p and funding_rate is not None:
            payment=p["units"]*mark*funding_rate
            p["funding"]+=payment
            self.cash-=payment
        current=p["side"] if p else 0
        if target is not None and target!=current:
            if p:self._close(price,ts)
            if target:
                if self.cash<=0:
                    raise ValueError("Insolvent account")
                fill=price*(1+target*self.slip)
                units=target*self.cash*fraction/fill
                fee=abs(units)*fill*self.fee
                self.cash-=fee
                self.position=dict(side=target,units=units,entry=fill,
                                   fee=fee,funding=0.,ts=ts)
        p=self.position
        equity=self.cash
        if p:
            fill=mark*(1-p["side"]*self.slip)
            equity+=p["units"]*(fill-p["entry"])-abs(p["units"])*fill*self.fee
        self.recorder.append(ts,equity)
        return equity

    def finish(self, price):
        if self.recorder.expected!=self.recorder.end or self.recorder.finished:
            raise ValueError("Incomplete account")
        self.price(price)
        if self.position:self._close(price,self.recorder.end)
        if not math.isclose(self.cash-self.initial,sum(t["net"] for t in self.closed),
                            rel_tol=1e-9,abs_tol=1e-7):
            raise ValueError("Cash does not reconcile")
        return self.recorder.finish(self.cash)
