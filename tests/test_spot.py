import copy
import tempfile
import unittest
from pathlib import Path
from analyst.spot import BinanceSpot
from analyst.config import validate
from analyst.data import DataError
from analyst.engine import Engine
from analyst.storage import Store
from test_core import config,MINUTE

def info(symbol="BTCUSDT",base="BTC"):
    return {"symbol":symbol,"baseAsset":base,"quoteAsset":"USDT","status":"TRADING","isSpotTradingAllowed":True,
            "filters":[{"filterType":"PRICE_FILTER","tickSize":"0.1"},
                       {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
                       {"filterType":"MARKET_LOT_SIZE","stepSize":"0","minQty":"0","maxQty":"50"},
                       {"filterType":"NOTIONAL","minNotional":"5"}]}

class SpotTests(unittest.TestCase):
    def test_spot_config_accepted_without_allowing_crossed_sources(self):
        c=config();c["exchange"]="binance";c["market"]="spot_reference";validate(c)
        c["market"]="linear_usdt_perpetual"
        with self.assertRaises(ValueError):validate(c)
    def test_order_endpoints_are_impossible(self):
        with self.assertRaises(DataError):BinanceSpot().get("/api/v3/order")
    def test_metadata_zero_market_step_falls_back_to_lot_step(self):
        a=BinanceSpot();a.info=lambda:{"symbols":[info()]};m=a.instrument("BTCUSDT")
        self.assertEqual(m["qty_step"],.001);self.assertEqual(m["min_notional"],5)
        self.assertEqual(m["max_qty"],50);self.assertEqual(m["funding_interval_minutes"],0)
    def test_inactive_symbol_rejected(self):
        a=BinanceSpot();i=info();i["status"]="BREAK";a.info=lambda:{"symbols":[i]}
        with self.assertRaises(DataError):a.instrument("BTCUSDT")
    def test_candles_use_open_timestamps_and_no_missing_minutes(self):
        a=BinanceSpot()
        a.get=lambda p,q:[[t,"100","101","99","100","2"] for t in (0,MINUTE,2*MINUTE)]
        d=a.candles("BTCUSDT",0,2*MINUTE);self.assertEqual(len(d),3)
        a.get=lambda p,q:[[MINUTE,"100","101","99","100","2"]]
        with self.assertRaises(DataError):a.candles("BTCUSDT",0,MINUTE)
    def test_funding_and_open_interest_never_fabricated(self):
        a=BinanceSpot();a.get=lambda p,q:{"bids":[["100","100"]],"asks":[["100.1","100"]]}
        s=a.snapshot("BTCUSDT")
        self.assertIsNone(s["funding_rate"]);self.assertIsNone(s["oi_change_pct"])
        self.assertIsNone(s["mark_price"]);self.assertEqual(a.funding("BTCUSDT",0,1),[])
    def test_rank_uses_quote_volume_and_excludes_stablecoin(self):
        a=BinanceSpot();now=1793866800000;cutoff=now-16*86400000
        a.server_time=lambda:now
        a.info=lambda:{"symbols":[info(),info("ETHUSDT","ETH"),info("USDCUSDT","USDC"),info("NEWUSDT","NEW")]}
        def get(path,params=None):
            if path.endswith("24hr"):
                return [{"symbol":s,"quoteVolume":v,"bidPrice":"100","askPrice":"100.1","volume":"999999999"}
                        for s,v in [("BTCUSDT","100"),("ETHUSDT","200"),("USDCUSDT","999999"),("NEWUSDT","1000")]]
            return [] if params["symbol"]=="NEWUSDT" else [[cutoff]]
        a.get=get;ranked,ts=a.top20(16)
        self.assertEqual([x["symbol"] for x in ranked],["ETHUSDT","BTCUSDT"])
    def test_config_source_cannot_change_inside_existing_account_store(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(Path(td)/"a.sqlite");e=Engine(s,config());c=copy.deepcopy(e.config)
            c["exchange"]="binance";c["market"]="spot_reference"
            with self.assertRaisesRegex(ValueError,"источника"):e.update_config(c)
            s.db.close()
    def test_higher_timeframes_are_closed_and_keep_indicator_seed(self):
        a=BinanceSpot();calls=[]
        def get(path,q):
            calls.append(q);step={"5m":5,"15m":15,"1h":60,"4h":240}[q["interval"]]*MINUTE
            return [[t,"100","101","99","100","2"] for t in range(q["startTime"],q["endTime"]+1,step)]
        a.get=get
        with tempfile.TemporaryDirectory() as td:
            s=Store(Path(td)/"a.sqlite");end=1793866800000
            first=a.analysis_frames("BTCUSDT",s,end);n=len(calls)
            repeat=a.analysis_frames("BTCUSDT",s,end)
            self.assertEqual(len(calls),n)
            for m,d in first.items():
                self.assertEqual(len(d),96 if m==240 else 320);self.assertLessEqual(int(d.close_ts.iloc[-1]),end)
                self.assertEqual(d.close.tolist(),repeat[m].close.tolist())
            later=a.analysis_frames("BTCUSDT",s,end+3600000)
            self.assertEqual(later[5].iloc[319].ema200,first[5].iloc[319].ema200)
            s.db.close()

if __name__=="__main__":unittest.main()
