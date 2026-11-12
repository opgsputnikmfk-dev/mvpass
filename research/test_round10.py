import unittest

import numpy as np

from search_round10 import SYMBOLS, pair, ranking


class FundingDispersionTests(unittest.TestCase):
    def test_cutoff_and_threshold(self):
        events = {
            s: (np.arange(1, 6), np.array([0, 0, 0, 1, 1.])*k + k*.0001)
            for k, s in enumerate(SYMBOLS)
        }
        self.assertEqual(ranking(events, 4, .0001)[0], [0, 2])
        self.assertIsNone(ranking(events, 4, .00025)[0])
        self.assertEqual(ranking(events, 5, .00025)[0], [0, 2])

    def test_short_receives_positive_funding_and_costs_increase(self):
        price = np.array([100., 100.])
        marks = np.tile(price, (2, 1))
        funding = np.array([[0, 0], [0, 0], [0, 1]])
        base = pair(price, price, marks, funding, 10000, .00045)
        stress = pair(price, price, marks, funding, 10000, .0009)
        self.assertGreater(base[2], 0)
        self.assertGreater(base[0], stress[0])
        self.assertAlmostEqual(base[1][-1], 10000 + base[0] - base[2])
