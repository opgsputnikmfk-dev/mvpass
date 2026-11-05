"""Owner-only Telegram UI; public market data only, no exchange credentials."""
from __future__ import annotations
import html
import json
import os
import time
import urllib.request
import urllib.error
import uuid
import hashlib
from .config import FILTERS,SETUPS
from .charts import price_chart

class Telegram:
    def __init__(self,engine):
        self.engine=engine;self.token=os.getenv("TELEGRAM_BOT_TOKEN","")
        chat=os.getenv("TELEGRAM_CHAT_ID","")
        if not self.token or not chat:raise ValueError("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        self.chat_id=int(chat)
        if self.chat_id<=0:raise ValueError("Используйте личный чат владельца, не группу")
        self.offset=engine.store.get("telegram_offset",0)

    def call(self,method,data,photo=None):
        if photo is None:
            body=json.dumps(data).encode();ctype="application/json"
        else:
            boundary="Analyst"+uuid.uuid4().hex;parts=[]
            for k,v in data.items():parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="analysis.png"\r\nContent-Type: image/png\r\n\r\n'.encode(),photo,f'\r\n--{boundary}--\r\n'.encode()])
            body=b"".join(parts);ctype="multipart/form-data; boundary="+boundary
        req=urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",data=body,headers={"Content-Type":ctype})
        try:
            with urllib.request.urlopen(req,timeout=40) as r:result=json.loads(r.read())
        except urllib.error.HTTPError as e:
            # Never log request URLs: bot tokens are embedded in those URLs.
            detail=e.read(2000).decode(errors="replace")
            if "message is not modified" in detail:return None
            raise RuntimeError(f"Telegram HTTP {e.code}") from None
        except Exception:raise RuntimeError("Telegram connection failed") from None
        if not result.get("ok"):raise RuntimeError("Telegram API error")
        return result.get("result")

    def menu(self,page=-1,message_id=None):
        state=self.engine.state();cfg=state["config"];v=hashlib.sha256(state["generation"].encode()).hexdigest()[:8]
        a=state["accounts"].get(state["generation"]+":active",{}).get("stats",{})
        if page == -1:
            rows=[[{"text":"📊 Статистика наборов фильтров","callback_data":f"{v}:stats"}],
                  [{"text":"🪙 Просматриваемые монеты","callback_data":f"{v}:coins"},{"text":"🎛 Все фильтры","callback_data":f"{v}:page:0"}],
                  [{"text":"▶ Продолжить" if cfg["paused"] else "⏸ Пауза","callback_data":f"{v}:pause"},{"text":"↻ Обновить","callback_data":f"{v}:home"}]]
            d=state["dashboard"];active=state["accounts"][state["generation"]+":active"]
            enabled=sum(x=="on" for x in cfg["filters"].values())
            text=("<b>AI Crypto · Главная</b>\nУчебные сделки · реальные ордера не отправляются\n\n"
                  +html.escape(state["data_source"])+"\n"+html.escape(d["status_label"])+f"\nМонет в списке: {len(cfg['symbols'])} · фильтров включено: {enabled}\n"
                  +f"Открыто сделок: {a.get('open',0)} · ожидают входа: {a.get('pending',0)}\n"
                  +f"Закрыто: {a.get('trades',0)} · результат: {a.get('return_pct',0):+.2f}%\n"
                  +f"Наибольшее снижение капитала: {a.get('max_drawdown_pct',0):.2f}%\n\n"
                  +("⚠ Биржа не отдаёт данные. Новые сигналы не формируются.\n\n" if state["status"]=="DATA_ERROR" else "")
                  +"<b>Что можно посмотреть</b>\n📊 Состав наборов, результаты и количество сделок\n🪙 Какие монеты выбраны и проверены\n🎛 Все переключатели в одном разделе\n\n/help — как пользоваться")
            payload={"chat_id":self.chat_id,"text":text,"parse_mode":"HTML","reply_markup":{"inline_keyboard":rows}}
            if message_id:payload["message_id"]=message_id
            return self.call("editMessageText" if message_id else "sendMessage",payload)
        rows=[];items=list(FILTERS.items());page=max(0,min(4,page))
        modes={"on":"🟢","observe":"🟡","off":"⚫"}
        for key,label in items[page*5:page*5+5]:
            rows.append([{"text":f"{modes[cfg['filters'][key]]} {label}","callback_data":f"{v}:f:{key}:{page}"}])
        rows.append([{"text":"←","callback_data":f"{v}:page:{max(0,page-1)}"},{"text":f"{page+1}/5 · обновить","callback_data":f"{v}:page:{page}"},{"text":"→","callback_data":f"{v}:page:{min(4,page+1)}"}])
        rows.append([{"text":"🏠 Главная","callback_data":f"{v}:home"}])
        rows.append([{"text":"Продолжить" if cfg["paused"] else "Пауза","callback_data":f"{v}:pause"},{"text":"Статистика","callback_data":f"{v}:stats"}])
        text=f"<b>AI Crypto · Analyst 4.1</b>\n{'ДЕМО / искусственные данные' if self.engine.demo else html.escape(state['data_source'])}\nСостояние: {html.escape(state['dashboard']['status_label'])}\nСерия: <code>{v}</code>\n\nКапитал: {a.get('equity',0):,.2f} USDT\nЗакрыто: {a.get('trades',0)} · результат: {a.get('return_pct',0):+.2f}%\n\n<b>Фильтры</b>\n🟢 включён · 🟡 наблюдение · ⚫ выключен\nНажатие меняет режим и создаёт новую серию.\n\n/chart BTCUSDT — график\n/stats — сравнение\n/setups — сценарии\n/help — команды"
        payload={"chat_id":self.chat_id,"text":text,"parse_mode":"HTML","reply_markup":{"inline_keyboard":rows}}
        if message_id:payload["message_id"]=message_id
        return self.call("editMessageText" if message_id else "sendMessage",payload)

    def source_note(self):
        return "Цены Binance Spot · ориентиры LONG/SHORT; сверяйте уровни своего фьючерса. Funding/OI недоступны." if self.engine.config["market"]=="spot_reference" else "Bybit USDT perpetual · учебное исполнение."

    def send(self,text,**kwargs):
        text=text+"\n\n"+html.escape(self.source_note())
        return self.call("sendMessage",{"chat_id":self.chat_id,"text":text,"parse_mode":"HTML",**kwargs})

    def stats(self,message_id=None):
        state=self.engine.state();lines=["<b>📊 Статистика сочетаний фильтров</b>"]
        for c in state["dashboard"]["comparisons"]:
            if not c["current"]:continue
            stat=c["stats"];names=" + ".join(c["filters"]) or "Без дополнительных фильтров"
            lines.append("\n<b>"+html.escape(c["name"])+"</b>\n"+html.escape(names[:700])
                         +f"\nПроверено ситуаций: {c['activity']['checked']} · допущено: {c['activity']['accepted']}"
                         +f"\nЗакрыто сделок: {stat['trades']} · удачных: {stat['wins']}"
                         +f"\nИтог закрытых: {c['realized_pnl']:+.2f} USDT"
                         +f"\nРезультат счёта: {stat['return_pct']:+.2f}% · просадка: {stat['max_drawdown_pct']:.2f}%"
                         +"\n"+html.escape(c["sample_note"]))
        lines.append("\nНаборы текущей серии наблюдаются параллельно. Их результаты не складываются. Подробности по монетам и архив серий — в веб-дашборде.")
        return self.info_screen("\n".join(lines),message_id)

    def info_screen(self,text,message_id=None):
        text=text+"\n\n"+html.escape(self.source_note())
        v=hashlib.sha256(self.engine.generation.encode()).hexdigest()[:8]
        payload={"chat_id":self.chat_id,"text":text,"parse_mode":"HTML","reply_markup":{"inline_keyboard":[[{"text":"🏠 Главная","callback_data":f"{v}:home"}]]}}
        if message_id:payload["message_id"]=message_id
        return self.call("editMessageText" if message_id else "sendMessage",payload)

    def coins(self,message_id=None):
        state=self.engine.state();d=state["dashboard"];lines=["<b>🪙 Просматриваемые монеты</b>",html.escape(state.get("universe",{}).get("label","Список выбран вручную"))]
        for c in d["coins"]:
            stamp=time.strftime("%d.%m %H:%M UTC",time.gmtime(c["last_analysis"]/1000)) if c["last_analysis"] else "ещё не проверена"
            lines.append("\n<b>"+html.escape(c["symbol"])+"</b> · "+html.escape(c["status"])+"\nПоследний анализ: "+stamp+f"\nНайдено ситуаций: {c['activity']['candidates']}")
        return self.info_screen("\n".join(lines),message_id)

    def handle(self,u):
        cb=u.get("callback_query");m=(cb or {}).get("message") or u.get("message",{})
        if m.get("chat",{}).get("id")!=self.chat_id or m.get("chat",{}).get("type")!="private":return
        if cb:
            if cb.get("from",{}).get("id")!=self.chat_id:return
            self.call("answerCallbackQuery",{"callback_query_id":cb["id"]})
            bits=cb.get("data","").split(":")
            if len(bits)<2:return
            if bits[0]!=hashlib.sha256(self.engine.generation.encode()).hexdigest()[:8]:return self.menu(message_id=m["message_id"])
            c=self.engine.state()["config"];page=0
            if bits[1]=="f" and len(bits)==4 and bits[2] in FILTERS:
                key=bits[2];c["filters"][key]={"on":"off","off":"observe","observe":"on"}[c["filters"][key]]
                self.engine.update_config(c);page=int(bits[3])
            elif bits[1]=="pause":
                c["paused"]=not c["paused"];self.engine.update_config(c);self.menu(-1,m["message_id"]);return
            elif bits[1]=="stats":self.stats(m["message_id"]);return
            elif bits[1]=="coins":self.coins(m["message_id"]);return
            elif bits[1]=="home":self.menu(-1,m["message_id"]);return
            elif bits[1]=="page" and len(bits)==3:page=int(bits[2])
            return self.menu(page,m["message_id"])
        if m.get("from",{}).get("id")!=self.chat_id:return
        words=m.get("text","").strip().split()
        if not words:return
        command=words[0].split("@")[0]
        if command in ("/start","/menu"):self.menu()
        elif command=="/filters":self.menu(0)
        elif command=="/coins":self.coins()
        elif command=="/combinations":self.stats()
        elif command=="/stats":self.stats()
        elif command=="/setups":self.send("<b>Сценарии</b>\n"+"\n".join(f"{'🟢' if k in self.engine.config['setups'] else '⚫'} {k}: {v}" for k,v in SETUPS.items())+"\n\nВключить сценарий: <code>/setup pullback on</code>")
        elif command in ("/filter","/setup") and len(words)==3:
            c=self.engine.state()["config"];key,mode=words[1:]
            if command=="/filter" and key in FILTERS and mode in ("on","off","observe"):c["filters"][key]=mode
            elif command=="/setup" and key in SETUPS and mode in ("on","off"):
                c["setups"]=[k for k in c["setups"] if k!=key]+([key] if mode=="on" else [])
            else:return self.send("Проверьте имя и режим команды.")
            self.engine.update_config(c);self.menu()
        elif command in ("/pause","/resume"):
            c=self.engine.state()["config"];c["paused"]=command=="/pause";self.engine.update_config(c);self.menu()
        elif command=="/chart":
            symbol=words[1].upper() if len(words)>1 else self.engine.config["symbols"][0]
            if symbol not in self.engine.config["symbols"]:return self.send("Символ отсутствует в настройках.")
            c=next((c for c in self.engine.store.recent_candidates(200) if c["symbol"]==symbol),None)
            image=price_chart(self.engine.store.candles(symbol),c,self.engine.demo,source="Binance Spot reference" if self.engine.config["exchange"]=="binance" else "Bybit perpetual")
            self.call("sendPhoto",{"chat_id":self.chat_id,"caption":symbol+" · PAPER · закрытые свечи"},photo=image)
        else:self.send("<b>Команды</b>\n/start — меню\n/filters — переключатели\n/filter ema observe — режим фильтра\n/setups — сценарии\n/setup breakout on — включить сценарий\n/pause · /resume\n/stats — статистика наборов\n/combinations — сочетания фильтров\n/coins — просматриваемые монеты\n/chart BTCUSDT — график\n\nПараметры и CSV доступны в веб-дашборде.")

    def notify(self,alert):
        if alert["kind"]=="candidate":
            c=alert["candidate"]
            buttons=[{"text":label,"copy_text":{"text":f"{c[key]:.10g}"}} for key,label in [("reference","Вход"),("stop","Стоп"),("target","Цель")]]
            self.send(f"<b>СЦЕНАРИЙ · PAPER</b>\n{html.escape(c['symbol'])} · {'LONG' if c['side']==1 else 'SHORT'}\n{html.escape(SETUPS[c['setup']])}\n\nПредварительный вход: <code>{c['reference']:.8g}</code>\nСтоп: <code>{c['stop']:.8g}</code>\nЦель: <code>{c['target']:.8g}</code>\n\n{html.escape(c['explanation'])}\nУчебный вход после задержки; может отмениться.\nID: <code>{c['id'][:10]}</code>",reply_markup={"inline_keyboard":[buttons]})
            try:self.call("sendPhoto",{"chat_id":self.chat_id,"caption":"Уровни на момент анализа · "+self.source_note()},photo=price_chart(self.engine.store.candles(c["symbol"]),c,self.engine.demo,source="Binance Spot reference" if self.engine.config["exchange"]=="binance" else "Bybit perpetual"))
            except Exception:self.engine.store.log("WARNING","График Telegram не доставлен")
        else:
            e=alert["event"]
            if e["reason"]=="ENTRY":self.send(f"<b>УЧЕБНЫЙ ВХОД</b>\n{e['symbol']} · {'LONG' if e['side']==1 else 'SHORT'}\nВход {e['entry']:.8g}\nСтоп {e['stop']:.8g}\nЦель {e['target']:.8g}\nРиск {e['planned_risk']:.2f} USDT")
            else:self.send(f"<b>УЧЕБНАЯ СДЕЛКА ЗАКРЫТА</b>\n{e['symbol']} · {e['reason']}\nИтог: {e['net_pnl']:+.2f} USDT ({e['net_r']:+.2f} R)\nПосле учтённых расходов.")

    def run(self):
        while not self.engine.stop.is_set():
            try:
                for u in self.call("getUpdates",{"offset":self.offset,"timeout":15,"allowed_updates":["message","callback_query"]}) or []:
                    try:self.handle(u)
                    except (ValueError,KeyError,IndexError):self.engine.store.log("WARNING","Некорректная команда Telegram")
                    self.offset=u["update_id"]+1;self.engine.store.set("telegram_offset",self.offset)
                for i,a in self.engine.store.alerts():self.notify(a);self.engine.store.ack_alert(i)
            except Exception as e:
                self.engine.store.log("WARNING",f"Telegram: {type(e).__name__}; повтор через 5 секунд")
                self.engine.stop.wait(5)
