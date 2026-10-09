import importlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import urlopen, Request
from unittest.mock import patch

from market_data import DEFAULT_HOSTS, MarketDataUnavailable, PublicMarketDataClient


class AdaptiveMatrixRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = str(Path(cls.directory.name) / "quantlab.sqlite3")
        with patch.dict(os.environ, {"DATABASE_PATH": database_path}):
            app = importlib.import_module("server")
        cls.app = app
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.directory.cleanup()

    def test_adaptive_matrix_javascript_is_served(self):
        with urlopen(self.base_url + "/adaptive_matrix.js", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers.get_content_type())
            self.assertTrue(response.read())

    def test_adaptive_matrix_api_is_paper_only_with_30_variants(self):
        with urlopen(self.base_url + "/api/adaptive-matrix", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            data = json.load(response)
        self.assertEqual(data["mode"], "PAPER")
        self.assertEqual(data["variantCount"], 30)

    def test_futures_research_read_only_routes_are_available(self):
        for route in ('/api/edge-lab/status','/api/edge-lab/candidates',
                      '/api/edge-lab/rankings','/api/autonomous-agent',
                      '/api/portfolio-risk'):
            with self.subTest(route=route), urlopen(self.base_url+route,timeout=10) as response:
                self.assertEqual(response.status,200)
                self.assertEqual(response.headers.get_content_type(),'application/json')
                self.assertEqual(json.load(response)['mode'],'PAPER_ONLY' if route!=
                    '/api/portfolio-risk' else 'BINANCE_USDS_M_FUTURES_PAPER')

    def test_paper_pause_control_requires_token(self):
        endpoint=self.base_url+'/api/paper/control'
        payload=b'{"action":"PAUSE_ALL"}'
        with patch.dict(os.environ,{'PAPER_CONTROL_TOKEN':'test-control-token'}):
            with self.assertRaises(HTTPError) as unauthorized:
                urlopen(Request(endpoint,data=payload,method='POST'),timeout=10)
            self.assertEqual(unauthorized.exception.code,401)
            headers={'Authorization':'Bearer test-control-token',
                     'Content-Type':'application/json'}
            with urlopen(Request(endpoint,data=payload,headers=headers,method='POST'),timeout=10) as response:
                self.assertTrue(json.load(response)['pausedAll'])
            with urlopen(Request(endpoint,data=b'{"action":"RESUME"}',
                                 headers=headers,method='POST'),timeout=10) as response:
                self.assertFalse(json.load(response)['pausedAll'])

    def test_worker_watchdog_restarts_dead_worker(self):
        watcher_stop=threading.Event()
        calls=[]
        def crashing_futures():
            calls.append(time.monotonic())
            raise RuntimeError('synthetic worker death')
        with patch.object(self.app,'stop',watcher_stop), \
             patch.object(self.app,'worker',lambda:None), \
             patch.object(self.app,'watch_worker',lambda:None), \
             patch.object(self.app,'futures_worker',crashing_futures), \
             patch.object(self.app,'futures_edge_worker',lambda:None), \
             patch.object(self.app.scanner,'run',lambda event:None), \
             patch.object(self.app.adaptive,'run',lambda event:None), \
             patch.object(self.app.microstructure,'run',lambda event:None):
            thread=threading.Thread(target=self.app.futures_watchdog,daemon=True)
            thread.start()
            deadline=time.monotonic()+7
            while len(calls)<2 and time.monotonic()<deadline:
                time.sleep(.1)
            watcher_stop.set()
            thread.join(timeout=4)
        self.assertGreaterEqual(len(calls),2)
        self.assertFalse(thread.is_alive())

    def test_edge_lab_is_read_only_and_paper_only(self):
        before = self.app.engine.db.total_changes
        with urlopen(self.base_url + "/", timeout=10) as response:
            page = response.read()
            self.assertIn(b'id="edge-lab"', page)
            self.assertIn(b'/edge_lab.js', page)
        with urlopen(self.base_url + "/edge_lab.js", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers.get_content_type())
            self.assertIn(b"/api/edge-lab", response.read())
        with urlopen(self.base_url + "/api/edge-lab", timeout=10) as response:
            self.assertEqual(response.status, 200)
            data = json.load(response)
        self.assertEqual(data["mode"], "PAPER_ONLY")
        self.assertTrue(data["noConfirmedEdge"])
        self.assertEqual(data["activePaperStrategies"], 0)
        self.assertEqual(len(data["candidates"]), 38)
        self.assertEqual(self.app.engine.db.total_changes, before)

    def test_runtime_blocks_new_entries(self):
        self.assertFalse(self.app.engine.entry_allowed("SMA 9/21"))
        self.assertFalse(self.app.adaptive.entry_enabled)
        self.assertFalse(self.app.adaptive.directional.entry_enabled)
        self.assertEqual(self.app.adaptive.agent.promoted, frozenset())

    def test_autonomous_agent_api_and_asset(self):
        with urlopen(self.base_url + "/portfolio_agent.js", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers.get_content_type())
        with urlopen(self.base_url + "/api/portfolio-agent", timeout=10) as response:
            self.assertEqual(response.status, 200)
            data = json.load(response)
        self.assertEqual(data["mode"], "PAPER_ONLY")
        self.assertFalse(data["liveReady"])
        self.assertFalse(data["manualTradeApproval"])
        self.assertEqual(data["promotedFamilies"], [])

    def test_futures_paper_is_isolated_and_read_only(self):
        before = self.app.futures.db.total_changes
        with urlopen(self.base_url + "/futures_paper.js", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers.get_content_type())
        with urlopen(self.base_url + "/api/futures-paper", timeout=10) as response:
            self.assertEqual(response.status, 200)
            data = json.load(response)
        self.assertEqual(data["mode"], "BINANCE_USDS_M_FUTURES_PAPER")
        self.assertTrue(data["paperOnly"])
        self.assertFalse(data["liveReady"])
        self.assertEqual(data["grossExposure"], 0)
        self.assertTrue(data["noConfirmedEdge"])
        self.assertEqual(self.app.futures.db.total_changes,before)

    def test_market_data_health_is_read_only(self):
        before = self.app.market_client.snapshot()
        with urlopen(self.base_url + "/api/market-data/health", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            data = json.load(response)
        self.assertEqual(data, before)
        self.assertEqual(self.app.market_client.snapshot(), before)
        self.assertEqual(len(data["hosts"]), 5)

    def test_market_data_dashboard_asset_is_served(self):
        with urlopen(self.base_url + "/market_data.js", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers.get_content_type())
            self.assertIn(b"/api/market-data/health", response.read())

    def test_market_outage_preserves_paper_state_and_cursor(self):
        before_cursor = self.app.engine.cursor()
        before_accounts = self.app.engine.accounts()
        before_trades = list(self.app.engine.db.execute("SELECT * FROM trades ORDER BY id"))
        with patch.object(self.app, "public_get", side_effect=MarketDataUnavailable("ALL_HOSTS_UNAVAILABLE")):
            with self.assertRaises(MarketDataUnavailable):
                self.app.sync()
        self.assertEqual(self.app.engine.cursor(), before_cursor)
        self.assertEqual(self.app.engine.accounts(), before_accounts)
        self.assertEqual(list(self.app.engine.db.execute("SELECT * FROM trades ORDER BY id")), before_trades)

    def test_first_host_outage_keeps_runtime_api_available(self):
        def opener(request, timeout):
            if request.full_url.startswith(DEFAULT_HOSTS[0]):
                raise URLError("deliberate primary outage")
            return io.BytesIO(json.dumps({"serverTime": int(time.time() * 1000)}).encode())
        client = PublicMarketDataClient(opener=opener)
        with patch.object(self.app, "market_client", client):
            self.assertIn("serverTime", self.app.public_get("time"))
            with urlopen(self.base_url + "/api/market-data/health", timeout=10) as response:
                health = json.load(response)
            self.assertEqual(health["activeHost"], DEFAULT_HOSTS[1])
            self.assertEqual(health["failoverCount"], 1)
            for path in ("/api/scanner", "/api/adaptive", "/api/directional"):
                with urlopen(self.base_url + path, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(), "application/json")

    def test_directional_edge_health_get_does_not_write_history(self):
        db = self.app.adaptive.directional.db
        before_changes = db.total_changes
        before_state = db.execute("SELECT data FROM directional_state WHERE id=1").fetchone()[0]
        with urlopen(self.base_url + "/api/directional/edge-health", timeout=20) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            report = json.load(response)
        self.assertEqual(report["mode"], "PAPER_ONLY")
        self.assertFalse(report["liveReady"])
        self.assertEqual(db.total_changes, before_changes)
        self.assertEqual(db.execute("SELECT data FROM directional_state WHERE id=1").fetchone()[0], before_state)


if __name__ == "__main__":
    unittest.main()
