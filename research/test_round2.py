import unittest
import numpy as np
import pandas as pd
import research_futures as r
import search_round2 as search

class Round2Tests(unittest.TestCase):
    def test_protocol_has_ordered_date_ranges(self):
        for period in [search.PLAN['development'],search.PLAN['selection']]+search.PLAN['unseen_periods']:
            self.assertIsInstance(period,list)
            self.assertEqual(len(period),2)
            self.assertLess(r.ms(period[0]),r.ms(period[1]))
    def test_trailing_stop_only_effective_after_its_candle(self):
        start=r.ms('2026-07-01');n=1440
        minute=np.array([[start+i*r.MIN,100.,101.,99.,100.] for i in range(n)])
        minute[1,1:]=[100,105,99,104]
        minute[2,1:]=[104,104.5,103.5,104]
        minute[3,1:]=[104,104.5,102,103]
        data={'BTCUSDT':{'minute':minute,'funding':{},'features':pd.DataFrame(),'trailing':{start+2*r.MIN-1:.5}}}
        schedule={'BTCUSDT':{start+r.MIN:{'side':1,'reference':100.,'atr':1.,'stop':98.,'target':None}}}
        result=r.simulate(data,'channel_4h',20,'2026-07-01','2026-07-02',execution_bps=0,schedules_override=schedule,trailing_multiple=3,include_trades=True)
        trade=result['trade_records'][0]
        self.assertEqual(trade['exit_ts'],start+4*r.MIN-1)
        self.assertGreater(trade['net'],0)
    def test_higher_frame_signals_do_not_use_future_partial_bars(self):
        rng=np.random.default_rng(33);n=2000;c=100*np.exp(np.cumsum(rng.normal(0,.005,n)))
        frame=pd.DataFrame({'open':c,'close':c,'high':c+1,'low':c-1,'volume':100.},index=(np.arange(n)+1)*15*r.MIN-1)
        frame.index.name='bucket'
        prefix=frame.iloc[:1600].copy();future=frame.copy();future.iloc[1600:,:4]*=2
        for kind,param in search.CANDIDATES:
            expected,_=search.make_signals(prefix,kind,param)
            actual,_=search.make_signals(future,kind,param)
            cutoff=prefix.index[-1]+1+r.MIN
            self.assertEqual(expected,{k:v for k,v in actual.items() if k<=cutoff})

if __name__=='__main__':unittest.main()
