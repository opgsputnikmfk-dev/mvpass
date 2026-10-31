"""Offline safety and behavior tests: python -m unittest -v. No external writes."""
import copy
import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import bot

M = bot.MINUTE

def candle(ts=0, o=100., h=101., l=99., c=100., duration=M):
    return dict(ts=ts, open=o, high=h, low=l, close=c, volume=100., close_ts=ts+duration-1)


def setup(side='LONG'):
    return dict(symbol='BTCUSDT', tf='5m', side=side, atr=1., reference=100.,
                stop=98. if side == 'LONG' else 102., obstacle=None, signal_ts=0,
                setup_id='example', strategy_id=bot.STRATEGY_ID, entry_ts=M,
                signal_features={key: .1 for key in bot.FEATURES}, related_symbols=[])


def prepared_frames(side='LONG'):
    # Known geometry, independent of EMA calculations: isolate pattern rules.
    end = 300*bot.TF_MS['1h']
    asof = end+bot.TF_MS['5m']-1
    frames = {}
    for tf, n in [('1h', 300), ('15m', 100), ('5m', 100)]:
        dt = bot.TF_MS[tf]
        stop = end+dt if tf == '5m' else end
        raw = pd.DataFrame([candle(stop-(n-i)*dt, 106., 107., 105., 106., dt) for i in range(n)])
        raw['ema200'] = 100.
        raw['ema50'] = np.linspace(104., 105., n)
        raw['ema20'] = 106.
        raw['atr'] = 2. if tf != '5m' else 1.
        for k in bot.FEATURES: raw[k] = .5
        frames[tf] = raw
    frames['1h']['close'] = 110.
    frames['1h']['high'] = 111.
    frames['1h']['ema50'] = np.linspace(105., 109., 300)
    frames['1h'].loc[296:, 'ema50'] = [108., 108.3, 108.6, 109.]
    pull = frames['15m']
    pull.loc[88, 'low'] = 100.
    pull.loc[95, 'high'] = 115.
    entry = frames['5m']
    entry.loc[99, ['open','high','low','close']] = [107.2,108.2,106.7,108.]
    if side == 'SHORT':
        for raw in frames.values():
            hi, lo = raw.high.copy(), raw.low.copy()
            for key in ['open','close','ema200','ema50','ema20']: raw[key] = 200-raw[key]
            raw['high'], raw['low'] = 200-lo, 200-hi
    return frames, asof


class StrategyTests(unittest.TestCase):
    def evaluate(self, frames, asof):
        with patch.object(bot, 'build_feature_frame', side_effect=lambda raw:raw):
            return bot.evaluate_setup(frames, asof, bot.Config())

    def test_mirrored_long_and_short(self):
        for side in ('LONG', 'SHORT'):
            frames, asof = prepared_frames(side)
            signal, reason, context = self.evaluate(frames, asof)
            self.assertEqual(reason, '')
            self.assertEqual(signal['side'], side)
            self.assertEqual(context['stage'], 3)
            self.assertEqual(signal['signal_ts'], asof)

    def test_future_and_forming_candles_cannot_change_signal(self):
        frames, asof = prepared_frames()
        expected = self.evaluate(frames, asof)
        extended = copy.deepcopy(frames)
        for tf, raw in extended.items():
            future = raw.iloc[-1].copy()
            future['ts'] += bot.TF_MS[tf]
            future['close_ts'] += bot.TF_MS[tf]
            future['high'] = future['close'] = future['open'] = 10000
            extended[tf] = pd.concat([raw,pd.DataFrame([future])], ignore_index=True)
        self.assertEqual(expected, self.evaluate(extended, asof))

    def test_incomplete_hour_does_not_supply_trend(self):
        frames, asof = prepared_frames()
        frames['1h'] = frames['1h'].iloc[:-1]
        with self.assertRaisesRegex(ValueError, 'Stale 1h'):
            self.evaluate(frames, asof)

    def test_gap_is_data_error_not_no_signal(self):
        frames, asof = prepared_frames()
        frames['5m'] = frames['5m'].drop(index=80)
        with self.assertRaisesRegex(ValueError, 'Missing'):
            self.evaluate(frames, asof)

    def test_flat_hour_waits_for_trend(self):
        frames, asof = prepared_frames()
        frames['1h']['ema50'] = 105.
        self.assertEqual(self.evaluate(frames, asof)[1], 'trend_unclear')

    def test_break_of_pullback_structure_rejected(self):
        frames, asof = prepared_frames()
        frames['15m'].loc[98, 'low'] = 99.
        self.assertEqual(self.evaluate(frames, asof)[1], 'structure_broken')

    def test_without_break_of_three_candles_waits(self):
        frames, asof = prepared_frames()
        frames['5m'].loc[99, ['open','high','low','close']] = [106.,106.8,105.8,106.5]
        signal, reason, stage = self.evaluate(frames, asof)
        self.assertIsNone(signal)
        self.assertEqual(reason,'trigger_wait')
        self.assertEqual(stage['stage'],2)

    def test_known_resistance_before_target_blocks_signal(self):
        frames, asof = prepared_frames()
        frames['15m'].loc[95, 'high'] = 112.
        self.assertEqual(self.evaluate(frames, asof)[1], 'target_obstacle')

    def test_pivot_requires_right_confirmation(self):
        raw = pd.DataFrame({'high':[1,2,5,3]})
        self.assertEqual(bot.confirmed_swings(raw,'high'), [])
        raw.loc[4] = [2]
        self.assertEqual(bot.confirmed_swings(raw,'high'), [(2,5.)])

    def test_features_causal_with_real_calculation(self):
        rng = np.random.default_rng(5)
        prices = 100+np.cumsum(rng.normal(0,.1,400))
        raw = pd.DataFrame([candle(i*M,p,p+1,p-1,p) for i,p in enumerate(prices)])
        before = bot.build_feature_frame(raw.iloc[:300].copy())
        raw.loc[300:, ['open','high','low','close']] *= 10
        after = bot.build_feature_frame(raw)
        pd.testing.assert_frame_equal(before,after.iloc[:300])


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = bot.Config(fee_bps=0,slippage_bps=0)

    def position(self,side='LONG'):
        return bot.make_setup_position('BTCUSDT',setup(side),100.,M,1.,self.cfg)

    def test_full_single_target_in_both_directions(self):
        for side in ('LONG','SHORT'):
            p = self.position(side)
            bot.step_position(p,candle(M,h=105,l=99) if side=='LONG' else candle(M,h=101,l=95))
            self.assertEqual(p.exit_reason,'TP')
            self.assertEqual(p.remaining,0)
            self.assertEqual(len(p.fills),2)
            self.assertAlmostEqual(p.net_pnl,4)
            self.assertAlmostEqual(p.net_pnl/p.planned_risk,2)

    def test_stop_wins_ambiguous_bar_both_sides(self):
        for side in ('LONG','SHORT'):
            p = self.position(side)
            bot.step_position(p,candle(M,h=110,l=90))
            self.assertEqual(p.exit_reason,'SL')
            self.assertEqual(p.ambiguous_bars,1)
            self.assertAlmostEqual(p.net_pnl,-2)

    def test_no_fill_from_preentry_or_repeated_bar(self):
        p = self.position()
        bot.step_position(p,candle(0,h=110,l=90))
        bot.step_position(p,candle(M,h=101,l=99))
        bot.step_position(p,candle(M,h=110,l=90))
        self.assertIsNone(p.exit_reason)

    def test_gap_stop_fills_at_worse_open(self):
        p=self.position()
        bot.step_position(p,candle(M,o=95,h=96,l=94,c=95))
        self.assertEqual(p.net_pnl,-5)

    def test_timeout_closes_whole_position(self):
        p=self.position(); p.deadline=2*M-1
        bot.step_position(p,candle(M,c=100.5))
        self.assertEqual(p.exit_reason,'TIMEOUT')
        self.assertAlmostEqual(p.net_pnl,.5)

    def test_fees_and_slippage_reduce_target_return(self):
        cfg=bot.Config()
        for side in ('LONG','SHORT'):
            p=bot.make_setup_position('BTCUSDT',setup(side),100,M,1,cfg)
            bot.step_position(p,candle(M,h=105,l=99) if side=='LONG' else candle(M,h=101,l=95))
            self.assertEqual(p.exit_reason,'TP')
            self.assertGreater(p.fees,0)
            self.assertLess(p.net_pnl/p.planned_risk,2)

    def test_tiny_stop_rejected_after_costs(self):
        x=setup();x['stop']=99.9;x['atr']=.1
        with self.assertRaisesRegex(ValueError,'costs_too_high'):
            bot.make_setup_position('BTCUSDT',x,100,M,1,bot.Config())

    def test_entry_gap_cancels_without_account_change(self):
        s=bot.new_state(self.cfg);before=copy.deepcopy(s)
        p,reason=bot.open_paper_position(s,setup(),candle(M,o=101,h=102,l=100,c=101),self.cfg)
        self.assertIsNone(p);self.assertEqual(reason,'entry_gap')
        self.assertEqual(s['balance'],before['balance'])
        self.assertFalse(s['positions'])

    def test_account_balance_realized_pnl_reconciles(self):
        cfg=bot.Config();s=bot.new_state(cfg)
        p,reason=bot.open_paper_position(s,setup(),candle(M),cfg)
        self.assertEqual(reason,'')
        trade=bot.settle_bar(s,p.id,candle(2*M,h=110,l=99),[],cfg)
        self.assertAlmostEqual(s['balance'],cfg.initial_equity+trade['net_pnl'])
        self.assertAlmostEqual(bot.account_equity(s),s['balance'])

    def test_pending_slots_are_reserved(self):
        s=bot.new_state(self.cfg)
        for i in range(3):s['pending'][str(i)]=dict(setup(),symbol=f'COIN{i}')
        self.assertEqual(bot.portfolio_gate(s,'ETHUSDT','5m','SHORT',M,self.cfg),'position_limit')

    def test_correlated_positions_share_risk_cap(self):
        cfg=bot.Config(max_position_notional=.8)
        s=bot.new_state(cfg)
        first=setup(); first['stop']=99.;first['atr']=.5
        p,reason=bot.open_paper_position(s,first,candle(M),cfg)
        self.assertEqual(reason,'')
        second=dict(first,symbol='ETHUSDT',related_symbols=['BTCUSDT'])
        q,reason=bot.open_paper_position(s,second,candle(M),cfg)
        self.assertEqual(reason,'')
        self.assertLess(q.planned_risk,p.planned_risk)
        self.assertLessEqual(p.planned_risk+q.planned_risk,10000*cfg.correlated_risk_cap+.001)

    def test_risk_pause_stays_on_after_manual_enable(self):
        s=bot.new_state(self.cfg);s['balance']=9000
        bot.update_risk_state(s,M,self.cfg)
        s['enabled']=True
        self.assertEqual(bot.portfolio_gate(s,'BTCUSDT','5m','LONG',M,self.cfg),'account_loss_limit')


class DiagnosticTests(unittest.TestCase):
    def test_knn_uses_only_already_closed_non_overlapping_new_trades(self):
        query={k:.1 for k in bot.FEATURES}
        trades=[dict(strategy_id=bot.STRATEGY_ID,side='LONG',entry_ts=i*10,exit_ts=i*10+5,
                     signal_features=query,net_pnl=-1.,net_r=-1.) for i in range(16)]
        score=bot.neighbor_diagnostic(trades,query,'LONG',150,neighbors=15)
        self.assertEqual(score['samples'],15)
        self.assertEqual(score['mean_r'],-1.)
        self.assertEqual(bot.neighbor_diagnostic(trades,query,'LONG',100,neighbors=15)['samples'],0)
        for t in trades:t['entry_ts']=0
        self.assertEqual(bot.neighbor_diagnostic(trades,query,'LONG',1000,neighbors=15)['samples'],0)

    def test_negative_knn_does_not_veto_entry(self):
        # The production scanner records this score without consulting a threshold.
        frames,asof=prepared_frames()
        with tempfile.TemporaryDirectory() as directory:
            store=bot.Store(Path(directory)/'a.db',bot.Config())
            client=Mock();client.now_ms.return_value=asof+2000;client.spread_bps.return_value=1;client.frames={}
            telegram=Mock();rt=bot.Runtime(store,bot.Config(),client,telegram)
            with patch.object(bot,'build_feature_frame',side_effect=lambda raw:raw),patch.object(bot,'neighbor_diagnostic',return_value={'samples':15,'mean_r':-1}):
                rt.scan_one('BTCUSDT','5m',asof+2000,frames)
            self.assertEqual(len(store.snapshot()['pending']),1)
            self.assertEqual(telegram.notify.call_count,1)
            rt.scan_one('BTCUSDT','5m',asof+2000,frames)
            self.assertEqual(telegram.notify.call_count,1)
            store.close()


class StoreAndTelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=bot.Config()
        self.store=bot.Store(Path(self.tmp.name)/'a.db',self.cfg)
        self.tg=Mock(admins={8299008675},token='test')
        self.tg.edit_panel.return_value=True
        self.tg.call.return_value={'message_id':99}
        self.rt=bot.Runtime(self.store,self.cfg,Mock(),self.tg)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def update(self,action='MENU',uid=1):
        return {'update_id':uid,'callback_query':{'id':'c','data':action,'from':{'id':8299008675},
            'message':{'message_id':99,'chat':{'id':8299008675}}}}

    def test_callbacks_edit_same_message_never_create_another(self):
        for i,action in enumerate(['MENU','SHOW_LIVE_TRADES','AN_ALL','SHOW_REJECTS','STRATEGY','STATUS','HELP']):
            self.rt.process_update(self.update(action,i+1))
        self.assertEqual(self.tg.edit_panel.call_count,7)
        self.tg.call.assert_not_called()
        self.tg.send.assert_not_called()

    def test_missing_callback_panel_does_not_spam(self):
        self.tg.edit_panel.return_value=False
        self.rt.process_update(self.update())
        self.tg.call.assert_not_called()

    def test_repeated_start_reuses_panel(self):
        self.store.transact(lambda s:(s['panel_messages'].update({'8299008675':99}),[]))
        self.rt.process_update({'update_id':1,'message':{'text':'/start','from':{'id':8299008675},'chat':{'id':8299008675}}})
        self.tg.edit_panel.assert_called_once()
        self.tg.call.assert_not_called()

    def test_unauthorized_sender_cannot_toggle(self):
        u=self.update('SIGNALS_OFF');u['callback_query']['from']['id']=1
        self.rt.process_update(u)
        self.assertTrue(self.store.snapshot()['enabled'])
        self.tg.edit_panel.assert_not_called()

    def test_duplicate_update_does_not_repeat_side_effects(self):
        u=self.update('SIGNALS_OFF')
        self.rt.process_update(u);self.rt.process_update(u)
        self.assertEqual(self.tg.edit_panel.call_count,1)

    def test_pause_cancels_pending_and_keeps_open_positions(self):
        pending=setup();p=bot.make_setup_position('BTCUSDT',pending,100,M,1,self.cfg)
        def populate(s):s['positions'][p.id]=dataclasses.asdict(p);s['pending']['x']=pending;return None,[]
        self.store.transact(populate)
        self.rt.process_update(self.update('SIGNALS_OFF'))
        state=self.store.snapshot()
        self.assertFalse(state['pending']);self.assertEqual(len(state['positions']),1)

    def test_native_copy_buttons_copy_exact_fixed_price(self):
        p=bot.make_setup_position('BTCUSDT',setup(),100,M,1,self.cfg)
        rows=bot.copy_levels_keyboard(dataclasses.asdict(p))
        self.assertEqual(rows[0][0]['copy_text']['text'],bot.price(p.entry))
        self.assertNotIn('callback_data',rows[0][0])
        self.assertEqual(bot.price(.000001),'0.000001')

    def test_alert_copy_buttons_available(self):
        tg=bot.Telegram();tg.call=Mock()
        p=bot.make_setup_position('BTCUSDT',setup(),100,M,1,self.cfg)
        tg.send(8299008675,bot.entry_message(p))
        data=tg.call.call_args.args[1]
        self.assertEqual(data['parse_mode'],'HTML')
        self.assertEqual(len(data['reply_markup']['inline_keyboard']),2)

    def test_all_dashboard_pages_fit_telegram_and_valid_callbacks(self):
        for action in ['MENU','STATUS','STRATEGY','HELP','SHOW_REJECTS','AN_ALL','DETAILS_ALL','CONTROLS','RISK','SHOW_HISTORY']:
            text,markup=self.rt.make_panel(action)
            self.assertLess(len(text.encode('utf-16-le'))//2,4096)
            for row in markup['inline_keyboard']:
                for button in row:
                    if 'callback_data' in button:self.assertLessEqual(len(button['callback_data'].encode()),64)

    def test_html_and_csv_escape_external_text(self):
        self.assertIn('&lt;script&gt;',bot.panel_html('<script>'))
        exported=bot.ledger_csv([dict(id='=CMD()',symbol='BTCUSDT',exit_ts=1)])
        self.assertIn(b"'=CMD()",exported)

    def test_sqlite_rollback_and_restart(self):
        def broken(s):s['balance']=1;raise RuntimeError('fail')
        with self.assertRaises(RuntimeError):self.store.transact(broken)
        self.assertEqual(self.store.snapshot()['balance'],10000)
        second=bot.Store(Path(self.tmp.name)/'a.db',self.cfg)
        self.assertEqual(second.snapshot()['balance'],10000);second.close()

    def test_new_ledger_leaves_old_db_and_keeps_menu_identity(self):
        old=Path(self.tmp.name)/'paper_spot_signals.sqlite3'
        db=bot.Store(old,self.cfg)
        db.transact(lambda s:(s.update(balance=8500,panel_messages={'8299008675':777},last_update_id=500),[]));db.close()
        before=old.read_bytes()
        new=bot.open_signal_store(self.tmp.name,self.cfg)
        self.assertEqual(new.snapshot()['balance'],10000)
        self.assertEqual(new.snapshot()['panel_messages'],{'8299008675':777})
        self.assertEqual(new.snapshot()['last_update_id'],500)
        new.close();self.assertEqual(old.read_bytes(),before)


class MarketTests(unittest.TestCase):
    def test_old_market_environment_cannot_select_futures_blocked_api(self):
        with patch.dict(os.environ,{'MARKET':'usdm'},clear=True):
            client=bot.MarketClient(bot.Config.from_env().market)
            self.assertEqual(client.base,'https://data-api.binance.vision')
            with patch.object(client,'get',side_effect=AssertionError('no funding HTTP')):
                self.assertEqual(client.funding('BTCUSDT',0,100),[])

    def test_only_closed_candles_returned(self):
        rows=[[0,'100','101','99','100','10',M-1,0,0,0,0,0],
              [M,'100','101','99','100','10',2*M-1,0,0,0,0,0]]
        self.assertEqual(len(bot.frame_from_klines(rows,M)),1)

    def test_universe_filters_status_volume_spread_and_stables(self):
        client=bot.MarketClient()
        listing=[dict(symbol=s,baseAsset=s[:-4],quoteAsset='USDT',status='TRADING',isSpotTradingAllowed=True)
                 for s in ['BTCUSDT','USDCUSDT','ETHUSDT','SOLUSDT']]
        quotes=[dict(symbol=s,bidPrice='100',askPrice='100.01' if s!='SOLUSDT' else '105') for s in ['BTCUSDT','USDCUSDT','ETHUSDT','SOLUSDT']]
        tickers=[dict(symbol=s,quoteVolume=volume) for s,volume in [('BTCUSDT',1e8),('USDCUSDT',1e9),('ETHUSDT',1),('SOLUSDT',1e8)]]
        client.get=Mock(side_effect=[{'symbols':listing},quotes,tickers])
        self.assertEqual(client.liquid_universe(bot.Config()),['BTCUSDT'])
        self.assertEqual(client.liquid_universe(bot.Config()),['BTCUSDT'])
        self.assertEqual(client.get.call_count,3)


class RuntimeIntegrationTests(unittest.TestCase):
    def test_scan_schedules_future_minute_and_reused_pullback_is_blocked(self):
        frames,asof=prepared_frames()
        cfg=bot.Config();store=bot.Store(':memory:',cfg)
        client=Mock();client.frames={};client.now_ms.return_value=asof+2000;client.spread_bps.return_value=1.
        rt=bot.Runtime(store,cfg,client,Mock())
        try:
            with patch.object(bot,'build_feature_frame',side_effect=lambda raw:raw):
                rt.scan_one('BTCUSDT','5m',asof+2000,frames)
                state=store.snapshot();pending=next(iter(state['pending'].values()))
                self.assertGreater(pending['entry_ts'],asof+3000)
                self.assertEqual(pending['entry_ts']%M,0)
                signal,_,context=bot.evaluate_setup(frames,asof,cfg)
            store.transact(lambda s:(s['pending'].clear(),[]))
            client.now_ms.return_value=asof+bot.TF_MS['5m']+2000
            with patch.object(bot,'evaluate_setup',return_value=(signal,'',context)):
                rt.scan_one('BTCUSDT','5m',client.now_ms(),frames)
            state=store.snapshot()
            self.assertFalse(state['pending'])
            self.assertEqual(state['rejects']['5m']['setup_used'],1)
        finally:store.close()

    def test_closed_minute_monitor_is_chronological_and_idempotent(self):
        cfg=bot.Config(fee_bps=0,slippage_bps=0);store=bot.Store(':memory:',cfg)
        class Client:
            def klines_range(self,symbol,tf,start,end):
                return pd.DataFrame([candle(i*M) for i in range(1,3) if i*M>=start])
            def funding(self,*args):return []
        def seed(s):
            for symbol in ('BTCUSDT','ETHUSDT'):
                p=bot.make_setup_position(symbol,setup(),100,M,1,cfg)
                s['positions'][p.id]=dataclasses.asdict(p)
            return None,[]
        store.transact(seed);rt=bot.Runtime(store,cfg,Client(),Mock())
        seen=[];original=bot.settle_bar
        def capture(s,pid,bar,*args,**kwargs):
            seen.append((bar['ts'],s['positions'][pid]['symbol']))
            return original(s,pid,bar,*args,**kwargs)
        try:
            with patch.object(bot,'settle_bar',side_effect=capture):
                rt.monitor(3*M+1000)
                rt.monitor(3*M+1000)
            self.assertEqual(seen,[(M,'BTCUSDT'),(M,'ETHUSDT'),(2*M,'BTCUSDT'),(2*M,'ETHUSDT')])
        finally:store.close()

    def test_missing_position_bar_blocks_monitoring_before_any_mutation(self):
        cfg=bot.Config();store=bot.Store(':memory:',cfg)
        p=bot.make_setup_position('BTCUSDT',setup(),100,M,1,cfg)
        store.transact(lambda s:(s['positions'].update({p.id:dataclasses.asdict(p)}),[]))
        client=Mock();client.klines_range.return_value=pd.DataFrame([candle(2*M)])
        rt=bot.Runtime(store,cfg,client,Mock());before=store.snapshot()
        try:
            with self.assertRaisesRegex(ValueError,'first unprocessed'):
                rt.monitor(3*M+1000)
            self.assertEqual(store.snapshot(),before)
        finally:store.close()

if __name__=='__main__':unittest.main()
