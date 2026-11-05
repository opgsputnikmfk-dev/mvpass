#!/usr/bin/env python3
"""Entrypoint; run `python bot.py --help`. See README_RU.md before use."""
from __future__ import annotations
import argparse
import copy
import json
import os
from pathlib import Path
import signal
import threading
import sys
from datetime import datetime,timezone

def load_env():
    path=Path(__file__).parent/".env"
    if not path.exists():return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line=line.strip()
        if not line or line.startswith("#"):continue
        if "=" not in line:raise ValueError("Строка .env должна иметь вид KEY=value")
        k,v=line.split("=",1)
        if k.strip() not in ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","DASHBOARD_PASSWORD","DASHBOARD_USERS_JSON","PUBLIC_ORIGIN","PORT","DATA_DIR"):continue
        os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))

def utc(s):
    d=datetime.fromisoformat(s.replace("Z","+00:00"))
    if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
    return int(d.timestamp()*1000)

def main():
    load_env()
    # Preserve the existing Render start command and Telegram environment.
    if len(sys.argv) == 1 and os.getenv("RENDER"):
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
        admins = os.getenv("ADMIN_CHAT_IDS", "8299008675").split(",")
        os.environ.setdefault("TELEGRAM_CHAT_ID", admins[0].strip())
        os.environ.setdefault("PUBLIC_ORIGIN", os.getenv("RENDER_EXTERNAL_URL", "https://mvpass.onrender.com"))
        os.environ.setdefault("DATA_DIR", "data_analyst_v4")
        sys.argv.extend(["serve", "--host", "0.0.0.0"])
        if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            sys.argv.append("--telegram")
    parser=argparse.ArgumentParser(description="AI Crypto Analyst 4.3 — учебные сделки и исследование фильтров")
    sub=parser.add_subparsers(dest="command",required=True)
    for name in ("serve","demo"):
        p=sub.add_parser(name);p.add_argument("--config");p.add_argument("--host",default="127.0.0.1");p.add_argument("--port",type=int,default=int(os.getenv("PORT",8765)));p.add_argument("--data-dir",default=os.getenv("DATA_DIR",".data"));p.add_argument("--telegram",action="store_true");p.add_argument("--source",choices=["bybit","binance_spot"],default=None)
    p=sub.add_parser("download");p.add_argument("--out",required=True);p.add_argument("--start",required=True);p.add_argument("--end",required=True);p.add_argument("--symbols",default="BTCUSDT,ETHUSDT,SOLUSDT")
    p=sub.add_parser("make-demo");p.add_argument("--out",required=True);p.add_argument("--days",type=int,default=20)
    p=sub.add_parser("backtest");p.add_argument("--data",required=True);p.add_argument("--config");p.add_argument("--start",required=True);p.add_argument("--end",required=True);p.add_argument("--out",required=True);p.add_argument("--all-filters",action="store_true")
    p=sub.add_parser("lab-init");p.add_argument("--data",required=True);p.add_argument("--config");p.add_argument("--out",required=True)
    p=sub.add_parser("lab-run");p.add_argument("--data",required=True);p.add_argument("--out",required=True);p.add_argument("--stage",choices=["develop","select","holdout"],required=True)
    sub.add_parser("doctor")
    args=parser.parse_args()
    from analyst.config import read_config
    from analyst.data import download_dataset,make_demo,Bybit
    from analyst.research import replay,save_report,experiment_configs,register,run_stage
    cfg=read_config(getattr(args,"config",None))
    if args.command=="demo" and args.config is None:cfg["symbols"]=["BTCUSDT","ETHUSDT","SOLUSDT"]
    if args.command=="download":download_dataset(args.out,args.symbols.split(","),utc(args.start),utc(args.end));return
    if args.command=="make-demo":make_demo(args.out,args.days);print("Synthetic dataset created; not market history.");return
    if args.command=="doctor":
        print("Python:",sys.version.split()[0]);print("Mode: paper only")
        try:print("Binance Spot server UTC:",datetime.fromtimestamp(__import__("analyst.spot",fromlist=["BinanceSpot"]).BinanceSpot().server_time()/1000,timezone.utc))
        except Exception as e:print("Binance Spot unavailable:",str(e));return 2
        return
    if args.command=="backtest":
        cs=experiment_configs(cfg) if args.all_filters else {"active":cfg}
        result=replay(args.data,cs,utc(args.start),utc(args.end),progress=print);save_report(result,args.out)
        print(json.dumps(result["summaries"],ensure_ascii=False,indent=2));return
    if args.command=="lab-init":register(args.data,cfg,args.out);print("Protocol registered. Next: lab-run --stage develop");return
    if args.command=="lab-run":run_stage(args.data,args.out,args.stage);print("Stage complete; report saved.");return
    from analyst.storage import Store
    from analyst.engine import Engine,variants
    from analyst.web import make_server
    demo=args.command=="demo";root=Path(args.data_dir);root.mkdir(parents=True,exist_ok=True)
    if not demo:
        source=args.source or ("binance_spot" if args.config is None else "binance_spot" if cfg["exchange"]=="binance" else "bybit")
        if source=="binance_spot":
            if args.config is None and (root/"paper.sqlite").exists():
                # Carry over settings only; never import futures candles/trades.
                import sqlite3
                with sqlite3.connect("file:"+str((root/"paper.sqlite").resolve())+"?mode=ro",uri=True) as previous:
                    row=previous.execute("SELECT value FROM kv WHERE key='config'").fetchone()
                    if row:cfg=json.loads(row[0])
            cfg["exchange"]="binance";cfg["market"]="spot_reference"
            # These features do not exist for spot; explicitly observe as N/A.
            cfg["filters"]["oi"]="observe";cfg["filters"]["funding"]="observe"
            root=root/"binance_spot_v43";root.mkdir(parents=True,exist_ok=True)
    store=Store(root/("demo.sqlite" if demo else "paper.sqlite"))
    if demo and not store.get("engine"):
        data=root/"synthetic";manifest=make_demo(data,20)
        print("Preparing synthetic demo. No market performance claims.",flush=True)
        store.save_config(cfg)
        result=replay(data,variants(cfg),manifest["start_ms"]+17*86400000,manifest["end_ms"],store=store,progress=lambda s:print(s,flush=True))
        save_report(result,root/"demo_report")
    engine=Engine(store,cfg,demo)
    if not demo and args.config is None:engine.enable_top20()
    from analyst.auth import load_users
    from analyst.profiles import EngineProfiles
    records=load_users(os.getenv("DASHBOARD_USERS_JSON",""))
    profiles={"Admin":engine}
    for username in records:
        if username=="Admin":continue
        userroot=root/"users"/username.lower();userroot.mkdir(parents=True,exist_ok=True)
        profiles[username]=Engine(Store(userroot/"paper.sqlite",market_store=store),copy.deepcopy(engine.config),demo)
    runner=EngineProfiles(profiles)
    server=make_server(engine,args.host,args.port,profiles=profiles,auth_records=records)
    threads=[]
    if not demo:threads.append(threading.Thread(target=runner.run,daemon=True,name="scanner"))
    if not demo:threads.append(threading.Thread(target=runner.quote_loop,daemon=True,name="position-quotes"))
    if args.telegram:
        from analyst.telegram import Telegram
        telegram=Telegram(engine);threads.append(threading.Thread(target=telegram.run,daemon=True,name="telegram"))
    for t in threads:t.start()
    def stop(*_):
        engine.stop.set();threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(f"AI Crypto Analyst: http://{args.host}:{server.server_port} · {'SYNTHETIC DEMO' if demo else 'PAPER'}",flush=True)
    try:server.serve_forever(poll_interval=.5)
    finally:
        engine.stop.set();server.server_close()
        for t in threads:t.join(timeout=2)

if __name__=="__main__":
    try:sys.exit(main())
    except (ValueError,FileNotFoundError,FileExistsError) as e:print(f"Ошибка: {e}",file=sys.stderr);sys.exit(2)
