import unittest
from research_equity import EquityRecorder, MINUTE, DAY


class RecorderTests(unittest.TestCase):
    def test_open_drawdown_not_hidden_by_profitable_exit(self):
        r = EquityRecorder(0, 3*MINUTE)
        for i,e in enumerate((10000,9000,10100)):
            r.append(i*MINUTE,e)
        out = r.finish(10200)
        self.assertAlmostEqual(out["max_drawdown_pct"],10)
        self.assertEqual(out["daily_open_equity"],[[0,10000]])
        self.assertEqual(out["terminal"]["equity"],10200)

    def test_missing_and_duplicate_minutes_fail(self):
        r = EquityRecorder(0, 2*MINUTE)
        with self.assertRaises(ValueError):r.append(MINUTE,10000)
        r.append(0,10000)
        with self.assertRaises(ValueError):r.append(0,10000)
        with self.assertRaises(ValueError):r.finish(10000)

    def test_midnight_and_terminal_are_distinct(self):
        r=EquityRecorder(DAY-MINUTE,DAY+MINUTE)
        r.append(DAY-MINUTE,9999)
        r.append(DAY,9998)
        out=r.finish(9997)
        self.assertEqual(out["daily_open_equity"],[[DAY,9998]])
        self.assertEqual(out["terminal"]["ts"],DAY+MINUTE)
        with self.assertRaises(ValueError):r.finish(9997)

    def test_nonfinite_does_not_advance_recorder(self):
        r=EquityRecorder(0,MINUTE)
        with self.assertRaises(ValueError):r.append(0,float("nan"))
        r.append(0,10000)
        with self.assertRaises(ValueError):r.finish(float("inf"))
        self.assertEqual(r.finish(10000)["terminal_equity"],10000)


if __name__=="__main__":
    unittest.main()
