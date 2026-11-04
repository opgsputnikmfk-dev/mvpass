"""Read-only, beginner-facing reporting over actual recorded observations."""
from collections import Counter
from .config import FILTERS, SETUPS

STATUS = {"RUNNING":"Анализирует рынок", "STARTING":"Подключается к бирже",
          "DATA_ERROR":"Нет доступа к данным биржи", "PAUSED":"Новые входы на паузе", "DEMO":"Демонстрация"}
NAMES = {"active":"Мои фильтры", "baseline":"Без дополнительных фильтров", "fibonacci":"Тренд + Фибоначчи"}

def report(engine):
    store=engine.store
    with store.lock:
        totals={s:{"candidates":int(n),"last_candidate":t} for s,n,t in store.db.execute("SELECT symbol,count(*),max(ts) FROM candidates GROUP BY symbol")}
        decisions={a:{"checked":int(n),"accepted":int(ok or 0),"start":t,"end":end} for a,n,ok,t,end in store.db.execute("SELECT d.account,count(*),sum(d.accepted),min(c.ts),max(c.ts) FROM decisions d JOIN candidates c ON c.id=d.candidate_id GROUP BY d.account")}
        cache={s:{"candles":int(n),"last_candle":t+60000} for s,n,t in store.db.execute("SELECT symbol,count(*),max(ts) FROM candles GROUP BY symbol")}
    comparisons=[];groups={};attribution={k:{"pass":{"n":0,"sum_r":0},"fail":{"n":0,"sum_r":0},"missing":{"n":0,"sum_r":0}} for k in FILTERS}
    for key,a in engine.accounts.items():
        group=key.rsplit(":",1)[0];groups.setdefault(group,len(groups)+1)
        on=[FILTERS[k] for k,v in a.config["filters"].items() if v=="on"]
        observed=[FILTERS[k] for k,v in a.config["filters"].items() if v=="observe"]
        by_symbol={};by_setup={}
        for t in a.closed:
            for target,name in ((by_symbol,t["symbol"]),(by_setup,SETUPS.get(t["setup"],t["setup"]))):
                item=target.setdefault(name,{"trades":0,"net_pnl":0,"wins":0});item["trades"]+=1;item["net_pnl"]+=t["net_pnl"];item["wins"]+=int(t["net_pnl"]>0)
        period=decisions.get(key,{"checked":0,"accepted":0,"start":None,"end":None})
        comparisons.append({"key":key,"group":group,"series":groups[group],"name":NAMES.get(a.name,a.name),"current":group==engine.generation,
                            "filters":on,"observed":observed,"setups":[SETUPS[k] for k in a.config["setups"]],"stats":a.stats(),
                            "activity":period,"initial_equity":a.config["risk"]["initial_equity"],"realized_pnl":sum(t["net_pnl"] for t in a.closed),"by_symbol":by_symbol,"by_setup":by_setup,
                            "sample_note":"Наблюдений пока недостаточно для вывода" if len(a.closed)<30 else "Сравнение описывает прошлые сделки; преимущество не доказано"})
        if group==engine.generation and a.name=="baseline":
            for trade in a.closed:
                c=store.candidate(trade["candidate_id"])
                if not c:continue
                for k,ch in c["checks"].items():
                    bucket=attribution[k]["missing" if ch["passed"] is None else "pass" if ch["passed"] else "fail"]
                    bucket["n"]+=1;bucket["sum_r"]+=trade["net_r"]
    for v in attribution.values():
        for bucket in v.values():bucket["mean_r"]=bucket.pop("sum_r")/bucket["n"] if bucket["n"] else None
    coins=[]
    for s in engine.config["symbols"]:
        m=engine.market.get(s,{})
        checked=engine.last_analysis.get(s)
        coins.append({"symbol":s,"selected":True,"last_analysis":checked,"status":("Нет свежих данных" if engine.status=="DATA_ERROR" else "Ещё не проверена" if not checked else "Найдена ситуация" if m.get("status")=="CANDIDATES" else "Ожидает условий входа"),
                      "market":m,"history":cache.get(s,{"candles":0,"last_candle":None}),"activity":totals.get(s,{"candidates":0,"last_candidate":None})})
    logs=store.logs();last_error=next((x for x in logs if x["level"]=="ERROR"),None)
    return {"status_label":STATUS.get(engine.status,engine.status),"last_error":last_error,"coins":coins,"comparisons":comparisons,"filter_attribution":attribution,
            "attribution_note":"Сравниваются закрытые сделки базового счёта текущей серии. Это группировка наблюдений, а не доказательство пользы фильтра.",
            "fresh":bool(engine.updated_at and int(__import__('time').time()*1000)-engine.updated_at<engine.config['scanner']['max_data_age_seconds']*1000)}
