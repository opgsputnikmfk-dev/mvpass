import unittest
from unittest.mock import patch

import numpy as np
from search_round8 import rf, run, signal


class LeadLagTests(unittest.TestCase):
    def test_only_closed_bars_and_lag_filter(self):
        for side in (1, -1):
            btc = np.full(300, 100.)
            eth = btc.copy()
            btc[60] += side
            btc[61:] = 1000.
            self.assertAlmostEqual(signal(btc, eth, 61), side*.01)
            eth[60] += side
            self.assertEqual(signal(btc, eth, 61), 0)

    def test_delayed_execution_funding_and_stress(self):
        prices = np.full(300, 100.)
        btc = prices.copy()
        btc[60] = 101.
        funding = np.zeros(300)
        funding[122] = 1.
        with patch.object(rf, 'ms', lambda x: {'a':61*rf.MIN,'b':123*rf.MIN}[x]):
            args = (0, prices, prices, btc, prices, funding), ('a','b'), .0035
            base = run(*args)
            stress = run(*args, .0009)
        self.assertEqual(base['count'], 1)
        self.assertEqual(base['trades'][0][:2], [62*rf.MIN,122*rf.MIN])
        self.assertLess(base['trades'][0][7], 0)
        self.assertLess(stress['net_usdt'], base['net_usdt'])
