import gzip
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import main
import network_metrics as metrics
from network_metrics_ui import overview_text


class MetricsHandler(BaseHTTPRequestHandler):
    payload = b"ChatGPT " * 10000
    compressed = gzip.compress(payload)

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/gzip")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.compressed if self.path == "/gzip" else b"response-body"
        self.send_response(200)
        if self.path == "/gzip":
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.last_body = self.rfile.read(int(self.headers["Content-Length"]))
        body = b'{"feature_gates": {}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NetworkMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.collector = metrics.TrafficCollector()
        metrics.configure_metrics(self.collector)
        self.proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"})
        self.proxy.start()

    def tearDown(self):
        metrics.configure_metrics(None)
        self.proxy.stop()

    def test_compressed_bytes_and_redirect_counted_once(self):
        self.assertTrue(main.check_service_probe({"url": self.url + "/redirect", "must_contain": "ChatGPT"}, 2)[0])
        snapshot = self.collector.snapshot()
        total = snapshot["totals"]
        self.assertEqual(total["rx_body"], len(MetricsHandler.compressed))
        self.assertLess(total["rx_body"], len(MetricsHandler.payload))
        self.assertEqual(total["requests"], 1)
        self.assertEqual(total["redirects"], 1)
        self.assertEqual(total["successes"], 1)
        self.assertEqual(snapshot["active"], 0)
        self.assertGreater(total["tx_headers"], 0)

    def test_post_body_is_measured_and_credentials_are_not_stored(self):
        secret = "секрет-который-нельзя-сохранять"
        body = {"key": secret, "feature_gates": {}}
        self.assertTrue(main.check_service_probe({"url": self.url + "/post?secret=hidden", "method": "POST",
                                                 "json": body, "must_contain": "feature_gates"}, 2)[0])
        snapshot = self.collector.snapshot()
        self.assertEqual(snapshot["totals"]["tx_body"], len(self.server.last_body))
        saved = json.dumps(snapshot["session"], ensure_ascii=False)
        self.assertNotIn(secret, saved)
        self.assertNotIn("hidden", saved)

    def test_failed_validation_and_timeout_keep_counters(self):
        self.assertFalse(main.check_service_probe({"url": self.url + "/plain", "must_contain": "missing"}, 2)[0])
        with mock.patch("network_metrics.requests.request", side_effect=main.requests.Timeout):
            self.assertFalse(main.check_service_probe({"url": self.url + "/plain"}, 2)[0])
        total = self.collector.snapshot()["totals"]
        self.assertEqual(total["requests"], 2)
        self.assertEqual(total["failures"], 2)
        self.assertEqual(total["timeouts"], 1)
        self.assertEqual(total["rx_body"], len(b"response-body"))
        self.assertEqual(total["unmeasured"], 1)

    def test_protocol_and_partial_receives_are_measured(self):
        @metrics.tracked_probe("Сервис")
        def receive(url, partial=False):
            metrics.record_protocol_bytes(tx=42)
            metrics.record_protocol_bytes(rx=25)
            if partial:
                return False, "TimeoutError"
            metrics.record_protocol_bytes(rx=64)
            return True, "resPQ"
        self.assertTrue(receive("mtproto://149.154.167.51:443")[0])
        self.assertFalse(receive("mtproto://149.154.167.51:443", True)[0])
        total = self.collector.snapshot()["totals"]
        self.assertEqual((total["rx"], total["tx"]), (114, 84))
        self.assertEqual(total["timeouts"], 1)

    def test_parallel_probes_have_independent_measurements(self):
        @metrics.tracked_probe("Сервис")
        def probe(url):
            metrics.record_protocol_bytes(rx=10, tx=5)
            time.sleep(0.002)
            return True, "ok"
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(probe, ["mtproto://test"] * 40))
        snapshot = self.collector.snapshot()
        self.assertEqual(snapshot["totals"]["requests"], 40)
        self.assertEqual((snapshot["totals"]["rx"], snapshot["totals"]["tx"]), (400, 200))
        self.assertEqual(snapshot["active"], 0)

    def test_retention_restart_and_invalid_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network-metrics.json"
            collector = metrics.TrafficCollector(path)
            old_day = (datetime.now().date() - timedelta(days=30)).isoformat()
            collector.daily[old_day] = {"old": metrics.empty_totals()}
            collector.finish("Сервис · test", metrics.Measurement(rx_body=500), True, 0.5)
            collector.save()
            restored = metrics.TrafficCollector(path).snapshot()
            self.assertNotIn(old_day, restored["days"])
            self.assertEqual(restored["totals"]["rx"], 0)
            today = datetime.now().date().isoformat()
            self.assertEqual(restored["days"][today]["Сервис · test"]["rx"], 500)
            self.assertIn("500 Б", overview_text(restored))
            for invalid in ('[]', '{"version": 1, "days": {"wrong": {}}}'):
                path.write_text(invalid, encoding="utf-8")
                with self.assertLogs("internet_checker", level="WARNING"):
                    self.assertEqual(metrics.TrafficCollector(path).snapshot()["days"], {})

    def test_rates_expire_and_overview_handles_empty_data(self):
        clock = mock.Mock(return_value=0.0)
        collector = metrics.TrafficCollector(clock=clock)
        clock.return_value = 10.0
        collector.finish("test", metrics.Measurement(rx_body=1000, tx_body=500), True, 0.5)
        snapshot = collector.snapshot()
        self.assertEqual((snapshot["rx_rate"], snapshot["tx_rate"]), (100, 50))
        clock.return_value = 71.0
        self.assertEqual(collector.snapshot()["rx_rate"], 0)
        self.assertIn("Трафик", " ".join(metrics.summary_lines(snapshot)))
        self.assertIn("Попыток проверки: 0", overview_text(metrics.TrafficCollector().snapshot()))


if __name__ == "__main__":
    unittest.main()
