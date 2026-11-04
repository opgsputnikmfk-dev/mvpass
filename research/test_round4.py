import unittest
import numpy as np
import pandas as pd
import search_round4 as s
import research_futures as r
class ShockTests(unittest.TestCase):
    def fixture(self):
        returns=np.array([.01,-.01]*10+[.02]+[.003,-.004]*20)
        rows=[];price=100.
        for day,ret in enumerate(returns):
            for j in range(96):
                o=price*np.exp(ret*j/96);c=price*np.exp(ret*(j+1)/96)
                rows.append([o,max(o,c)*1.001,min(o,c)*.999,c,100.])
            price*=np.exp(ret)
        frame=pd.DataFrame(rows,columns=['open','high','low','close','volume'],index=(np.arange(len(rows))+1)*15*r.MIN-1);frame.index.name='bucket'
        return {'features':frame}
    def test_previous_twenty_days_exclude_signal_day(self):
        x=self.fixture();x['features']=x['features'].iloc[:21*96]
        out=s.make_signals(x,'shock_momentum',1.9)
        self.assertEqual(list(out),[21*1440*r.MIN+r.MIN])
        self.assertEqual(next(iter(out.values()))['side'],1)
        self.assertFalse(s.make_signals(x,'shock_momentum',2.1))
    def test_future_and_partial_day_do_not_change_past(self):
        x=self.fixture();cut=35*96+21;prefix={'features':x['features'].iloc[:cut].copy()}
        changed={'features':x['features'].copy()};changed['features'].iloc[cut:,:4]*=4
        for kind,param in s.CANDIDATES:
            a=s.make_signals(prefix,kind,param);b=s.make_signals(changed,kind,param)
            self.assertTrue(a)
            cutoff=prefix['features'].index[-1]+1+r.MIN
            self.assertEqual(a,{k:v for k,v in b.items() if k<=cutoff})
            self.assertTrue(all(k%(1440*r.MIN)==r.MIN for k in a))
    def test_reversal_mirrors_momentum(self):
        x=self.fixture();a=s.make_signals(x,'shock_momentum',1.5);b=s.make_signals(x,'shock_reversal',1.5)
        self.assertEqual(a.keys(),b.keys())
        for t in a:
            self.assertEqual(a[t]['side'],-b[t]['side'])
            self.assertAlmostEqual(a[t]['stop']+b[t]['stop'],2*a[t]['reference'])
if __name__=='__main__':unittest.main()
