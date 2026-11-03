import unittest
import numpy as np
import pandas as pd
import search_round3 as s
import research_futures as r
class FundingTests(unittest.TestCase):
    def fixture(self):
        n=4*24*5;ts=(np.arange(n)+1)*15*r.MIN-1;c=np.linspace(100,120,n)
        f=pd.DataFrame(dict(open=c,high=c+1,low=c-1,close=c,volume=np.ones(n)),index=ts)
        f.index.name='bucket'
        events={i*480:[(i*480*r.MIN,-.0004,100)] for i in range(15)}
        return {'features':f,'funding':events}
    def test_signal_after_settlement_and_future_invariance(self):
        x=self.fixture();cut=3*24*60*r.MIN
        short={'features':x['features'][x['features'].index<cut].copy(),'funding':{i:v for i,v in x['funding'].items() if v[0][0]<cut}}
        for kind,param in s.CANDIDATES:
            expected=s.make_signals(short,kind,param)
            self.assertTrue(expected)
            changed=self.fixture();changed['features'].loc[changed['features'].index>=cut,'close']*=3
            changed['funding']={i:[(t,rate if t<cut else .1,m)] for i,[(t,rate,m)] in changed['funding'].items()}
            actual=s.make_signals(changed,kind,param)
            self.assertEqual(expected,{t:v for t,v in actual.items() if t<cut})
            self.assertTrue(all((t-16*r.MIN)%(480*r.MIN)==0 for t in expected))
            self.assertTrue(all(v['side']==1 for v in expected.values()))
    def test_positive_rate_fades_short_and_confirmation_rejects_uptrend(self):
        x=self.fixture();x['funding']={i:[(t,.0004,m)] for i,[(t,_,m)] in x['funding'].items()}
        signals=s.make_signals(x,'funding_fade',3)
        self.assertTrue(signals);self.assertTrue(all(v['side']==-1 for v in signals.values()))
        self.assertFalse(s.make_signals(x,'funding_fade_confirmed',3))
    def test_no_first_event_signal(self):
        x=self.fixture();x['funding']={480:[(480*r.MIN,-.0004,100)]}
        self.assertFalse(s.make_signals(x,'funding_fade',1))
if __name__=='__main__':unittest.main()
