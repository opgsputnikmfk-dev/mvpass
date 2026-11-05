import copy
import unittest
from analyst.valuation import position_view
from analyst.execution import Ledger
from analyst.spot import BinanceSpot
from test_core import config,candidate,bar,META,MINUTE

class ValuationTests(unittest.TestCase):
    def opened(self,side=1):
        a=Ledger(config());a.propose(candidate(side));a.step(2*MINUTE,{"BTCUSDT":bar()},META)
        return a,next(iter(a.positions.values()))
    def test_long_uses_bid_and_all_estimated_costs(self):
        a,p=self.opened();now=4*MINUTE
        v=position_view(p,a,{p.symbol:{"bid":102.,"ask":102.1,"ts":now}},now)
        fill=102*(1-p.rules["slippage_bps"]/10000)
        expected=(fill-p.entry)*p.qty-p.entry_fee-fill*p.qty*p.rules["fee_rate"]
        self.assertEqual(v["current_price"],102);self.assertEqual(v["price_kind"],"bid")
        self.assertAlmostEqual(v["estimated_pnl"],expected)
        self.assertAlmostEqual(v["estimated_pnl_pct"],expected/(p.entry*p.qty)*100)
    def test_short_uses_ask_and_funding(self):
        a,p=self.opened(-1);p.funding_paid=2.;now=4*MINUTE
        v=position_view(p,a,{p.symbol:{"bid":97.9,"ask":98.,"ts":now}},now)
        fill=98*(1+p.rules["slippage_bps"]/10000)
        self.assertEqual(v["current_price"],98)
        self.assertAlmostEqual(v["estimated_pnl"],(p.entry-fill)*p.qty-p.entry_fee-fill*p.qty*p.rules["fee_rate"]-2)
    def test_no_price_does_not_show_fake_zero_pnl(self):
        a,p=self.opened();a.last_prices={};a.last_ts=0
        v=position_view(p,a,{},4*MINUTE)
        self.assertIsNone(v["estimated_pnl"]);self.assertTrue(v["price_stale"])
    def test_old_quote_falls_back_to_known_minute_and_marks_stale(self):
        a,p=self.opened();now=20*MINUTE
        v=position_view(p,a,{p.symbol:{"bid":200.,"ask":201.,"ts":MINUTE}},now)
        self.assertEqual(v["current_price"],100);self.assertEqual(v["price_kind"],"minute_close")
        self.assertTrue(v["price_stale"])
    def test_future_crossed_and_nan_quotes_not_used(self):
        a,p=self.opened();now=4*MINUTE
        for q in ({"bid":200,"ask":201,"ts":now+1},{"bid":200,"ask":199,"ts":now},{"bid":float('nan'),"ask":201,"ts":now}):
            self.assertEqual(position_view(p,a,{p.symbol:q},now)["price_kind"],"minute_close")
    def test_reporting_does_not_modify_execution_or_risk(self):
        a,p=self.opened();before=copy.deepcopy(a.dump());now=4*MINUTE
        position_view(p,a,{p.symbol:{"bid":102.,"ask":103.,"ts":now}},now)
        self.assertEqual(a.dump(),before)
    def test_batched_quotes_exclude_unrequested_symbols(self):
        api=BinanceSpot();calls=[]
        def get(path,params):
            calls.append((path,params))
            return [{"symbol":"BTCUSDT","bidPrice":"100","askPrice":"101"},{"symbol":"ETHUSDT","bidPrice":"1","askPrice":"2"}]
        api.get=get;q=api.live_quotes({"BTCUSDT"})
        self.assertEqual(set(q),{"BTCUSDT"});self.assertEqual(len(calls),1)

if __name__=="__main__":unittest.main()
