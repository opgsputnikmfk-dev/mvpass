from __future__ import annotations
import csv
import hmac
import io
import json
import os
import secrets
import time
import urllib.parse
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from .config import FILTERS,SETUPS
from .charts import price_chart
from .auth import NamedAuth,load_users

def make_server(engine,host="127.0.0.1",port=8765,profiles=None,auth_records=None):
    admin_engine=engine
    records=load_users(os.getenv("DASHBOARD_USERS_JSON","")) if auth_records is None else auth_records
    auth=NamedAuth(records) if records else None
    profiles=profiles or {"Admin":engine}
    if auth and set(records)!=set(profiles):raise ValueError("Accounts and profiles must match")
    password=os.getenv("DASHBOARD_PASSWORD","")
    if host not in ("127.0.0.1","localhost","::1") and not auth and len(password)<16:
        raise ValueError("Для внешнего доступа задайте DASHBOARD_PASSWORD длиной ≥16 символов")
    origin=os.getenv("PUBLIC_ORIGIN","").rstrip("/")
    allowed_hosts={"localhost","127.0.0.1","::1",host}
    if origin:allowed_hosts.add(urllib.parse.urlsplit(origin).hostname)
    sessions={};failures={};static=Path(__file__).parent/"static"
    class Handler(BaseHTTPRequestHandler):
        server_version="AI-Crypto-Analyst"
        def log_message(self,*args):pass
        def respond(self,status,body,ctype="application/json; charset=utf-8",cookie=None):
            if not isinstance(body,bytes):body=body.encode()
            self.send_response(status);self.send_header("Content-Type",ctype)
            self.send_header("Content-Length",str(len(body)));self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff");self.send_header("Referrer-Policy","no-referrer")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:self.send_header("Set-Cookie",cookie)
            self.end_headers();self.wfile.write(body)
        def json(self,status,obj):self.respond(status,json.dumps(obj,ensure_ascii=False,allow_nan=False))
        def valid_host(self):
            try:return urllib.parse.urlsplit("http://"+self.headers.get("Host","")).hostname in allowed_hosts
            except ValueError:return False
        def token(self):
            cookies=dict(p.strip().split("=",1) for p in self.headers.get("Cookie","").split(";") if "=" in p)
            return cookies.get("analyst_session","")
        def identity(self):
            if not auth and not password:return "Admin"
            session=sessions.get(self.token())
            return session[0] if session and session[1]>time.time() else None
        def authenticated(self):return self.identity() is not None
        def do_GET(self):
            if not self.valid_host():return self.json(403,{"error":"Invalid host"})
            u=urllib.parse.urlsplit(self.path);path=u.path;q=urllib.parse.parse_qs(u.query)
            engine=admin_engine
            if path in ("/health", "/healthz"):return self.json(200,{"process":"ok","version":"5.2.0","scanner":engine.status,"updated_at":engine.updated_at,"mode":"paper","selected_symbols":len(engine.config["symbols"]),"universe_verified":engine.universe.get("verified",False),"data_source":engine.config["market"],"analyzed_symbols":len(engine.last_analysis)})
            if path in ("/style.css","/app.js","/logo.svg"):
                return self.respond(200,(static/path[1:]).read_bytes(),"image/svg+xml" if path.endswith("svg") else "text/css" if path.endswith("css") else "text/javascript")
            if not self.authenticated():
                if path=="/":return self.respond(200,(static/"login.html").read_bytes(),"text/html; charset=utf-8")
                return self.json(401,{"error":"Войдите в дашборд"})
            username=self.identity();engine=profiles[username]
            try:
                if path=="/":return self.respond(200,(static/"index.html").read_bytes(),"text/html; charset=utf-8")
                if path=="/api/state":return self.json(200,{**engine.state(),"filter_names":FILTERS,"setup_names":SETUPS,"user":{"username":username,"role":records[username]["role"] if auth else "admin","telegram_linked":username=="Admin"}})
                if path in ("/api/position", "/api/position-chart"):
                    from .valuation import position_view
                    key=q.get("account",[""])[0];pid=q.get("id",[""])[0]
                    with engine.lock:
                        account=engine.accounts.get(key)
                        position=account.positions.get(pid) if account else None
                        if position is None:return self.json(404,{"error":"Открытая сделка не найдена в вашем аккаунте"})
                        p=position_view(position,account,engine.live_prices,int(time.time()*1000))
                        candidate=engine.store.candidate(position.candidate_id)
                    if path=="/api/position":return self.json(200,{"position":p,"candidate":candidate,"account":key})
                    chart={**(candidate or {}),"symbol":p["symbol"],"side":p["side"],"setup":p["setup"],"reference":p["entry"],"stop":p["stop"],"target":p["target"],"levels":(candidate or {}).get("levels",{}),"entry_ts":p["entry_ts"],"current_price":p["current_price"],"live_position":True}
                    return self.respond(200,price_chart(engine.store.candles(p["symbol"]),chart,engine.demo,source="Binance Spot reference" if engine.config["exchange"]=="binance" else "Bybit perpetual"),"image/png")
                if path=="/api/chart":
                    cid=q.get("candidate",[None])[0];c=engine.store.candidate(cid) if cid else None
                    if cid and not c:return self.json(404,{"error":"Сигнал не найден"})
                    symbol=c["symbol"] if c else q.get("symbol",[engine.config["symbols"][0]])[0]
                    if symbol not in engine.config["symbols"]:return self.json(404,{"error":"Символ не найден"})
                    df=engine.store.candles(symbol)
                    return self.respond(200,price_chart(df,c,engine.demo,source="Binance Spot reference" if engine.config["exchange"]=="binance" else "Bybit perpetual"),"image/png")
                if path=="/api/export":
                    key=q.get("account",[engine.generation+":active"])[0]
                    with engine.lock:
                        a=engine.accounts.get(key)
                        if a is None:return self.json(404,{"error":"Серия не найдена"})
                        trades=list(a.closed)
                    out=io.StringIO();writer=csv.writer(out)
                    writer.writerow(["Монета","Направление","Вход UTC","Выход UTC","Цена входа","Стоп","Цель","Итог USDT","Результат R","Комиссии","Funding","Причина","Версия"])
                    labels={"SL":"Стоп","SL_GAP":"Стоп с разрывом","TP":"Цель","TIMEOUT":"Лимит времени","END_OF_TEST":"Конец теста"}
                    for t in trades:
                        stamp=lambda ts:time.strftime("%Y-%m-%d %H:%M:%S",time.gmtime(ts/1000))
                        writer.writerow([t["symbol"],"LONG" if t["side"]==1 else "SHORT",stamp(t["entry_ts"]),stamp(t["exit_ts"]),f'{t["entry"]:.8g}',f'{t["initial_stop"]:.8g}',f'{t["target"]:.8g}',round(t["net_pnl"],2),round(t["net_r"],3),round(t["fees"],4),round(t["funding_paid"],4),labels.get(t["reason"],t["reason"]),t["config_version"]])
                    return self.respond(200,("\ufeff"+out.getvalue()).encode(),"text/csv; charset=utf-8")
                return self.json(404,{"error":"Not found"})
            except ValueError as e:return self.json(400,{"error":str(e)})
            except Exception:
                engine.store.log("ERROR","Ошибка построения ответа дашборда")
                return self.json(500,{"error":"Не удалось сформировать ответ; подробности в журнале"})
        def do_POST(self):
            if not self.valid_host():return self.json(403,{"error":"Invalid host"})
            if self.headers.get("X-Requested-With")!="Analyst":return self.json(403,{"error":"CSRF check"})
            incoming=self.headers.get("Origin")
            expected=origin or "http://"+self.headers.get("Host","")
            if incoming and incoming!=expected:return self.json(403,{"error":"Origin check"})
            try:
                n=int(self.headers.get("Content-Length",0))
                if not 0<n<=100000:return self.json(413,{"error":"Body too large"})
                data=json.loads(self.rfile.read(n))
            except (ValueError,TypeError):return self.json(400,{"error":"Invalid JSON"})
            if not isinstance(data,dict):return self.json(400,{"error":"Ожидался JSON объект"})
            if self.path=="/api/login":
                ip=self.client_address[0];recent=[t for t in failures.get(ip,[]) if t>time.time()-60]
                if len(recent)>=5:return self.json(429,{"error":"Повторите через минуту"})
                username=auth.authenticate(data.get("username"),data.get("password")) if auth else ("Admin" if isinstance(data.get("password"),str) and hmac.compare_digest(data["password"].encode(),password.encode()) else None)
                if username is None:
                    failures[ip]=recent+[time.time()];return self.json(401,{"error":"Неверный логин или пароль"})
                sessions.pop(self.token(),None)
                token=secrets.token_urlsafe(32);sessions[token]=(username,time.time()+8*3600)
                for key,session in list(sessions.items()):
                    if session[1]<time.time():sessions.pop(key,None)
                cookie=f"analyst_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"+("; Secure" if origin.startswith("https://") else "")
                return self.respond(200,'{"ok":true}',cookie=cookie)
            if not self.authenticated():return self.json(401,{"error":"Требуется вход"})
            username=self.identity();engine=profiles[username]
            if self.path=="/api/logout":
                sessions.pop(self.token(),None)
                return self.respond(200,'{"ok":true}',cookie="analyst_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            if auth and self.headers.get("X-Account")!=username:return self.json(409,{"error":"Аккаунт изменился. Обновите страницу перед сохранением."})
            try:
                if self.path=="/api/config":return self.json(200,{"version":engine.update_config(data)})
                if self.path=="/api/pause":
                    c=engine.state()["config"];c["paused"]=not c["paused"];return self.json(200,{"version":engine.update_config(c)})
                return self.json(404,{"error":"Not found"})
            except (ValueError,TypeError,KeyError) as e:return self.json(400,{"error":str(e)})
    return ThreadingHTTPServer((host,port),Handler)
