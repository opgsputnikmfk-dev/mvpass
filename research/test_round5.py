import unittest
import numpy as np
from search_round5 import DAY, FEE, pair, ranking


class PairTests(unittest.TestCase):
    def test_flat_pair_costs_and_equity(self):
        p = np.array([100., 200.])
        args = (p, p, np.tile(p, (2, 1)), np.zeros((3, 2)), 10000)
        net, equity, fund, fee = pair(*args, 0)
        self.assertAlmostEqual(net, -4000 * FEE)
        self.assertAlmostEqual(fee, -net)
        self.assertEqual(fund, 0)
        np.testing.assert_allclose(equity, 10000 + net)
        self.assertLess(pair(*args, .0009)[0], net)

    def test_funding_sign_and_boundary(self):
        p = np.array([100., 200.])
        net, equity, fund, fee = pair(p, p, np.tile(p, (2, 1)),
                                      np.array([[0, 0], [1, 0], [1, 4]]), 10000, 0)
        self.assertAlmostEqual(fund, 10)
        self.assertAlmostEqual(net, fund - fee)
        self.assertAlmostEqual(equity[-1], 10000 - fee - 10)

    def test_future_and_partial_day_do_not_change_rank(self):
        t = DAY * 8
        c = np.full((t + 5, 3), 100.)
        c[t-1] = [120, 100, 80]
        self.assertEqual(ranking(c, t, 7), (0, 2))
        c[t:] = [1, 100, 999]
        self.assertEqual(ranking(c, t, 7), (0, 2))
