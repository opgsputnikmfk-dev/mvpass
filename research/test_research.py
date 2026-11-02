import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import research_futures as r

class ResearchTests(unittest.TestCase):
    def data(self,side=1):
        start=r.ms('2026-07-01');n=1440
        a=np.array([[start+i*r.MIN,100.,100.1,99.9,100.] for i in range(n)])
        return {'BTCUSDT':{'minute':a,'funding':{},'features':pd.DataFrame()}},start
    def run_trade(self,side=1,funding=None,gap=False):
        data,start=self.data();a=data['BTCUSDT']['minute']
        # Entry then simultaneous target/stop: conservative stop must win.
        a[2,1:]=[95,110,90,100] if gap else [100,110,90,100]
        if funding:data['BTCUSDT']['funding'][1]=[(start+r.MIN+8,*funding)]
        sig={'side':side,'reference':100.,'atr':1.,'stop':98. if side==1 else 102.,'target':104. if side==1 else 96.}
        with patch.object(r,'signals',return_value={start+r.MIN:sig}):
            return r.simulate(data,'pullback',2,'2026-07-01','2026-07-02',0.)
    def test_stop_first_both_sides_and_accounting(self):
        for side in (1,-1):
            result=self.run_trade(side)
            self.assertEqual(result['trades'],1)
            self.assertAlmostEqual(result['net_usdt'],-25.,places=6)
    def test_gap_has_larger_loss(self):
        self.assertLess(self.run_trade(gap=True)['net_usdt'],-25.)
    def test_funding_changes_sign_for_shorts(self):
        self.assertGreater(self.run_trade(1,(.001,100.))['funding_net_paid'],0)
        self.assertLess(self.run_trade(-1,(.001,100.))['funding_net_paid'],0)
    def test_future_extreme_does_not_affect_past_features_or_signals(self):
        rng=np.random.default_rng(7);n=800
        prices=100*np.exp(np.cumsum(rng.normal(0,.004,n)))
        raw=pd.DataFrame({'open':prices,'close':prices,'high':prices+1,'low':prices-1,'volume':100.},index=np.arange(n)*15*r.MIN)
        def frame(raw):
            f=r.features(raw)
            for k in ('close','ema50','ema200','slope','atr'):f['h_'+k]=f[k]
            return f
        short=frame(raw.iloc[:600].copy());raw.iloc[600:,:4]*=5;full=frame(raw)
        pd.testing.assert_frame_equal(short,full.iloc[:600])
        for kind,param in r.VARIANTS:
            a=r.signals(short,kind,param);b=r.signals(full,kind,param)
            self.assertEqual(a,{t:v for t,v in b.items() if t<=short.index[-1]+1+r.MIN})
    def test_missing_candle_rejected(self):
        d=pd.DataFrame({'open_time':[0,2*r.MIN],'close_time':[r.MIN-1,3*r.MIN-1],'open':[1.,1.],'high':[2.,2.],'low':[.5,.5],'close':[1.,1.],'volume':[1.,1.]})
        with self.assertRaises(ValueError):r.validate_minutes(d)
    def test_higher_costs_reduce_identical_trade(self):
        data,start=self.data();data['BTCUSDT']['minute'][2,1:]=[100,105,99,104]
        sig={'side':1,'reference':100.,'atr':1.,'stop':98.,'target':104.}
        with patch.object(r,'signals',return_value={start+r.MIN:sig}):
            base=r.simulate(data,'pullback',2,'2026-07-01','2026-07-02',4.5)
            stress=r.simulate(data,'pullback',2,'2026-07-01','2026-07-02',9)
        self.assertEqual(base['trades'],stress['trades']);self.assertLess(stress['net_usdt'],base['net_usdt'])

if __name__=='__main__':unittest.main()
