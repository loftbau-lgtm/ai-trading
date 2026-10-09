import importlib
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
from unittest.mock import patch


class AdaptiveMatrixRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = str(Path(cls.directory.name) / "quantlab.sqlite3")
        with patch.dict(os.environ, {"DATABASE_PATH": database_path}):
            app = importlib.import_module("server")
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


if __name__ == "__main__":
    unittest.main()
