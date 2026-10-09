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
from urllib.error import URLError
from urllib.request import urlopen
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


if __name__ == "__main__":
    unittest.main()
