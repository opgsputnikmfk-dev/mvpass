"""Offline minute replay adapter, intentionally no download or strategy discovery."""
from research_accounting import Account
from research_equity import MINUTE
from research_lock import verify


def replay(rows, start, end, targets, terminal_price, fee=.00055, slip=.00045, fraction=.1):
    """rows: ts, open, mark, funding_rate (None except known events).
    targets: ts -> {target, decision_ts}; decision must predate fill by >=1min.
    No future candle fields are passed to the accounting engine.
    """
    account=Account(start,end,fee=fee,slip=slip)
    targets=dict(targets)
    for ts,item in targets.items():
        if ts<start or ts>=end or ts%MINUTE or item["decision_ts"]>ts-MINUTE:
            raise ValueError("Unregistered, misaligned or noncausal target")
    last_price=None
    for row in rows:
        ts=row["ts"]
        item=targets.pop(ts,None)
        account.step(ts,row["open"],row["mark"],
                     target=None if item is None else item["target"],
                     fraction=fraction,funding_rate=row["funding_rate"])
        last_price=row["open"]
    if targets or last_price is None:
        raise ValueError("Unexecuted targets or empty period")
    result=account.finish(terminal_price)
    result.update(trades=account.closed,
                  terminal_price_semantics="explicit trading price at end boundary")
    return result


def guarded_replay(lock_path, root, anchor, window, load_rows, targets, **kwargs):
    """Verify BEFORE invoking a holdout loader. No authorization implied."""
    doc=verify(lock_path,root,anchor)
    if window not in doc["windows"]:
        raise ValueError("Window not registered")
    from datetime import datetime,timezone
    start,end=[int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()*1000)
               for t in window]
    return replay(load_rows(start,end),start,end,targets,**kwargs)
