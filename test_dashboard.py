import csv
import io
import unittest
import dashboard
from bot import Config, new_state, account_equity, report, Store, Runtime, keyboard


def trade(i, pnl, ts=1000000):
    return dict(id=str(i), net_pnl=pnl, exit_ts=ts, tf='15m', side='LONG', symbol='BTCUSDT', fees=1,
                funding_paid=0, exit_reason='STOP', tp1_hit=False)


class DashboardTests(unittest.TestCase):
    def test_metrics(self):
        m=dashboard.metrics([trade(1,10),trade(2,-2),trade(3,-3),trade(4,0)])
        self.assertEqual((m['net'],m['pf'],m['streak'],m['current_streak']), (5,'2.00',2,0))
        self.assertEqual(m['winrate'],25)
        self.assertEqual(m['avg_loss'],-2.5)

    def test_empty_and_no_losses(self):
        self.assertEqual(dashboard.metrics([])['pf'],'—')
        self.assertIn('∞', dashboard.metrics([trade(1,3)])['pf'])
        self.assertIn('Закрытых позиций: 0',dashboard.analytics([]))

    def test_period_boundary_future_sort(self):
        now=10*86400
        rows=[trade(1,1,now*1000),trade(2,1,(now-86400)*1000),trade(3,1,(now+1)*1000)]
        self.assertEqual([t['id'] for t in dashboard.select(rows,'TODAY',now)],['1'])
        self.assertEqual([t['id'] for t in dashboard.select(rows,'7D',now)],['2','1'])

    def test_csv(self):
        t=trade('=HYPERLINK("test")',-4)
        data=dashboard.ledger_csv([t]).decode('utf-8-sig')
        row=list(csv.DictReader(io.StringIO(data)))[0]
        self.assertTrue(row['id'].startswith("'="))
        self.assertEqual(row['net_pnl'],'-4')

    def test_overview_reconciliation(self):
        cfg=Config(); s=new_state(cfg)
        text=dashboard.overview(s,cfg,account_equity(s))
        self.assertIn('10,000.00',text)
        self.assertIn('v2.1',text)
        self.assertIn('Просадка сейчас / максимум: 0.00% / 0.00%',text)

    def test_routes_readonly(self):
        store=Store(':memory:',Config())
        runtime=Runtime(store,Config(),client=object(),telegram=object())
        before=store.snapshot()
        for action in ['MENU','SHOW_STATS','AN_TODAY','AN_7D','AN_30D','AN_ALL','DETAILS','STATUS']:
            self.assertTrue(runtime.handle_action(action))
        self.assertEqual(before,store.snapshot())
        self.assertNotIn('TOGGLE_SIGNALS',str(keyboard()))
        store.close()

if __name__ == '__main__':
    unittest.main()
