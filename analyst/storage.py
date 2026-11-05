from __future__ import annotations
import json
import sqlite3
import threading
import time
from pathlib import Path
import pandas as pd
from .config import validate, version

def dumps(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",", ":"))

class Store:
    def __init__(self,path,market_store=None):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.market_store=market_store
        self.lock=threading.RLock();self.db=sqlite3.connect(str(path),check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL");self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS configs(version TEXT PRIMARY KEY, created INTEGER, json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, symbol TEXT, ts INTEGER, json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS decisions(account TEXT, candidate_id TEXT, accepted INTEGER, reasons TEXT,
            PRIMARY KEY(account,candidate_id));
          CREATE TABLE IF NOT EXISTS candles(symbol TEXT,ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,
            PRIMARY KEY(symbol,ts));
          CREATE TABLE IF NOT EXISTS snapshots(symbol TEXT,ts INTEGER,json TEXT,PRIMARY KEY(symbol,ts));
          CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,created INTEGER,json TEXT,sent INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,level TEXT,message TEXT);
        ''');self.db.commit()

    def get(self,key,default=None):
        with self.lock:
            r=self.db.execute("SELECT value FROM kv WHERE key=?",(key,)).fetchone()
            return json.loads(r[0]) if r else default

    def set(self,key,value):
        with self.lock,self.db:self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)",(key,dumps(value)))

    def save_config(self,c):
        c=validate(c);v=version(c)
        with self.lock,self.db:
            self.db.execute("INSERT OR IGNORE INTO configs VALUES(?,?,?)",(v,int(time.time()*1000),dumps(c)))
            self.db.execute("INSERT OR REPLACE INTO kv VALUES('config',?)",(dumps(c),))
        return v

    def save_config_and_state(self,c,state):
        c=validate(c);v=version(c)
        with self.lock,self.db:
            self.db.execute("INSERT OR IGNORE INTO configs VALUES(?,?,?)",(v,int(time.time()*1000),dumps(c)))
            self.db.execute("INSERT OR REPLACE INTO kv VALUES('config',?)",(dumps(c),))
            self.db.execute("INSERT OR REPLACE INTO kv VALUES('engine',?)",(dumps(state),))
        return v

    def commit_cycle(self,state,candidates,decisions,alerts):
        # State, candidates, decisions, and durable notification outbox are atomic.
        with self.lock,self.db:
            for c in candidates:self.db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?)",(c.id,c.symbol,c.ts,dumps(c.to_dict())))
            for account,cid,ok,reasons in decisions:self.db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?,?,?)",(account,cid,int(ok),dumps(reasons)))
            for a in alerts:self.db.execute("INSERT INTO alerts(created,json) VALUES(?,?)",(int(time.time()*1000),dumps(a)))
            self.db.execute("INSERT OR REPLACE INTO kv VALUES('engine',?)",(dumps(state),))

    def candles(self,symbol):
        if self.market_store is not None:return self.market_store.candles(symbol)
        with self.lock:return pd.read_sql_query("SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? ORDER BY ts",self.db,params=(symbol,))

    def add_candles(self,symbol,df):
        records=[(symbol,int(r.ts),float(r.open),float(r.high),float(r.low),float(r.close),float(r.volume)) for r in df.itertuples()]
        with self.lock,self.db:self.db.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?)",records)

    def snapshot(self,symbol,snap):
        with self.lock,self.db:self.db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?)",(symbol,snap["ts"],dumps(snap)))

    def recent_candidates(self,limit=50):
        with self.lock:
            rows=self.db.execute("SELECT json FROM candidates ORDER BY ts DESC LIMIT ?",(limit,)).fetchall()
            result=[]
            for row in rows:
                c=json.loads(row[0]);c["decisions"]=[{"account":a,"accepted":bool(b),"reasons":json.loads(r)} for a,b,r in self.db.execute("SELECT account,accepted,reasons FROM decisions WHERE candidate_id=?",(c["id"],))]
                result.append(c)
            return result

    def candidate(self,cid):
        with self.lock:
            row=self.db.execute("SELECT json FROM candidates WHERE id=?",(cid,)).fetchone()
            return json.loads(row[0]) if row else None

    def log(self,level,message):
        with self.lock,self.db:self.db.execute("INSERT INTO log(ts,level,message) VALUES(?,?,?)",(int(time.time()*1000),level,str(message)[:1500]))

    def logs(self):
        with self.lock:return [{"ts":t,"level":l,"message":m} for t,l,m in self.db.execute("SELECT ts,level,message FROM log ORDER BY id DESC LIMIT 30")]

    def alerts(self):
        with self.lock:return [(i,json.loads(j)) for i,j in self.db.execute("SELECT id,json FROM alerts WHERE sent=0 ORDER BY id LIMIT 20")]

    def ack_alert(self,i):
        with self.lock,self.db:self.db.execute("UPDATE alerts SET sent=1 WHERE id=?",(i,))
