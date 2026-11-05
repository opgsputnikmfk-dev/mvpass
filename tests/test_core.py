import copy
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
from analyst.config import DEFAULT,FILTERS,validate,version
from analyst.indicators import aggregate,clean_candles,pivots,enrich,rma
from analyst.models import Candidate,Check,MINUTE
from analyst.execution import Ledger
from analyst.analysis import allowed,analyze,prepare
from analyst.data import make_demo,load_dataset,Bybit,DataError
from analyst.storage import Store

def config():
    c=copy.deepcopy(DEFAULT);c["symbols"]=["BTCUSDT","ETHUSDT","SOLUSDT"];c["filters"]={k:"off" for k in FILTERS};return c

def candidate(side=1,symbol="BTCUSDT",ts=MINUTE,cid="test",reference=100):
    return Candidate(cid,symbol,ts,"pullback",side,reference,reference-side*2,reference+side*4,2,
                     {k:Check(True,1,"test") for k in FILTERS},{"support":None,"resistance":None,"fibonacci":{},"pivots":[]},"test")

META={s:{"tick_size":.01,"qty_step":.001,"min_qty":.001,"min_notional":1} for s in ("BTCUSDT","ETHUSDT","SOLUSDT")}
def bar(o=100,h=100.2,l=99.8,c=100):return {"open":o,"high":h,"low":l,"close":c,"volume":10}

class ConfigTests(unittest.TestCase):
    def test_default_is_valid_and_complete(self):self.assertEqual(len(validate(DEFAULT)["filters"]),25)
    def test_nan_and_unknown_rejected(self):
        for mutate in (lambda c:c["risk"].update(risk_pct=float("nan")),lambda c:c.update(typo=1),lambda c:c["risk"].update(max_notional_equity=100)):
            c=config();mutate(c)
            with self.assertRaises(ValueError):validate(c)
    def test_live_mode_rejected(self):
        c=config();c["mode"]="live"
        with self.assertRaises(ValueError):validate(c)
    def test_versions_change_with_settings(self):
        c=config();a=version(c);c["filters"]["rsi"]="on";self.assertNotEqual(a,version(c))
    def test_observe_and_missing(self):
        c=config();x=candidate();x.checks["oi"]=Check(None,None,"missing")
        c["filters"]["oi"]="observe";self.assertTrue(allowed(x,c)[0])
        c["filters"]["oi"]="on";self.assertFalse(allowed(x,c)[0])
    def test_no_setup_no_signal(self):
        c=config();c["setups"]=[];self.assertFalse(allowed(candidate(),c)[0])

class IndicatorTests(unittest.TestCase):
    def test_partial_bars_not_exposed(self):
        d=pd.DataFrame([{"ts":i*MINUTE,**bar()} for i in range(9)])
        out=aggregate(d,5);self.assertEqual(len(out),1);self.assertEqual(out.iloc[0].close_ts,5*MINUTE)
    def test_gaps_and_duplicates_rejected(self):
        for ts in ([0,MINUTE,3*MINUTE],[0,0,MINUTE]):
            with self.assertRaises(ValueError):clean_candles(pd.DataFrame([{"ts":t,**bar()} for t in ts]))
    def test_bad_ohlc_rejected(self):
        with self.assertRaises(ValueError):clean_candles(pd.DataFrame([{"ts":0,**bar(l=101)}]))
    def test_pivots_confirmed_only_after_right_bars(self):
        vals=[1,2,3,8,4,3,2]
        d=pd.DataFrame({"ts":np.arange(7)*MINUTE,"close_ts":(np.arange(7)+1)*MINUTE,"high":vals,"low":np.array(vals)-.5})
        self.assertFalse(pivots(d.iloc[:6]))
        p=pivots(d);self.assertEqual(p[0]["price"],8);self.assertEqual(p[0]["confirmed_ts"],7*MINUTE)
    def test_wilder_seed(self):
        x=rma(pd.Series([1.,2.,3.,4.]),3)
        self.assertTrue(np.isnan(x.iloc[1]));self.assertAlmostEqual(x.iloc[2],2);self.assertAlmostEqual(x.iloc[3],8/3)
    def test_flat_rsi_and_atr(self):
        d=pd.DataFrame([{"ts":i*MINUTE,"close_ts":(i+1)*MINUTE,**bar(h=100,l=100)} for i in range(220)])
        e=enrich(d);self.assertEqual(e.rsi.iloc[-1],50);self.assertEqual(e.atr.iloc[-1],0)

class ExecutionTests(unittest.TestCase):
    def enter(self,side=1,cfg=None):
        a=Ledger(cfg or config());c=candidate(side);a.propose(c);a.step(2*MINUTE,{"BTCUSDT":bar()},META)
        self.assertEqual(len(a.positions),1);return a,next(iter(a.positions.values()))
    def test_no_entry_on_signal_bar(self):
        a=Ledger(config());a.propose(candidate());a.step(MINUTE,{"BTCUSDT":bar()},META);self.assertFalse(a.positions)
    def test_long_stop_includes_all_costs(self):
        a,p=self.enter();a.step(3*MINUTE,{"BTCUSDT":bar(h=100,l=97.5,c=98)},META)
        t=a.closed[-1];self.assertAlmostEqual(t["net_r"],-1,places=10)
        self.assertAlmostEqual(a.cash,10000+t["net_pnl"])
    def test_short_stop_symmetry(self):
        a,p=self.enter(-1);a.step(3*MINUTE,{"BTCUSDT":bar(h=103,l=99,c=102)},META)
        self.assertAlmostEqual(a.closed[-1]["net_r"],-1,places=10)
    def test_target_positive_after_costs(self):
        a,p=self.enter();a.step(3*MINUTE,{"BTCUSDT":bar(h=105,l=99,c=104)},META)
        self.assertEqual(a.closed[-1]["reason"],"TP");self.assertGreater(a.closed[-1]["net_pnl"],0);self.assertLess(a.closed[-1]["net_r"],2)
    def test_ambiguous_stop_first(self):
        a,p=self.enter();a.step(3*MINUTE,{"BTCUSDT":bar(h=110,l=90)},META)
        self.assertEqual(a.closed[-1]["reason"],"SL");self.assertEqual(a.closed[-1]["ambiguous_bars"],1)
    def test_gap_larger_than_planned_loss(self):
        a,p=self.enter();a.step(3*MINUTE,{"BTCUSDT":bar(o=95,h=96,l=94,c=95)},META)
        self.assertEqual(a.closed[-1]["reason"],"SL_GAP");self.assertLess(a.closed[-1]["net_r"],-1)
    def test_entry_drift_cancels(self):
        a=Ledger(config());a.propose(candidate());a.step(2*MINUTE,{"BTCUSDT":bar(o=105,h=106,l=104,c=105)},META)
        self.assertFalse(a.positions);self.assertIn("ушла",a.rejections[-1]["reason"])
    def test_missed_entry_not_backfilled(self):
        a=Ledger(config());a.propose(candidate());a.step(3*MINUTE,{"BTCUSDT":bar()},META)
        self.assertFalse(a.positions);self.assertIn("Пропущено",a.rejections[-1]["reason"])
    def test_funding_direction_and_idempotency(self):
        for side in (1,-1):
            a,p=self.enter(side);old=a.cash
            f={"BTCUSDT":[{"ts":3*MINUTE,"rate":.001,"mark_price":100}]}
            a.step(3*MINUTE,{"BTCUSDT":bar()},META,f);expected=old-side*p.qty*.1
            self.assertAlmostEqual(a.cash,expected)
            a.step(3*MINUTE,{"BTCUSDT":bar()},META,f);self.assertAlmostEqual(a.cash,expected)
    def test_funding_not_charged_on_same_timestamp_entry(self):
        a=Ledger(config());a.propose(candidate());a.step(2*MINUTE,{"BTCUSDT":bar()},META,{"BTCUSDT":[{"ts":2*MINUTE,"rate":.1,"mark_price":100}]})
        self.assertEqual(next(iter(a.positions.values())).funding_paid,0)
    def test_funding_in_closed_pnl(self):
        a,p=self.enter();a.step(3*MINUTE,{"BTCUSDT":bar()},META,{"BTCUSDT":[{"ts":3*MINUTE,"rate":.001,"mark_price":100}]})
        a.step(4*MINUTE,{"BTCUSDT":bar(h=105,l=99,c=104)},META)
        self.assertAlmostEqual(a.cash,10000+a.closed[0]["net_pnl"])
    def test_total_and_side_risk_caps(self):
        a=Ledger(config())
        for sym in META:a.propose(candidate(symbol=sym,cid=sym))
        a.step(2*MINUTE,{s:bar() for s in META},META)
        self.assertLessEqual(sum(p.planned_risk for p in a.positions.values()),50.0001)
        self.assertLessEqual(sum(p.qty*p.entry for p in a.positions.values()),10000)
    def test_same_bar_future_exit_cannot_free_slot(self):
        c=config();c["risk"]["max_positions"]=1;a,p=self.enter(cfg=c)
        a.propose(candidate(symbol="ETHUSDT",cid="eth",ts=2*MINUTE))
        a.step(3*MINUTE,{"BTCUSDT":bar(h=105,l=99,c=104),"ETHUSDT":bar()},META)
        self.assertFalse(a.positions);self.assertTrue(any("Лимит позиций" in r["reason"] for r in a.rejections))
    def test_timeout(self):
        c=config();c["risk"]["max_hold_minutes"]=5;a,p=self.enter(cfg=c)
        for t in range(3,7):a.step(t*MINUTE,{"BTCUSDT":bar()},META)
        self.assertEqual(a.closed[-1]["reason"],"TIMEOUT")
    def test_restart_same_ledger_state(self):
        a,p=self.enter();b=Ledger(a.config,a.name,json.loads(json.dumps(a.dump())))
        for l in (a,b):l.step(3*MINUTE,{"BTCUSDT":bar(h=105,l=99,c=104)},META)
        self.assertEqual(a.dump(),b.dump())
    def test_rules_snapshot_and_trailing_next_bar(self):
        c=config();c["risk"]["trailing_atr"]=.5;a,p=self.enter(cfg=c)
        a.step(3*MINUTE,{"BTCUSDT":bar(h=103,l=99,c=103)},META)
        self.assertTrue(a.positions);self.assertAlmostEqual(p.stop,102)
        a.step(4*MINUTE,{"BTCUSDT":bar(o=103,h=103.5,l=101,c=102)},META)
        self.assertEqual(a.closed[-1]["reason"],"SL")
    def test_risk_halt(self):
        a,p=self.enter();a.peak=12000;a.step(3*MINUTE,{"BTCUSDT":bar()},META);self.assertTrue(a.halted)
    def test_no_metadata_no_entry(self):
        a=Ledger(config());a.propose(candidate());a.step(2*MINUTE,{"BTCUSDT":bar()},{})
        self.assertFalse(a.positions)

class DataAndStorageTests(unittest.TestCase):
    def test_bad_contract_rejected(self):
        b=Bybit();b.get=lambda *a,**k:{"result":{"list":[{"contractType":"InversePerpetual","settleCoin":"BTC","status":"Trading"}]}}
        with self.assertRaises(DataError):b.instrument("BTCUSDT")
    def test_funding_pages_and_mark_source(self):
        b=Bybit();calls=[]
        def get(path,p):
            calls.append(path)
            if path.endswith("history"):return {"result":{"list":[{"fundingRateTimestamp":"60000","fundingRate":"0.001"}]}}
            return {"result":{"list":[["60000","100","101","99","100"]]}}
        b.get=get;e=b.funding("BTCUSDT",0,120000)
        self.assertEqual(e[0]["mark_price"],100);self.assertEqual(e[0]["mark_source"],"1m_mark_open_approximation")
    def test_persistence_and_decision_uniqueness(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(Path(td)/"a.sqlite");s.save_config(config());c=candidate()
            for _ in range(2):s.commit_cycle({"ok":True},[c],[("a",c.id,False,["test"])],[])
            self.assertEqual(len(s.recent_candidates()),1);self.assertEqual(len(s.recent_candidates()[0]["decisions"]),1)
            s.db.close();s=Store(Path(td)/"a.sqlite");self.assertTrue(s.get("engine")["ok"]);s.db.close()

class CausalityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();make_demo(cls.temp.name,16)
        cls.m,cls.data,_=load_dataset(cls.temp.name)
        cls.d=cls.data["BTCUSDT"];cls.frames=prepare(cls.d)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_full_history_equals_prefix(self):
        cutoff=self.m["start_ms"]+15*86400000
        prefix=prepare(self.d[self.d.ts<cutoff])
        a,s=analyze("BTCUSDT",self.frames,cutoff,config(),None,self.frames)
        b,t=analyze("BTCUSDT",prefix,cutoff,config(),None,prefix)
        self.assertEqual([x.to_dict() for x in a],[x.to_dict() for x in b]);self.assertEqual(s,t)
    def test_future_changes_cannot_change_decision(self):
        cutoff=self.m["start_ms"]+15*86400000;changed=self.d.copy()
        changed.loc[changed.ts>=cutoff,["open","high","low","close"]]*=2
        ff=prepare(changed)
        for ts in range(cutoff-3600000,cutoff,300000):
            a,s=analyze("BTCUSDT",self.frames,ts,config(),None,self.frames)
            b,t=analyze("BTCUSDT",ff,ts,config(),None,ff)
            self.assertEqual([x.to_dict() for x in a],[x.to_dict() for x in b]);self.assertEqual(s,t)
    def test_future_snapshot_marked_missing(self):
        found=[]
        for ts in range(self.m["end_ms"]-86400000,self.m["end_ms"],300000):
            found,_=analyze("BTCUSDT",self.frames,ts,config(),{"ts":ts+1,"spread_bps":1},self.frames)
            if found:break
        self.assertTrue(found);self.assertIsNone(found[0].checks["spread"].passed)
    def test_warmup(self):
        out,status=analyze("BTCUSDT",self.frames,self.m["start_ms"]+86400000,config())
        self.assertFalse(out);self.assertEqual(status["status"],"WARMUP")
    def test_checksum_detects_mutation(self):
        p=Path(self.temp.name)/"manifest.json";m=json.loads(p.read_text());old=m["files"]["BTCUSDT.csv"]
        m["files"]["BTCUSDT.csv"]="bad";p.write_text(json.dumps(m))
        try:
            with self.assertRaises(DataError):load_dataset(self.temp.name)
        finally:m["files"]["BTCUSDT.csv"]=old;p.write_text(json.dumps(m))

if __name__=="__main__":unittest.main()
