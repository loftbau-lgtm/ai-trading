import io
import os
import unittest
from unittest.mock import patch

from futures_external_agent import external_decision, validate_decision


class Response:
    status=200
    def __init__(self, body): self.stream=io.BytesIO(body)
    def __enter__(self): return self
    def __exit__(self,*_): return False
    def read(self, size): return self.stream.read(size)


class FuturesExternalTests(unittest.TestCase):
    def test_strict_json_rejects_unbounded_or_live_orders(self):
        self.assertIsNone(validate_decision({'action':'LIVE_ORDER','symbol':'BTCUSDT'}))
        self.assertIsNone(validate_decision({'action':'OPEN_LONG','symbol':'BTCUSDT',
            'positionSide':'LONG','desiredSize':float('inf'),'stop':90,'target':110}))
        self.assertIsNone(validate_decision({'action':'OPEN_LONG','symbol':'BTCUSDT',
            'positionSide':'LONG','desiredSize':10,'stop':90,'target':110,'leverage':5}))
        self.assertIsNone(validate_decision({'action':'REDUCE_LONG','symbol':'BTCUSDT',
            'positionSide':'LONG','desiredSize':.9}))

    def test_timeout_and_invalid_response_fall_back(self):
        env={'AGENT_PROVIDER':'EXTERNAL','AGENT_API_URL':'https://example.test/agent',
             'AGENT_API_KEY':'test-key','AGENT_TIMEOUT':'1'}
        with patch.dict(os.environ,env):
            self.assertIsNone(external_decision({'paperOnly':True},
                opener=lambda request,timeout: (_ for _ in ()).throw(TimeoutError())))
            self.assertIsNone(external_decision({'paperOnly':True},
                opener=lambda request,timeout: Response(b'not JSON')))
            self.assertEqual(external_decision({'paperOnly':True},
                opener=lambda request,timeout: Response(b'{"action":"FLAT","symbol":null}'))['action'],
                'FLAT')


if __name__=='__main__': unittest.main()
