import unittest
from unittest.mock import Mock, patch
from scanner import Scanner, active_markets, rank_markets


def ticker(symbol, **changes):
    return dict(symbol=symbol,lastPrice='100',openPrice='100',highPrice='110',lowPrice='95',
                quoteVolume='1000000',bidPrice='99.99',askPrice='100.01',priceChangePercent='2',
                count=2000,closeTime=120000,**changes) if not changes else {**ticker(symbol),**changes}


class ScannerTests(unittest.TestCase):
    def test_discover_only_active_spot_across_all_quotes(self):
        info={'symbols':[dict(symbol='ETHBTC',baseAsset='ETH',quoteAsset='BTC',status='TRADING',isSpotTradingAllowed=True),
                         dict(symbol='OLDUSDT',baseAsset='OLD',quoteAsset='USDT',status='BREAK',isSpotTradingAllowed=True),
                         dict(symbol='FUTUSDT',baseAsset='FUT',quoteAsset='USDT',status='TRADING',isSpotTradingAllowed=False)]}
        self.assertEqual(list(active_markets(info)),['ETHBTC'])

    def test_rank_with_spread_penalty_and_tied_percentiles(self):
        catalog={s:dict(symbol=s,base=s,quote='USDT') for s in ('A','B','C')}
        ranked=rank_markets(catalog,[ticker('A'),ticker('B'),ticker('C',bidPrice='99',askPrice='101')],120000)
        values={r['symbol']:r for r in ranked}
        self.assertEqual(values['A']['score'],values['B']['score'])
        self.assertGreater(values['A']['score'],values['C']['score'])
        self.assertEqual(values['A']['rangePct'],15)

    def test_quote_groups_do_not_mix_volume_units(self):
        catalog={'A':dict(symbol='A',base='A',quote='BTC'),'B':dict(symbol='B',base='B',quote='USDT')}
        ranked=rank_markets(catalog,[ticker('A',quoteVolume='1'),ticker('B',quoteVolume='999999999')],120000)
        self.assertEqual(ranked[0]['score'],ranked[1]['score'])

    def test_invalid_stale_unknown_and_zero_volume_are_excluded(self):
        catalog={s:dict(symbol=s,base=s,quote='USDT') for s in ('A','B','C','D','E','F')}
        rows=[ticker('A',lastPrice='nan'),ticker('B',quoteVolume='0'),ticker('C',closeTime=-999999),
              ticker('D',count=0),ticker('E',highPrice='90'),ticker('F',lastPrice='bad'),ticker('UNKNOWN')]
        self.assertEqual(rank_markets(catalog,rows,120000),[])

    def test_missing_book_has_zero_score(self):
        catalog={'A':dict(symbol='A',base='A',quote='USDT')}
        row=rank_markets(catalog,[ticker('A',bidPrice='0')],120000)[0]
        self.assertIsNone(row['spreadPct'])
        self.assertEqual(row['score'],0)

    def test_refresh_retains_catalog_but_hides_stale_ranking(self):
        info={'symbols':[dict(symbol='ETHBTC',baseAsset='ETH',quoteAsset='BTC',status='TRADING',isSpotTradingAllowed=True)]}
        get=Mock(side_effect=[info,[ticker('ETHBTC')],RuntimeError('secret upstream detail')])
        scanner=Scanner(get,Mock())
        with patch('scanner.time.time',return_value=120):
            scanner.refresh()
            self.assertTrue(scanner.snapshot()['fresh'])
            scanner.refresh()
            data=scanner.snapshot()
            self.assertFalse(data['fresh'])
            self.assertEqual(data['rows'],[])
            self.assertEqual(data['activeCount'],1)
            self.assertNotIn('secret',str(data))

    def test_chart_allowlist_closed_candles_and_cache(self):
        bars=[dict(time=i*60000,end=i*60000+59999,open=100,high=101,low=99,close=100) for i in range(120)]
        get=Mock(return_value={'serverTime':7200000})
        fetch=Mock(return_value=bars)
        scanner=Scanner(get,fetch)
        scanner.catalog={'ETHBTC':dict(symbol='ETHBTC',base='ETH',quote='BTC')}
        self.assertEqual(scanner.market('../order')[1],404)
        get.assert_not_called()
        with patch('scanner.time.time',return_value=7200):
            data,code=scanner.market('ETHBTC')
            self.assertEqual(code,200)
            self.assertTrue(data['signals']['fresh'])
            self.assertEqual(len(data['signals']['items']),6)
            self.assertEqual(scanner.market('ETHBTC')[1],200)
            self.assertEqual(fetch.call_count,1)
            self.assertEqual(fetch.call_args.args,('ETHBTC',0,7199999))

    def test_chart_failures_do_not_return_old_or_partial_data(self):
        scanner=Scanner(Mock(return_value={'serverTime':7200000}),Mock(return_value=[]))
        scanner.catalog={'A':dict(symbol='A',base='A',quote='USDT')}
        data,code=scanner.market('A')
        self.assertEqual(code,502)
        self.assertNotIn('bars',data)
        self.assertEqual(scanner.market('A')[1],429)


if __name__=='__main__': unittest.main()
