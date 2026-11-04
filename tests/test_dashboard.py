import copy
import tempfile
import unittest
from pathlib import Path
from analyst.engine import Engine
from analyst.storage import Store
from test_core import config,candidate,bar,META,MINUTE

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/"a.sqlite")
        self.engine=Engine(self.store,config(),demo=True)
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def test_selected_coins_visible_when_api_unavailable(self):
        self.engine.status="DATA_ERROR";d=self.engine.state()["dashboard"]
        self.assertEqual(len(d["coins"]),3)
        self.assertTrue(all(c["last_analysis"] is None for c in d["coins"]))
        self.assertTrue(all(c["status"]=="Нет свежих данных" for c in d["coins"]))
    def test_combinations_preserve_exact_filters_and_archive(self):
        c=copy.deepcopy(self.engine.config);c["filters"]["rsi"]="on";self.engine.update_config(c)
        d=self.engine.state()["dashboard"];current=[v for v in d["comparisons"] if v["current"]]
        self.assertEqual(len(current),3);self.assertEqual(len(d["comparisons"]),6)
        active=next(v for v in current if v["name"]=="Мои фильтры")
        self.assertEqual(len(active["filters"]),1);self.assertIn("RSI",active["filters"][0])
        baseline=next(v for v in current if v["name"]=="Без дополнительных фильтров")
        self.assertFalse(baseline["filters"])
    def test_real_trade_statistics_and_filter_attribution(self):
        c=candidate();c.checks["rsi"].passed=False
        key=self.engine.generation+":baseline";a=self.engine.accounts[key]
        self.store.commit_cycle(self.engine.dump(),[c],[(key,c.id,True,[])],[])
        a.propose(c);a.step(2*MINUTE,{"BTCUSDT":bar()},META)
        a.step(3*MINUTE,{"BTCUSDT":bar(l=97,h=100,c=98)},META)
        d=self.engine.state()["dashboard"];b=next(x for x in d["comparisons"] if x["key"]==key)
        self.assertEqual(b["stats"]["trades"],1);self.assertLess(b["realized_pnl"],0)
        self.assertEqual(b["by_symbol"]["BTCUSDT"]["trades"],1)
        self.assertEqual(b["activity"]["accepted"],1)
        self.assertEqual(d["filter_attribution"]["rsi"]["fail"]["n"],1)
        self.assertEqual(d["filter_attribution"]["rsi"]["pass"]["n"],0)
        self.assertAlmostEqual(d["filter_attribution"]["ema"]["pass"]["mean_r"],-1)
    def test_no_fake_filter_outcomes_without_trades(self):
        d=self.engine.state()["dashboard"]
        self.assertIsNone(d["filter_attribution"]["ema"]["pass"]["mean_r"])
        self.assertTrue(all(c["stats"]["trades"]==0 for c in d["comparisons"]))

if __name__=="__main__":unittest.main()
