import unittest
from research_accounting import Account
from research_equity import MINUTE


class AccountingTests(unittest.TestCase):
    def test_funding_before_reversal(self):
        a=Account(0,3*MINUTE,fee=0,slip=0)
        a.step(0,100,100,target=1,funding_rate=.9)
        self.assertEqual(a.cash,10000)
        a.step(MINUTE,100,100,target=-1,funding_rate=.01)
        self.assertEqual(a.closed[0]["net"],-10)
        a.step(2*MINUTE,100,100,funding_rate=.01)
        a.finish(100)
        self.assertAlmostEqual(a.cash,9999.99)
        self.assertAlmostEqual(sum(t["net"] for t in a.closed),-.01)

    def test_open_drawdown_and_terminal_reconciliation(self):
        a=Account(0,2*MINUTE,fee=0,slip=0)
        a.step(0,100,100,target=1,fraction=1)
        a.step(MINUTE,90,90)
        r=a.finish(110)
        self.assertAlmostEqual(r["max_drawdown_pct"],10)
        self.assertEqual(a.cash,11000)

    def test_costs_hold_and_duplicate(self):
        a=Account(0,2*MINUTE)
        a.step(0,100,100,target=1)
        before=a.cash
        with self.assertRaises(ValueError):
            a.step(0,100,100,funding_rate=.5)
        self.assertEqual(a.cash,before)
        a.step(MINUTE,100,100,target=1)
        self.assertEqual(a.cash,before)
        a.finish(100)
        self.assertEqual(len(a.closed),1)
        self.assertLess(a.cash,10000)


if __name__=="__main__":
    unittest.main()
