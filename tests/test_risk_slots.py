import copy,tempfile,unittest
from pathlib import Path
from analyst.execution import Ledger
from analyst.engine import Engine
from analyst.storage import Store
from analyst.models import MINUTE
from test_core import config,candidate,bar

class RiskSlotTests(unittest.TestCase):
    def ledger(self):
        c=config();c['symbols']=[f'COIN{i}USDT' for i in range(11)];return Ledger(c)
    def step_entries(self,a,tight=False):
        for symbol in a.config['symbols']:
            c=candidate(symbol=symbol,cid=symbol)
            if tight:c.stop=99.8
            a.propose(c)
        meta={s:dict(tick_size=.01,qty_step=.001,min_qty=.001,min_notional=1) for s in a.config['symbols']}
        a.step(2*MINUTE,{s:bar(h=100.1,l=99.99,c=100) for s in meta},meta)
    def test_ten_same_direction_entries_with_original_aggregate_caps(self):
        a=self.ledger();self.step_entries(a)
        self.assertEqual(len(a.positions),10)
        self.assertTrue(any('Лимит позиций' in r['reason'] for r in a.rejections))
        eq=a.equity();risk=sum(p.planned_risk for p in a.positions.values())
        self.assertLessEqual(risk,eq*.005+.01)
        self.assertLessEqual(sum(p.qty*p.entry for p in a.positions.values()),eq)
        self.assertTrue(all(p.planned_risk<=5 for p in a.positions.values()))
    def test_tight_stop_does_not_consume_future_slots_capital(self):
        a=self.ledger();self.step_entries(a,tight=True)
        self.assertEqual(len(a.positions),10)
        self.assertTrue(all(p.entry*p.qty<=1000 for p in a.positions.values()))
        self.assertLessEqual(sum(p.entry*p.qty for p in a.positions.values()),a.equity()+.01)
    def test_existing_large_positions_reserve_budget_after_config_change(self):
        old=self.ledger();c=candidate(symbol='COIN0USDT',cid='old')
        old.config['risk']['max_positions']=3;old.config['risk']['risk_pct']=.0025
        meta=dict(tick_size=.01,qty_step=.001,min_qty=.001,min_notional=1)
        old._open(c,bar(),meta,2*MINUTE);p=next(iter(old.positions.values()))
        fresh=self.ledger();fresh.last_prices={p.symbol:100}
        self.assertIsNotNone(fresh._open(candidate(symbol=p.symbol,cid='duplicate'),bar(),meta,3*MINUTE,[p]))
        fresh.config['risk']['max_side_risk_pct']=.0025
        reason=fresh._open(candidate(symbol='COIN1USDT',cid='other'),bar(),meta,3*MINUTE,[p])
        total=p.planned_risk+sum(x.planned_risk for x in fresh.positions.values())
        self.assertLessEqual(total,25+.01)
    def test_minimum_order_never_increases_quantity_over_budget(self):
        a=self.ledger();meta=dict(tick_size=.01,qty_step=.001,min_qty=100,min_notional=10000)
        reason=a._open(candidate(),bar(),meta,2*MINUTE)
        self.assertIn('минимального объёма',reason);self.assertFalse(a.positions)
    def test_upgrade_preserves_stricter_caps_and_open_positions(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/'x.db');c=config();c['risk']['risk_pct']=.0002;c['risk']['max_side_risk_pct']=.002;c['risk']['max_open_risk_pct']=.003
            e=Engine(store,c,True);e.upgrade_risk_allocation()
            self.assertEqual(e.config['risk']['max_open_risk_pct'],.003);self.assertEqual(e.config['risk']['risk_pct'],.0002)
            c=copy.deepcopy(e.config);c['risk']['risk_pct']=.0001;e.update_config(c);e.upgrade_risk_allocation()
            self.assertEqual(e.config['risk']['risk_pct'],.0001);store.db.close()
