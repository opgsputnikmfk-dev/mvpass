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

    def menu(self,page=0,message_id=None):
        state=self.engine.state();cfg=state["config"];v=hashlib.sha256(state["generation"].encode()).hexdigest()[:8]
        a=state["accounts"].get(state["generation"]+":active",{}).get("stats",{})
        rows=[];items=list(FILTERS.items());page=max(0,min(4,page))
        modes={"on":"🟢","observe":"🟡","off":"⚫"}
        for key,label in items[page*5:page*5+5]:
            rows.append([{"text":f"{modes[cfg['filters'][key]]} {label}","callback_data":f"{v}:f:{key}:{page}"}])
        rows.append([{"text":"←","callback_data":f"{v}:page:{max(0,page-1)}"},{"text":f"{page+1}/5 · обновить","callback_data":f"{v}:page:{page}"},{"text":"→","callback_data":f"{v}:page:{min(4,page+1)}"}])
        rows.append([{"text":"Продолжить" if cfg["paused"] else "Пауза","callback_data":f"{v}:pause"},{"text":"Статистика","callback_data":f"{v}:stats"}])
        text=f"<b>AI Crypto · Analyst 4.0</b>\n{'ДЕМО / искусственные данные' if self.engine.demo else 'УЧЕБНЫЕ СДЕЛКИ · Bybit USDT'}\nСостояние: {html.escape(state['status'])}\nСерия: <code>{v}</code>\n\nКапитал: {a.get('equity',0):,.2f} USDT\nЗакрыто: {a.get('trades',0)} · результат: {a.get('return_pct',0):+.2f}%\n\n<b>Фильтры</b>\n🟢 включён · 🟡 наблюдение · ⚫ выключен\nНажатие меняет режим и создаёт новую серию.\n\n/chart BTCUSDT — график\n/stats — сравнение\n/setups — сценарии\n/help — команды"
        payload={"chat_id":self.chat_id,"text":text,"parse_mode":"HTML","reply_markup":{"inline_keyboard":rows}}
        if message_id:payload["message_id"]=message_id
        return self.call("editMessageText" if message_id else "sendMessage",payload)

    def send(self,text,**kwargs):return self.call("sendMessage",{"chat_id":self.chat_id,"text":text,"parse_mode":"HTML",**kwargs})

    def stats(self):
        state=self.engine.state();lines=["<b>Параллельные учебные счета</b>"]
        for key,a in state["accounts"].items():
            if key.startswith(state["generation"]):
                s=a["stats"];lines.append(f"\n{html.escape(s['name'])}: {s['trades']} сделок\nРезультат {s['return_pct']:+.2f}% · просадка {s['max_drawdown_pct']:.2f}%")
        lines.append("\nЭто модель исполнения. Демо-результаты не оценивают прибыльность рынка.")
        self.send("\n".join(lines))

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
            elif bits[1]=="pause":c["paused"]=not c["paused"];self.engine.update_config(c)
            elif bits[1]=="stats":self.stats();return
            elif bits[1]=="page" and len(bits)==3:page=int(bits[2])
            return self.menu(page,m["message_id"])
        if m.get("from",{}).get("id")!=self.chat_id:return
        words=m.get("text","").strip().split()
        if not words:return
        command=words[0].split("@")[0]
        if command in ("/start","/menu","/filters"):self.menu()
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
            image=price_chart(self.engine.store.candles(symbol),c,self.engine.demo)
            self.call("sendPhoto",{"chat_id":self.chat_id,"caption":symbol+" · PAPER · закрытые свечи"},photo=image)
        else:self.send("<b>Команды</b>\n/start — меню\n/filters — переключатели\n/filter ema observe — режим фильтра\n/setups — сценарии\n/setup breakout on — включить сценарий\n/pause · /resume\n/stats — сравнение\n/chart BTCUSDT — график\n\nПараметры и CSV доступны в веб-дашборде.")

    def notify(self,alert):
        if alert["kind"]=="candidate":
            c=alert["candidate"]
            buttons=[{"text":label,"copy_text":{"text":f"{c[key]:.10g}"}} for key,label in [("reference","Вход"),("stop","Стоп"),("target","Цель")]]
            self.send(f"<b>СЦЕНАРИЙ · PAPER</b>\n{html.escape(c['symbol'])} · {'LONG' if c['side']==1 else 'SHORT'}\n{html.escape(SETUPS[c['setup']])}\n\nПредварительный вход: <code>{c['reference']:.8g}</code>\nСтоп: <code>{c['stop']:.8g}</code>\nЦель: <code>{c['target']:.8g}</code>\n\n{html.escape(c['explanation'])}\nУчебный вход после задержки; может отмениться.\nID: <code>{c['id'][:10]}</code>",reply_markup={"inline_keyboard":[buttons]})
            try:self.call("sendPhoto",{"chat_id":self.chat_id,"caption":"Уровни на момент анализа · PAPER"},photo=price_chart(self.engine.store.candles(c["symbol"]),c,self.engine.demo))
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
