import copy,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from analyst.engine import Engine
from analyst.execution import Ledger
from analyst.storage import Store
from analyst.models import MINUTE
from test_core import config,candidate,bar,META

class PositionSearchTests(unittest.TestCase):
    def test_reject_same_symbol_and_duplicate_pending(self):
        a=Ledger(config());self.assertTrue(a.propose(candidate())[0])
        self.assertFalse(a.propose(candidate(cid='second',side=-1))[0])
        a.step(2*MINUTE,{'BTCUSDT':bar()},META)
        self.assertFalse(a.propose(candidate(cid='third'))[0]);self.assertFalse(a.pending)
        p=next(iter(a.positions.values()));a.close(p,100,3*MINUTE,'TIMEOUT')
        self.assertTrue(a.propose(candidate(cid='after-close',ts=4*MINUTE))[0])
    def test_saved_limit_upgrade_is_one_time_and_keeps_open_trade(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/'a.db');c=config();c['risk']['max_positions']=3;e=Engine(store,c,True)
            a=e.accounts[e.generation+':active'];a.propose(candidate());a.step(2*MINUTE,{'BTCUSDT':bar()},META)
            e.upgrade_position_limit();self.assertEqual(e.config['risk']['max_positions'],10)
            self.assertEqual(len(a.positions),1);self.assertIn('BTCUSDT',e.occupied_symbols())
            c=copy.deepcopy(e.config);c['risk']['max_positions']=3;e.update_config(c)
            e=Engine(store,config(),True);e.upgrade_position_limit();self.assertEqual(e.config['risk']['max_positions'],3)
            store.db.close()
    def test_scanner_skips_occupied_coin_across_series_resumes_after_close(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/'a.db');e=Engine(store,config(),True);a=e.accounts[e.generation+':active']
            a.propose(candidate());a.step(2*MINUTE,{'BTCUSDT':bar()},META)
            c=copy.deepcopy(e.config);c['filters']['rsi']='on';e.update_config(c)
            end=int(time.time()*1000)//MINUTE*MINUTE
            def bundle(end):
                candles={s:pd.DataFrame([{'ts':end-MINUTE,**bar()}]) for s in c['symbols']}
                frames={s:{5:pd.DataFrame([{'close_ts':end,'close':100}])} for s in c['symbols']}
                return dict(end=end,now=end,symbols=c['symbols'],candles=candles,frames=frames,metadata=META,snaps={},events={})
            e.last_ts=end-MINUTE
            with patch('analyst.engine.analyze',return_value=([],{'status':'WAIT'})) as analyze:
                e.apply_market(bundle(end));self.assertNotIn('BTCUSDT',[x.args[0] for x in analyze.call_args_list])
                self.assertEqual(e.market['BTCUSDT']['status'],'POSITION_OPEN')
                self.assertIn('ETHUSDT',[x.args[0] for x in analyze.call_args_list])
            a.close(next(iter(a.positions.values())),100,end,'TIMEOUT');end+=5*MINUTE;e.last_ts=end-MINUTE
            with patch('analyst.engine.analyze',return_value=([],{'status':'WAIT'})) as analyze:
                e.apply_market(bundle(end));self.assertIn('BTCUSDT',[x.args[0] for x in analyze.call_args_list])
            store.db.close()
