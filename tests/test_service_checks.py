import copy
import json
import logging
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import main


class ProbeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received_payload = payload
        body = b'{"feature_gates": {}, "has_updates": true}'
        status = 403 if self.path == "/forbidden" else 200
        if self.path == "/invalid":
            body = b'<html>"feature_gates"</html>'
        if self.path == "/missing":
            body = b'{"message": "ok"}'
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if self.path == "/slow-body":
                self.wfile.write(body[:25])
                self.wfile.flush()
                time.sleep(0.5)
                self.wfile.write(body[25:])
            elif self.path == "/drip":
                for offset in range(0, len(body), 4):
                    self.wfile.write(body[offset:offset + 4])
                    self.wfile.flush()
                    time.sleep(0.04)
            elif self.path == "/longer-than-default":
                time.sleep(1.1)
                self.wfile.write(body)
            else:
                self.wfile.write(body)
        except ConnectionError:
            pass


class ServiceChecksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.probe = copy.deepcopy(next(
            service for service in main.DEFAULT_CONFIG["service_checks"] if service["id"] == "chatgpt-init"
        )["probe_urls"][0])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def probe_for(self, path):
        probe = copy.deepcopy(self.probe)
        probe["url"] = f"http://127.0.0.1:{self.server.server_port}{path}"
        return probe

    def test_success_sends_sdk_json(self):
        self.assertTrue(main.check_service([self.probe_for("/ok")], 1.0))
        self.assertEqual(self.server.received_payload, self.probe["json"])

    def test_forbidden_invalid_and_incomplete_json_are_offline(self):
        for path in ("/forbidden", "/invalid", "/missing"):
            with self.subTest(path=path):
                self.assertFalse(main.check_service([self.probe_for(path)], 1.0))

    def test_full_body_must_arrive_before_deadline(self):
        for path in ("/slow-body", "/drip"):
            with self.subTest(path=path):
                started_at = time.monotonic()
                self.assertFalse(main.check_service([self.probe_for(path)], 0.2))
                self.assertLess(time.monotonic() - started_at, 0.4)

    def test_service_timeout_can_exceed_global_default(self):
        service = {"id": "test", "name": "Тест", "timeout_seconds": 1.5,
                   "probe_urls": [self.probe_for("/longer-than-default")]}
        results = main.check_services([service], 0.2, logging.getLogger("test"))
        self.assertTrue(results["test"])

    def test_initialization_failure_is_visible_immediately(self):
        debouncer = main.StateDebouncer(1, 1, 1, 1, 3, {"chatgpt-init": 1})
        debouncer.update(True, "Italy", "IT", {"chatgpt-init": True, "chatgpt": True})
        debouncer.update(True, "Italy", "IT", {"chatgpt-init": False, "chatgpt": False})
        self.assertFalse(debouncer.stable_service_online("chatgpt-init"))
        self.assertTrue(debouncer.stable_service_online("chatgpt"))

    def test_tray_shows_initialization_status(self):
        state = main.NetworkState(True, "Italy", "IT", (
            main.ServiceStatus("chatgpt-init", "ab.chatgpt.com", False),
        ), datetime.now())
        lines = main.tray_status_lines(main.StatusSnapshot(state, False, state.checked_at, None))
        self.assertIn("ab.chatgpt.com: OFFLINE", lines)


if __name__ == "__main__":
    unittest.main()
