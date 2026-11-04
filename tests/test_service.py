import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
from analyst.storage import Store
from analyst.engine import Engine
from analyst.web import make_server
from analyst.telegram import Telegram
from analyst.config import DEFAULT
from analyst.models import MINUTE
from test_core import candidate,config,bar,META

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/"test.sqlite")
        self.engine=Engine(self.store,config(),demo=True)
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def test_new_config_separates_accounts_cancels_pending_keeps_positions(self):
        old=self.engine.generation;account=self.engine.accounts[old+":active"]
        account.propose(candidate());account.step(2*MINUTE,{"BTCUSDT":bar()},META)
        account.propose(candidate(cid="next",symbol="ETHUSDT",ts=3*MINUTE))
        original=next(iter(account.positions.values())).rules.copy()
        c=copy.deepcopy(self.engine.config);c["filters"]["rsi"]="on";c["risk"]["target_r"]=3
        self.engine.update_config(c)
        self.assertNotEqual(old,self.engine.generation);self.assertFalse(account.pending)
        self.assertEqual(next(iter(account.positions.values())).rules,original)
        reloaded=Engine(self.store,demo=True)
        self.assertIn(old+":active",reloaded.accounts);self.assertEqual(len(reloaded.accounts[old+":active"].positions),1)
    def test_cannot_remove_open_symbol(self):
        account=self.engine.accounts[self.engine.generation+":active"]
        account.propose(candidate());c=copy.deepcopy(self.engine.config);c["symbols"]=["SOLUSDT"]
        with self.assertRaises(ValueError):self.engine.update_config(c)
    def test_returning_to_same_config_creates_distinct_series(self):
        original=copy.deepcopy(self.engine.config);first=self.engine.generation
        changed=copy.deepcopy(original);changed["filters"]["rsi"]="on";self.engine.update_config(changed)
        self.engine.update_config(original)
        self.assertNotEqual(self.engine.generation,first)
        self.assertIn(first+":active",self.engine.accounts)
    def test_full_scanner_cycle_with_recorded_public_data(self):
        from analyst.data import make_demo,load_dataset
        data=Path(self.temp.name)/"data";make_demo(data,16);manifest,candles,_=load_dataset(data)
        end=manifest["end_ms"];now=end+5000
        class PublicFixture:
            def server_time(self):return now
            def candles(self,symbol,start,finish):return candles[symbol][(candles[symbol].ts>=start)&(candles[symbol].ts<=finish)].copy()
            def instrument(self,symbol):return META[symbol]
            def snapshot(self,symbol):return {"ts":now,"spread_bps":2.,"depth_usdt":1e6,"oi_change_pct":.1,"funding_rate":.0001,"next_funding_ts":end+480*MINUTE}
            def funding(self,*args):return []
        self.engine.api=PublicFixture()
        with patch("analyst.engine.time.time",return_value=now/1000):
            self.engine.cycle();count=len(self.store.recent_candidates(1000));state=self.store.get("engine")
            self.engine.cycle()
        self.assertEqual(self.engine.status,"RUNNING");self.assertEqual(self.engine.last_ts,end-MINUTE)
        self.assertEqual(len(self.store.recent_candidates(1000)),count)
        self.assertEqual(state["last_analysis"],self.store.get("engine")["last_analysis"])
    def test_external_binding_requires_password(self):
        with patch.dict(os.environ,{"DASHBOARD_PASSWORD":""}):
            with self.assertRaises(ValueError):make_server(self.engine,"0.0.0.0",0)
    def test_dashboard_auth_csrf_and_config_validation(self):
        with patch.dict(os.environ,{"DASHBOARD_PASSWORD":"a-test-password-123456","PUBLIC_ORIGIN":""}):
            server=make_server(self.engine,"127.0.0.1",0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f"http://127.0.0.1:{server.server_port}"
        def request(path,data=None,headers=None):
            return urllib.request.urlopen(urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers=headers or {}),timeout=5)
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:request("/api/state")
            self.assertEqual(caught.exception.code,401)
            self.assertIn(b"login",request("/").read())
            with self.assertRaises(urllib.error.HTTPError):request("/api/login",{"password":"a-test-password-123456"})
            response=request("/api/login",{"password":"a-test-password-123456"},{"X-Requested-With":"Analyst"})
            cookie=response.headers["Set-Cookie"].split(";")[0]
            headers={"Cookie":cookie,"X-Requested-With":"Analyst"}
            self.assertEqual(json.loads(request("/api/state",headers=headers).read())["mode"],"SYNTHETIC_DEMO")
            with self.assertRaises(urllib.error.HTTPError):request("/api/config",{"mode":"live"},headers)
            with self.assertRaises(urllib.error.HTTPError):request("/api/pause",{},dict(headers,Origin="https://evil.example"))
            with self.assertRaises(urllib.error.HTTPError):request("/api/state",headers=dict(headers,Host="evil.example"))
            c=copy.deepcopy(self.engine.config);c["filters"]["ema"]="on"
            self.assertIn("version",json.loads(request("/api/config",c,headers).read()))
            self.assertIn("Сторона" if False else "Монета",request("/api/export",headers=headers).read().decode())
        finally:server.shutdown();server.server_close();thread.join()
    def test_owner_only_telegram(self):
        with patch.dict(os.environ,{"TELEGRAM_BOT_TOKEN":"test:token","TELEGRAM_CHAT_ID":"123"}):t=Telegram(self.engine)
        calls=[];t.call=lambda method,data,photo=None:calls.append((method,data))
        t.handle({"message":{"chat":{"id":999,"type":"private"},"from":{"id":999},"text":"/pause"}})
        self.assertFalse(self.engine.config["paused"]);self.assertFalse(calls)
        t.handle({"message":{"chat":{"id":123,"type":"private"},"from":{"id":123},"text":"/pause"}})
        self.assertTrue(self.engine.config["paused"]);self.assertTrue(calls)
    def test_entry_notification(self):
        with patch.dict(os.environ,{"TELEGRAM_BOT_TOKEN":"test:token","TELEGRAM_CHAT_ID":"123"}):t=Telegram(self.engine)
        calls=[];t.call=lambda method,data,photo=None:calls.append(data)
        a=self.engine.accounts[self.engine.generation+":active"];a.propose(candidate())
        notes=a.step(2*MINUTE,{"BTCUSDT":bar()},META)
        self.assertEqual(notes[0]["reason"],"ENTRY")
        t.notify({"kind":"execution","event":notes[0]});self.assertIn("УЧЕБНЫЙ ВХОД",calls[0]["text"])

if __name__=="__main__":unittest.main()
