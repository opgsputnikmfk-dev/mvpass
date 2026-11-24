import unittest
from unittest.mock import Mock,patch
from research_runner import replay,guarded_replay
from research_equity import MINUTE


class RunnerTests(unittest.TestCase):
    def rows(self):
        return [dict(ts=i*MINUTE,open=p,mark=p,funding_rate=None)
                for i,p in enumerate((100.,90.,110.))]

    def test_causal_replay_drawdown_and_balance(self):
        r=replay(self.rows(),0,3*MINUTE,{MINUTE:dict(target=1,decision_ts=0)},terminal_price=110,fee=0,slip=0)
        self.assertAlmostEqual(r["terminal_equity"]-10000,sum(t["net"] for t in r["trades"]))

    def test_future_or_missing_data_rejected(self):
        with self.assertRaises(ValueError):
            replay(self.rows(),0,3*MINUTE,{MINUTE:dict(target=1,decision_ts=MINUTE)},terminal_price=110)
        with self.assertRaises(ValueError):
            replay(self.rows()[::2],0,3*MINUTE,{},terminal_price=110)
        with self.assertRaises(ValueError):
            replay(self.rows()[:2],0,3*MINUTE,{},terminal_price=110)

    def test_loader_not_called_if_lock_or_window_invalid(self):
        loader=Mock()
        with patch("research_runner.verify",side_effect=ValueError("bad")):
            with self.assertRaises(ValueError):
                guarded_replay("x",".","bad",["2024-07-01","2024-08-01"],loader,{})
        loader.assert_not_called()
        with patch("research_runner.verify",return_value=dict(windows=[])):
            with self.assertRaises(ValueError):
                guarded_replay("x",".","bad",["2024-07-01","2024-08-01"],loader,{})
        loader.assert_not_called()


if __name__=="__main__":
    unittest.main()
