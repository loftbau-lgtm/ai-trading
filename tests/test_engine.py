import tempfile, unittest
from unittest.mock import patch
from engine import Engine, SYMBOLS, STRATEGIES, signal

def bar(t,p=100): return {'time':t,'end':t+59999,'open':p,'high':p+1,'low':p-1,'close':p}
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=self.tmp.name+'/db.sqlite'; self.e=Engine(self.path)
        self.e.bootstrap({s:[bar(i*60000) for i in range(120)] for s in SYMBOLS})
    def tearDown(self): self.e.db.close(); self.tmp.cleanup()
    def tick(self,i=120,p=100): return self.e.process({s:bar(i*60000,p) for s in SYMBOLS},(i+1)*60000)
    def test_duplicate_and_restart(self):
        with patch('engine.signal',return_value='BUY'): self.tick()
        before=self.e.accounts(); self.assertFalse(self.tick()); self.assertEqual(before,self.e.accounts())
        self.e.db.close(); self.e=Engine(self.path)
        self.assertFalse(self.tick()); self.assertEqual(before,self.e.accounts())
        self.tick(121); self.assertEqual(self.e.cursor(),121*60000)
    def test_gap_and_open_candle(self):
        with self.assertRaises(ValueError): self.tick(122)
        with self.assertRaises(ValueError): self.e.process({s:bar(120*60000) for s in SYMBOLS},120*60000+30000)
        self.assertEqual(self.e.cursor(),119*60000)
    def test_buy_sell_accounting(self):
        with patch('engine.signal',return_value='BUY'): self.tick()
        for a in self.e.accounts():
            self.assertAlmostEqual(a['cash'],100*.75**3)
            self.assertEqual(len(a['positions']),3)
            self.assertAlmostEqual(a['pnl'],a['realized']+a['unrealized'])
        first=self.e.db.execute('SELECT * FROM trades ORDER BY id LIMIT 1').fetchone()
        self.assertAlmostEqual(first['qty']*first['price']+first['fee'],25)
        with patch('engine.signal',return_value='SELL'): self.tick(121,110)
        for a in self.e.accounts():
            self.assertEqual(a['positions'],[]); self.assertGreater(a['realized'],0)
            self.assertAlmostEqual(a['pnl'],a['realized']); self.assertEqual(a['trades'],6)
    def test_atomic_rollback(self):
        original=self.e.execute
        def fail(strategy,*args):
            if strategy==STRATEGIES[2]: raise RuntimeError('crash')
            original(strategy,*args)
        with patch('engine.signal',return_value='BUY'),patch.object(self.e,'execute',side_effect=fail):
            with self.assertRaises(RuntimeError): self.tick()
        self.assertEqual(self.e.cursor(),119*60000)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM trades').fetchone()[0],0)
        self.assertTrue(all(a['cash']==100 for a in self.e.accounts()))
    def test_no_short_or_pyramiding(self):
        with patch('engine.signal',return_value='SELL'): self.tick()
        self.assertTrue(all(a['trades']==0 for a in self.e.accounts()))
        with patch('engine.signal',return_value='BUY'): self.tick(121); self.tick(122)
        self.assertTrue(all(a['trades']==3 for a in self.e.accounts()))
    def test_deterministic_replay(self):
        other=Engine(self.tmp.name+'/other.sqlite')
        other.bootstrap({s:[bar(i*60000) for i in range(120)] for s in SYMBOLS})
        for i in range(120,145):
            bars={s:bar(i*60000,100+(i%10)) for s in SYMBOLS}
            self.e.process(bars,(i+1)*60000);other.process(bars,(i+1)*60000)
        self.assertEqual(self.e.accounts(),other.accounts());other.db.close()
    def test_signals(self):
        flat=[bar(i*60000) for i in range(120)]
        for name in STRATEGIES: self.assertIn(signal(name,flat),('HOLD','SELL'))
        up=[bar(i*60000,100+i) for i in range(120)]
        self.assertEqual(signal('Momentum',up),'BUY')
        self.assertEqual(signal('RSI mean reversion',up),'SELL')
if __name__=='__main__': unittest.main()
