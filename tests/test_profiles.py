import copy,json,os,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
from analyst.auth import password_hash,load_users,NamedAuth
from analyst.engine import Engine
from analyst.storage import Store
from analyst.profiles import EngineProfiles
from analyst.web import make_server
from test_core import config,candidate

class ProfileTests(unittest.TestCase):
    def test_auth_fail_closed(self):
        with self.assertRaises(ValueError):load_users('{"Sasha":{"password_hash":"plain","role":"admin"}}')
        auth=NamedAuth({'Admin':{'password_hash':password_hash('one'),'role':'admin'}})
        self.assertIsNone(auth.authenticate('Admin','wrong'));self.assertIsNone(auth.authenticate('unknown','one'))
        self.assertEqual(auth.authenticate('admin','one'),'Admin')
    def test_private_http_and_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            a=Engine(Store(Path(root)/'a.db'),config(),True);s=Engine(Store(Path(root)/'s.db',market_store=a.store),config(),True)
            records={n:{'password_hash':password_hash(p),'role':r} for n,p,r in [('Admin','owner','admin'),('Sasha','friend','user')]}
            runner=EngineProfiles({'Admin':a,'Sasha':s});self.assertFalse(s.notifications_enabled);self.assertTrue(a.notifications_enabled)
            a.store.commit_cycle(a.dump(),[candidate(cid='private')],[],[])
            with patch.dict(os.environ,{'DASHBOARD_PASSWORD':'legacy-password','PUBLIC_ORIGIN':''}):server=make_server(a,port=0,profiles=runner.profiles,auth_records=records)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def request(path,data=None,cookie='',account=''):
                h={'X-Requested-With':'Analyst','Cookie':cookie,'X-Account':account}
                return urllib.request.urlopen(urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers=h),timeout=5)
            try:
                with self.assertRaises(urllib.error.HTTPError):request('/api/login',{'password':'legacy-password'})
                ca=request('/api/login',{'username':'Admin','password':'owner'}).headers['Set-Cookie'].split(';')[0]
                cs=request('/api/login',{'username':'Sasha','password':'friend'}).headers['Set-Cookie'].split(';')[0]
                self.assertEqual(json.load(request('/api/state',cookie=cs))['user']['username'],'Sasha')
                cfg=copy.deepcopy(s.config);cfg['filters']['rsi']='on'
                with self.assertRaises(urllib.error.HTTPError):request('/api/config',cfg,cs,'Admin')
                request('/api/config',cfg,cs,'Sasha');self.assertEqual(a.config['filters']['rsi'],'off')
                self.assertEqual(Engine(s.store,config(),True).config['filters']['rsi'],'on')
                request('/api/pause',{},cs,'Sasha');self.assertFalse(a.config['paused']);self.assertTrue(s.config['paused'])
                self.assertFalse(json.load(request('/api/state',cookie=cs))['candidates'])
                with self.assertRaises(urllib.error.HTTPError):request('/api/chart?candidate=private',cookie=cs)
                with self.assertRaises(urllib.error.HTTPError):request('/api/export?account=unknown',cookie=cs)
                request('/api/logout',{},cs)
                with self.assertRaises(urllib.error.HTTPError):request('/api/state',cookie=cs)
                self.assertEqual(json.load(request('/api/state',cookie=ca))['user']['telegram_linked'],True)
                with patch.object(a,'collect_market',return_value={}) as collect,patch.object(a,'refresh_top20'),patch.object(a,'apply_market') as aa,patch.object(s,'apply_market') as ss:
                    runner.cycle();self.assertEqual(collect.call_count,1);aa.assert_called_once();ss.assert_called_once()
            finally:
                server.shutdown();server.server_close();thread.join();a.store.db.close();s.store.db.close()
