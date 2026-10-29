"""Offline regression tests. No HTTP calls, no Telegram messages, no orders."""
import dataclasses
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import bot
from bot import (Config, Position, MINUTE, TF_MS, Store, Runtime, account_equity,
                 build_feature_frame, choose_neighbors, cost_aware_be_stop,
                 eligible_history_indices, frame_from_klines, historical_r,
                 make_position, new_state, open_paper_position, portfolio_gate,
                 settle_bar, step_position, update_risk_state, validate_bars)
from backtest import aggregate, run_backtest


def bar(ts=0, o=100., h=100.5, l=99.5, c=100., duration=MINUTE):
    return dict(ts=ts, open=o, high=h, low=l, close=c, volume=100., close_ts=ts+duration-1)


def position(side='LONG', cfg=None, ts=0):
    cfg = cfg or Config(market='usdm', fee_bps=0, slippage_bps=0)
    return make_position('BTCUSDT', '15m', side, 100., 1., ts, 1., cfg)


class Exits(unittest.TestCase):
    def test_no_preentry_or_duplicate_bar(self):
        p = position(ts=MINUTE)
        step_position(p, bar(0, h=120, l=80))
        self.assertIsNone(p.exit_reason)
        step_position(p, bar(MINUTE))
        step_position(p, bar(MINUTE, h=120, l=80))
        self.assertIsNone(p.exit_reason)

    def test_old_stop_wins_ambiguous_candle(self):
        for side in ('LONG', 'SHORT'):
            p = position(side)
            step_position(p, bar(h=105, l=95))
            self.assertEqual(p.exit_reason, 'SL')
            self.assertAlmostEqual(p.net_pnl, -1.5)
            self.assertFalse(p.tp1_hit)
            self.assertEqual(p.ambiguous_bars, 1)

    def test_new_be_not_applied_retroactively(self):
        p = position()
        step_position(p, bar(h=102, l=99, c=101))
        self.assertTrue(p.tp1_hit)
        self.assertIsNone(p.exit_reason)
        self.assertAlmostEqual(p.remaining, .5)
        step_position(p, bar(MINUTE, o=101, h=101, l=99, c=100))
        self.assertEqual(p.exit_reason, 'BE')
        self.assertAlmostEqual(p.net_pnl, .75)

    def test_partial_and_final_targets_long_short(self):
        for side in ('LONG', 'SHORT'):
            p = position(side)
            step_position(p, bar(h=104, l=99) if side == 'LONG' else bar(h=101, l=96))
            self.assertEqual(p.exit_reason, 'TP2')
            self.assertAlmostEqual(p.net_pnl, 2.25)
            self.assertEqual([f['qty'] for f in p.fills], [1, .5, .5])

    def test_timeout_only_remaining_quantity(self):
        p = position()
        step_position(p, bar(h=102, l=99, c=101))
        p.deadline = 2 * MINUTE - 1
        step_position(p, bar(MINUTE, o=101, h=101.2, l=100.5, c=101))
        self.assertEqual(p.exit_reason, 'TIMEOUT')
        self.assertAlmostEqual(p.net_pnl, 1.25)

    def test_stop_gap_loses_more_than_planned_risk(self):
        p = position()
        step_position(p, bar(o=90, h=91, l=89, c=90))
        self.assertAlmostEqual(p.net_pnl, -10)
        self.assertLess(p.net_pnl / p.planned_risk, -1)

    def test_be_formula_covers_remaining_leg_fees_and_slippage(self):
        cfg = Config(market='usdm', fee_bps=10, slippage_bps=2)
        for side in ('LONG', 'SHORT'):
            p = position(side, cfg)
            be = cost_aware_be_stop(p)
            bot.exit_fill(p, be, 1., MINUTE, 'BE')
            self.assertAlmostEqual(p.net_pnl, 0., places=10)

    def test_funding_direction_and_idempotence(self):
        event = {'fundingTime': MINUTE, 'fundingRate': .001, 'markPrice': 100}
        for side, expected in [('LONG', -.1), ('SHORT', .1)]:
            p = position(side)
            step_position(p, bar(MINUTE), [event])
            step_position(p, bar(MINUTE), [event])
            self.assertAlmostEqual(p.net_pnl, expected)

    def test_funding_does_not_charge_before_or_exactly_at_entry(self):
        p = position(ts=MINUTE)
        event = {'fundingTime': MINUTE, 'fundingRate': .001, 'markPrice': 100}
        step_position(p, bar(MINUTE), [event])
        self.assertEqual(p.funding_paid, 0)

    def test_partial_funding_uses_remaining_size(self):
        p = position()
        step_position(p, bar(h=102, l=99, c=101))
        step_position(p, bar(MINUTE, o=101, h=101.2, l=100.5, c=101),
                      [{'fundingTime': MINUTE, 'fundingRate': .001, 'markPrice': 100}])
        self.assertAlmostEqual(p.funding_paid, .05)


class DataAndLearning(unittest.TestCase):
    def sample_frame(self, n=1300, interval=TF_MS['15m']):
        rng = np.random.default_rng(42)
        price = 100 * np.exp(np.cumsum(rng.normal(0, .002, n)))
        df = pd.DataFrame([bar(i*interval, p, p+.5, p-.5, p, interval)
                           for i, p in enumerate(price)])
        return df

    def test_only_closed_candles(self):
        rows = [[0, '100', '101', '99', '100', '10', MINUTE-1, 0, 0, 0, 0, 0],
                [MINUTE, '100', '101', '99', '100', '10', 2*MINUTE-1, 0, 0, 0, 0, 0]]
        self.assertEqual(len(frame_from_klines(rows, MINUTE)), 1)

    def test_missing_candles_are_rejected(self):
        df = pd.DataFrame([bar(0), bar(2*MINUTE)])
        with self.assertRaises(ValueError):
            validate_bars(df, MINUTE)

    def test_features_do_not_change_when_future_is_added(self):
        raw = self.sample_frame(400)
        a = build_feature_frame(raw.iloc[:300])
        raw.loc[300:, ['open', 'high', 'low', 'close']] *= 5
        b = build_feature_frame(raw)
        pd.testing.assert_frame_equal(a, b.iloc[:300])

    def test_flat_rsi_is_finite(self):
        raw = pd.DataFrame([bar(i*MINUTE) for i in range(300)])
        df = build_feature_frame(raw)
        self.assertAlmostEqual(df.iloc[-1].rsi, 50)

    def test_all_outcomes_finish_strictly_before_query(self):
        cfg = Config()
        indices = eligible_history_indices(1200, cfg)
        self.assertTrue((indices + cfg.horizon < 1200).all())

    def test_neighbors_do_not_overlap(self):
        cfg = Config()
        frame = build_feature_frame(self.sample_frame(2500))
        neighbors = choose_neighbors(frame, 2400, cfg)
        self.assertEqual(len(neighbors), cfg.neighbors)
        self.assertTrue(all(abs(a-b) >= cfg.horizon for j, a in enumerate(neighbors)
                            for b in neighbors[j+1:]))

    def test_prediction_does_not_inspect_future_outcomes(self):
        cfg = Config()
        df = build_feature_frame(self.sample_frame(2500))
        i = 2400
        for name, value in dict(close=110, ema200=100, ema50=105, slope=1,
                                rsi=55, adx=30, anomaly=False).items():
            df.loc[i, name] = value
        seen = []
        def fake_outcome(frame, index, side, tf, config):
            self.assertLess(index + config.horizon, i)
            seen.append(index)
            return .5
        with patch('bot.historical_r', side_effect=fake_outcome):
            signal, reason = bot.predict_knn(df, i, '15m', cfg)
        self.assertIsNotNone(signal, reason)
        self.assertEqual(len(seen), cfg.neighbors)

    def test_training_target_uses_partial_exit_costs_and_next_open(self):
        cfg = Config(market='usdm', fee_bps=0, slippage_bps=0)
        rows = [bar(i*TF_MS['15m'], duration=TF_MS['15m']) for i in range(50)]
        rows[1].update(high=104, low=99)
        df = pd.DataFrame(rows)
        df['atr'] = 1.
        self.assertAlmostEqual(historical_r(df, 0, 'LONG', '15m', cfg), 1.5)


class PortfolioAndPersistence(unittest.TestCase):
    def pending(self, symbol='BTCUSDT', tf='15m'):
        return dict(symbol=symbol, tf=tf, side='LONG', atr=1., reference=100., entry_ts=0)

    def test_spot_short_rejected(self):
        with self.assertRaises(ValueError):
            position('SHORT', Config())

    def test_sizing_includes_costs_and_closing_balances(self):
        cfg = Config()
        state = new_state(cfg)
        p, reason = open_paper_position(state, self.pending(), bar(), cfg)
        self.assertFalse(reason)
        self.assertLessEqual(p.planned_risk, cfg.initial_equity * cfg.risk_fraction + 1e-9)
        self.assertLessEqual(p.qty*p.entry, cfg.initial_equity*cfg.max_position_notional+1e-9)
        closed = settle_bar(state, p.id, bar(h=104, l=99), [], cfg)
        self.assertIsNotNone(closed)
        self.assertAlmostEqual(state['balance'], cfg.initial_equity + closed['net_pnl'])
        self.assertAlmostEqual(account_equity(state), state['balance'])

    def test_same_symbol_and_global_direction_limits(self):
        cfg = Config()
        state = new_state(cfg)
        open_paper_position(state, self.pending(), bar(), cfg)
        self.assertEqual(portfolio_gate(state, 'BTCUSDT', '1h', 'LONG', 0, cfg), 'symbol_already_open')
        open_paper_position(state, self.pending('ETHUSDT'), bar(), cfg)
        self.assertEqual(portfolio_gate(state, 'SOLUSDT', '4h', 'LONG', 0, cfg), 'direction_limit')

    def test_gap_cancels_entry_instead_of_historical_fill(self):
        cfg = Config()
        p, why = open_paper_position(new_state(cfg), self.pending(), bar(o=102, h=103, l=101, c=102), cfg)
        self.assertIsNone(p)
        self.assertEqual(why, 'entry_gap')

    def test_daily_latch_and_persistent_drawdown_pause(self):
        cfg = Config()
        state = new_state(cfg)
        update_risk_state(state, 0, cfg)
        state['balance'] = 9700
        update_risk_state(state, MINUTE, cfg)
        self.assertTrue(state['day_paused'])
        state['balance'] = 9900
        update_risk_state(state, 2*MINUTE, cfg)
        self.assertTrue(state['day_paused'])
        state['balance'] = 9100
        update_risk_state(state, 3*MINUTE, cfg)
        self.assertTrue(state['dd_paused'])
        update_risk_state(state, 86_400_000, cfg)
        self.assertFalse(state['day_paused'])
        self.assertTrue(state['dd_paused'])

    def test_store_atomic_rollback_and_complete_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3', Config())
            def fail(state):
                state['balance'] = 0
                raise RuntimeError('simulated failure')
            with self.assertRaises(RuntimeError):
                store.transact(fail)
            self.assertEqual(store.snapshot()['balance'], 10000)
            errors = []
            def worker(worker_id):
                try:
                    for j in range(20):
                        def mutate(state):
                            state['balance'] += 1
                            return None, [{'id': f'{worker_id}_{j}', 'net_pnl': 1}]
                        store.transact(mutate)
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])
            self.assertEqual(store.snapshot()['balance'], 10080)
            self.assertEqual(len(store.trades()), 80)
            store.close()
            reopened = Store(Path(directory)/'test.sqlite3', Config())
            self.assertEqual(len(reopened.trades()), 80)
            reopened.close()

    def test_duplicate_ledger_insert_rolls_back_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3', Config())
            def mutation(s):
                s['balance'] += 1
                return None, [{'id': 'same'}]
            store.transact(mutation)
            with self.assertRaises(Exception):
                store.transact(mutation)
            self.assertEqual(store.snapshot()['balance'], 10001)
            store.close()


class RuntimeAndReplay(unittest.TestCase):
    def test_scan_deduplicates_closed_candle_and_schedules_future_entry(self):
        cfg = Config()
        now = 100 * TF_MS['15m'] + 2000
        raw = pd.DataFrame([bar(99*TF_MS['15m'], duration=TF_MS['15m'])])
        class FakeClient:
            def history(self, *args): return raw
            def now_ms(self): return now
            def spread_bps(self, *args): return 1.
        class FakeTelegram:
            def notify(self, *args): pass
        signal = dict(side='LONG', atr=1., reference=100., signal_ts=int(raw.iloc[0].close_ts),
                      neighbor_mean_r=.2, neighbor_win_fraction=.6, neighbors=15)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3', cfg)
            runtime = Runtime(store, cfg, FakeClient(), FakeTelegram())
            with patch.object(bot, 'SYMBOLS', ('BTCUSDT',)), patch('bot.predict_knn', return_value=(signal, '')):
                runtime.scan(now)
                runtime.scan(now)
            state = store.snapshot()
            self.assertEqual(state['evaluated'].get('15m'), 1)
            self.assertEqual(len(state['pending']), 1)
            self.assertGreater(next(iter(state['pending'].values()))['entry_ts'], now + 1000)
            store.close()

    def test_pause_cancels_pending_but_keeps_open_positions(self):
        cfg = Config()
        class FakeTelegram: pass
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3', cfg)
            def seed(s):
                s['pending']['BTC'] = {'tf': '15m'}
                s['positions']['dummy'] = {'placeholder': True}
                return None, []
            store.transact(seed)
            Runtime(store, cfg, client=object(), telegram=FakeTelegram()).handle_action('TOGGLE_SIGNALS')
            state = store.snapshot()
            self.assertFalse(state['enabled'])
            self.assertFalse(state['pending'])
            self.assertIn('dummy', state['positions'])
            store.close()

    def test_multisymbol_backfill_is_chronological_and_restart_idempotent(self):
        cfg = Config(market='usdm', fee_bps=0, slippage_bps=0)
        class FakeClient:
            def klines_range(self, symbol, tf, start, end):
                return pd.DataFrame([bar(i*MINUTE) for i in range(2) if i*MINUTE >= start])
            def funding(self, *args): return []
        class FakeTelegram:
            def notify(self, *args): pass
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3', cfg)
            def seed(s):
                for symbol in ('BTCUSDT', 'ETHUSDT'):
                    p = position(cfg=cfg)
                    p.symbol = symbol
                    s['positions'][p.id] = dataclasses.asdict(p)
                return None, []
            store.transact(seed)
            runtime = Runtime(store, cfg, FakeClient(), FakeTelegram())
            seen = []
            original = bot.settle_bar
            def capture(state, pid, candle, *args, **kwargs):
                seen.append((candle['ts'], state['positions'][pid]['symbol']))
                return original(state, pid, candle, *args, **kwargs)
            with patch('bot.settle_bar', side_effect=capture):
                runtime.monitor(2*MINUTE+1000)
                runtime.monitor(2*MINUTE+1000)
            self.assertEqual([x[0] for x in seen], [0, 0, MINUTE, MINUTE])
            store.close()

    def test_monitor_keeps_scheduled_entry_before_any_notification(self):
        cfg = Config(fee_bps=0, slippage_bps=0)
        class FakeClient:
            def klines_range(self, *args): return pd.DataFrame([bar(0, h=104, l=99)])
            def funding(self, *args): return []
        telegram = bot.Telegram()
        telegram.admins = {1}
        telegram.token = ''
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3', cfg)
            def seed(s):
                s['pending']['BTCUSDT_15m'] = dict(symbol='BTCUSDT', tf='15m', side='LONG',
                                                   atr=1., reference=100., entry_ts=0)
                return None, []
            store.transact(seed)
            with patch.object(telegram, 'send', side_effect=RuntimeError('offline')):
                Runtime(store, cfg, FakeClient(), telegram).monitor(MINUTE+1000)
            self.assertEqual(len(store.trades()), 1)
            self.assertFalse(store.snapshot()['positions'])
            self.assertFalse(store.snapshot()['pending'])
            store.close()

    def test_backtest_end_position_and_fees_not_dropped(self):
        cfg = Config()
        n = 1050 * 15
        minutes = pd.DataFrame([bar(i*MINUTE, h=100.05, l=99.95) for i in range(n)])
        def last_only(frame, index, tf, config):
            if index == 1048:
                return dict(side='LONG', atr=1., reference=100., signal_ts=int(frame.iloc[index].close_ts)), ''
            return None, 'test_no_signal'
        with patch('backtest.predict_knn', side_effect=last_only):
            stats, ledger, curve = run_backtest(minutes, '15m', 'BTCUSDT', cfg)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['exit_reason'], 'END_OF_DATA')
        self.assertLess(stats['return_pct'], 0)
        self.assertAlmostEqual(stats['final_equity'], 10000 + ledger[0]['net_pnl'])
        self.assertAlmostEqual(curve[-1]['equity'], stats['final_equity'])

    def test_usdm_backtest_cannot_assume_zero_funding(self):
        with self.assertRaisesRegex(ValueError, 'funding'):
            run_backtest(pd.DataFrame(), '15m', 'BTCUSDT', Config(market='usdm'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
