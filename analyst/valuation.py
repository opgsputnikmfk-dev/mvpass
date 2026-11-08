"""Read-only open-position valuation. Never affects fills, stops or risk limits."""
import math

def position_view(p,account,quotes,now_ms):
    row=p.to_dict()
    quote=quotes.get(p.symbol,{})
    minute_ts=account.last_ts+60000 if account.last_ts else None
    price=None;timestamp=None;kind=None
    bid=quote.get("bid");ask=quote.get("ask");ts=quote.get("ts")
    valid=(isinstance(ts,(int,float)) and 0<=now_ms-ts<=60000
           and all(isinstance(x,(int,float)) and math.isfinite(x) and x>0 for x in (bid,ask)) and ask>=bid)
    if valid and (minute_ts is None or ts>=minute_ts):
        price=bid if p.side==1 else ask;timestamp=int(ts);kind="bid" if p.side==1 else "ask"
    else:
        candidate=account.last_prices.get(p.symbol)
        if minute_ts and isinstance(candidate,(int,float)) and math.isfinite(candidate) and candidate>0:
            price=float(candidate);timestamp=minute_ts;kind="minute_close"
    age=max(0,now_ms-timestamp) if timestamp else None
    row.update(current_price=price,price_ts=timestamp,price_kind=kind,price_age_seconds=age/1000 if age is not None else None,
               price_stale=age is None or age>120000,unrealized_gross=None,estimated_pnl=None,estimated_pnl_pct=None)
    if price is not None:
        fill=price*(1-p.side*p.rules["slippage_bps"]/10000)
        gross=p.side*(price-p.entry)*p.qty
        exit_fee=fill*p.qty*p.rules["fee_rate"]
        net=p.side*(fill-p.entry)*p.qty+p.partial_gross-p.entry_fee-exit_fee-p.partial_fee-p.funding_paid
        row.update(unrealized_gross=gross,estimated_pnl=net,estimated_pnl_pct=100*net/(p.entry*(p.qty+(p.tp1_qty if p.tp1_exit_ts is not None else 0))))
    return row
