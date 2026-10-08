import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from adaptive import load_config,config_hash
from adaptive_live import BinanceLiveAdapter,LiveLocked


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=load_config()
        self.args=dict(path=Path(self.temp.name)/'live.sqlite3',config=self.config,
                      env=dict(TRADING_MODE='LIVE',ENABLE_LIVE_ORDERS='true',BINANCE_API_KEY='dummy',BINANCE_API_SECRET='dummy'),
                      confirmation='CONFIRM LIVE '+config_hash(self.config),paper_report=dict(paperGatePassed=True,trades=1000,expectancyNet=.1),
                      catalog={'BTCUSDT':dict(status='TRADING',isSpotTradingAllowed=True,quoteAsset='USDT',filters=[
                          dict(filterType='PRICE_FILTER',minPrice='0.01',maxPrice='9999999',tickSize='0.01'),
                          dict(filterType='LOT_SIZE',minQty='0.00001',maxQty='100',stepSize='0.00001'),
                          dict(filterType='MIN_NOTIONAL',minNotional='5')])},transport=Mock())
        self.adapter=None
    def tearDown(self):
        if self.adapter:self.adapter.db.close()
        self.temp.cleanup()

    def test_all_gates_block_before_network_or_database(self):
        cases=[dict(env={}),dict(confirmation='yes'),dict(paper_report={'paperGatePassed':False}),
               dict(paper_report=dict(paperGatePassed=True,trades=999,expectancyNet=1)),
               dict(paper_report=dict(paperGatePassed=True,trades=1000,expectancyNet=-1))]
        for changed in cases:
            with self.assertRaises(LiveLocked):BinanceLiveAdapter(**{**self.args,**changed})
        self.args['transport'].assert_not_called()
        self.assertFalse(self.args['path'].exists())

    def test_ambiguous_submit_never_retries_post(self):
        self.adapter=BinanceLiveAdapter(**self.args)
        self.args['transport'].side_effect=TimeoutError('secret')
        with self.assertRaises(LiveLocked):self.adapter.submit_maker('candle1','BTCUSDT','BUY',.01,10000)
        with self.assertRaises(TimeoutError):self.adapter.submit_maker('candle1','BTCUSDT','BUY',.01,10000)
        methods=[call.args[0] for call in self.args['transport'].call_args_list]
        self.assertEqual(methods,['POST','GET'])
        with self.assertRaises(LiveLocked):self.adapter.submit_maker('candle2','BTCUSDT','BUY',.01,10000)

    def test_maker_filters_and_no_market_order(self):
        self.adapter=BinanceLiveAdapter(**self.args)
        self.args['transport'].side_effect=lambda method,path,p:dict(clientOrderId=p['newClientOrderId'],status='NEW')
        self.adapter.submit_maker('one','BTCUSDT','BUY',.010001,10000.009)
        params=self.args['transport'].call_args.args[2]
        self.assertEqual(params['type'],'LIMIT_MAKER')
        self.assertEqual(params['quantity'],'0.01000')
        self.assertEqual(params['price'],'10000.00')
        with self.assertRaises(LiveLocked):self.adapter.submit_maker('two','BTCUSDT','BUY',.00001,10000)
        with self.assertRaises(LiveLocked):self.adapter.cancel('somebody-elses-order')
