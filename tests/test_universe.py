import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from analyst.config import DEFAULT
from analyst.engine import Engine
from analyst.storage import Store
from analyst.data import DataError
from analyst.universe import FALLBACK20,rank_contracts,fetch_top20
from test_core import config,candidate,bar,META,MINUTE

NOW=1793836800000
def instrument(s):
    return {"symbol":s,"baseCoin":s[:-4],"quoteCoin":"USDT","settleCoin":"USDT",
            "status":"Trading","contractType":"LinearPerpetual","launchTime":str(NOW-100*86400000)}
def ticker(s,volume=100):return {"symbol":s,"turnover24h":str(volume),"bid1Price":"100","ask1Price":"100.1"}

class UniverseTests(unittest.TestCase):
    def test_twenty_unique_defaults(self):
        self.assertEqual(len(DEFAULT["symbols"]),20);self.assertEqual(len(set(FALLBACK20)),20)
    def test_rank_by_turnover_not_unit_volume(self):
        symbols=[f"X{i:02}USDT" for i in range(30)]
        ranked=rank_contracts([instrument(s) for s in symbols],[ticker(s,i+1) for i,s in enumerate(symbols)],NOW)
        self.assertEqual(len(ranked),20);self.assertEqual(ranked[0]["symbol"],"X29USDT")
        self.assertEqual(ranked[-1]["symbol"],"X10USDT")
    def test_exclusions_and_missing_data(self):
        good=instrument("BTCUSDT");stable=instrument("USDCUSDT");new=instrument("NEWUSDT");new["launchTime"]=str(NOW-86400000)
        stock=instrument("AAPLUSDT");stock["symbolType"]="xstocks"
        stopped=instrument("ETHUSDT");stopped["status"]="Settling"
        inverse=instrument("INVUSDT");inverse["contractType"]="InversePerpetual"
        missing=instrument("NANUSDT")
        rr=[good,stable,new,stock,stopped,inverse,missing]
        ts=[ticker(i["symbol"],1e8) for i in rr];ts[-1]["turnover24h"]="nan"
        ranked=rank_contracts(rr,ts,NOW);self.assertEqual([x["symbol"] for x in ranked],["BTCUSDT"])
    def test_jup_is_not_a_leveraged_up_token(self):
        self.assertEqual(rank_contracts([instrument("JUPUSDT")],[ticker("JUPUSDT")],NOW)[0]["symbol"],"JUPUSDT")
    def test_pagination_and_deterministic_ties(self):
        class API:
            def get(self,path,params):
                if path.endswith("tickers"):return {"time":NOW,"result":{"list":[ticker("SOLUSDT"),ticker("BTCUSDT")]}}
                if "cursor" not in params:return {"result":{"list":[instrument("SOLUSDT")],"nextPageCursor":"next"}}
                return {"result":{"list":[instrument("BTCUSDT")],"nextPageCursor":""}}
        ranked,ts=fetch_top20(API(),16)
        self.assertEqual([r["symbol"] for r in ranked],["BTCUSDT","SOLUSDT"]);self.assertEqual(ts,NOW)
    def test_saved_config_migration_preserves_filters_and_open_trade(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)/"a.sqlite");c=config();c["filters"]["rsi"]="on";engine=Engine(store,c)
            key=engine.generation+":active";a=engine.accounts[key];signal=candidate()
            a.propose(signal);a.step(2*MINUTE,{"BTCUSDT":bar()},META)
            before=next(iter(a.positions.values())).to_dict()
            engine.enable_top20()
            self.assertEqual(len(engine.config["symbols"]),20);self.assertEqual(engine.config["filters"]["rsi"],"on")
            self.assertEqual(next(iter(a.positions.values())).to_dict(),before)
            loaded=Engine(store);loaded.enable_top20();self.assertEqual(loaded.generation,engine.generation)
            store.db.close()
    def test_api_failure_keeps_twenty_without_claiming_verified_ranking(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)/"a.sqlite");engine=Engine(store,config());engine.enable_top20()
            with patch("analyst.universe.fetch_top20",side_effect=DataError("HTTP 403")):
                engine.refresh_top20()
            self.assertEqual(len(engine.config["symbols"]),20);self.assertFalse(engine.universe["verified"])
            self.assertIn("Резервный",engine.universe["label"]);store.db.close()
    def test_ranking_refresh_safe_on_empty_portfolio_and_rate_limited(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)/"a.sqlite");engine=Engine(store,config());engine.enable_top20()
            rows=[{"symbol":f"X{i:02}USDT","turnover24h":100+i} for i in range(20)]
            with patch("analyst.engine.time.time",return_value=NOW/1000),patch("analyst.universe.fetch_top20",return_value=(rows,NOW)) as fetch:
                engine.refresh_top20();engine.refresh_top20()
                self.assertEqual(fetch.call_count,1)
            self.assertTrue(engine.universe["verified"]);self.assertEqual(engine.config["symbols"],[r["symbol"] for r in rows]);store.db.close()
    def test_twenty_coin_full_scan_records_all_symbols(self):
        from analyst.data import make_demo,load_dataset
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);make_demo(root/"data",16);manifest,candles,_=load_dataset(root/"data")
            end=manifest["end_ms"];now=end+5000;fixture=candles["BTCUSDT"]
            class API:
                def server_time(self):return now
                def candles(self,symbol,start,finish):return fixture[(fixture.ts>=start)&(fixture.ts<=finish)].copy()
                def instrument(self,symbol):return META["BTCUSDT"]
                def snapshot(self,symbol):return {"ts":now,"spread_bps":2.,"depth_usdt":1e6,"oi_change_pct":.1,"funding_rate":.0001,"next_funding_ts":end+480*MINUTE}
            store=Store(root/"db.sqlite");engine=Engine(store);engine.api=API()
            with patch("analyst.engine.time.time",return_value=now/1000):engine.cycle()
            self.assertEqual(set(engine.last_analysis),set(FALLBACK20))
            self.assertEqual(engine.status,"RUNNING");self.assertEqual(len(engine.state()["dashboard"]["coins"]),20)
            store.db.close()

if __name__=="__main__":unittest.main()
