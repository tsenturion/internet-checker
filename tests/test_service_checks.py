import copy
import json
import logging
import os
import threading
import time
import unittest
from unittest import mock
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import main


class ProbeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        status = 200
        body = b"Microsoft Connect Test"
        if path == "/portal":
            body = b"<html>Sign in to Wi-Fi</html>"
        elif path == "/redirect":
            status, body = 302, b""
        elif path == "/blocked":
            status, body = 403, b"Access denied"
        elif path == "/no-content":
            status, body = 204, b""
        elif path == "/v1/models":
            self.server.received_authorization = self.headers.get("Authorization")
            status = getattr(self.server, "api_status", 200)
            body = b'{"object": "list", "data": [{"id": "test-model"}]}'
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        if status == 302:
            self.send_header("Location", "/ok")
        self.end_headers()
        self.wfile.write(body)

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
        cls.proxy_environment = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"})
        cls.proxy_environment.start()
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
        cls.proxy_environment.stop()

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

    def test_tray_groups_partial_chatgpt_access(self):
        state = main.NetworkState(True, "Italy", "IT", (
            main.ServiceStatus("chatgpt", "ChatGPT", True),
            main.ServiceStatus("chatgpt-init", "ab.chatgpt.com", False),
            main.ServiceStatus("telegram", "Telegram Desktop", True),
            main.ServiceStatus("openai-api", "OpenAI API", None, detail="ключ не указан", notify=False),
        ), datetime.now())
        lines = main.tray_status_lines(main.StatusSnapshot(state, False, state.checked_at, None))
        self.assertIn("ChatGPT: ab.chatgpt.com OFFLINE", lines)
        self.assertFalse(any(line.startswith("ab.chatgpt.com:") for line in lines))
        self.assertFalse(any(line.startswith(("Telegram Desktop:", "OpenAI API:")) for line in lines))
        self.assertEqual(lines[0], "Italy")
        self.assertNotIn("ONLINE", " | ".join(lines))

    def test_connectivity_rejects_portal_redirect_and_block(self):
        for path in ("/portal", "/redirect", "/blocked"):
            with self.subTest(path=path):
                probe = {"url": self.probe_for(path)["url"], "expected_status": 200,
                         "expected_body": "Microsoft Connect Test"}
                self.assertFalse(main.check_connectivity([probe], 1.0, 1))
        self.assertTrue(main.check_connectivity([
            {"url": self.probe_for("/ok")["url"], "expected_status": 200,
             "expected_body": "Microsoft Connect Test"},
        ], 1.0, 1))
        self.assertFalse(main.check_connectivity([
            {"url": self.probe_for("/ok")["url"], "expected_status": 200},
        ], 1.0, 1))
        self.assertTrue(main.check_connectivity([
            {"url": self.probe_for("/no-content")["url"], "expected_status": 204},
        ], 1.0, 1))

    def test_missing_network_does_not_fall_back_to_tcp(self):
        with mock.patch("main.requests.get", side_effect=main.requests.ConnectionError):
            self.assertFalse(main.check_connectivity(main.DEFAULT_CONFIG["connectivity_urls"], 0.2, 1))

    def test_api_checks_only_model_catalog_and_rejects_unauthorized(self):
        url = self.probe_for("/v1/models")["url"]
        probe = {"url": url, "method": "GET", "api_key_env": "OPENAI_API_KEY"}
        with mock.patch.object(main, "OPENAI_MODELS_URL", url), mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            self.server.api_status = 200
            self.assertTrue(main.check_service_probe(probe, 1.0)[0])
            self.assertEqual(self.server.received_authorization, "Bearer test-only")
            self.server.api_status = 401
            self.assertFalse(main.check_service_probe(probe, 1.0)[0])
        self.server.api_status = 200

    def test_api_never_sends_credentials_to_other_urls_or_generation(self):
        probe = {"url": main.OPENAI_MODELS_URL, "method": "POST", "api_key_env": "OPENAI_API_KEY"}
        with mock.patch("main.requests.request") as request:
            self.assertFalse(main.check_service_probe(probe, 1.0)[0])
            probe.update(url="https://example.invalid/v1/models", method="GET")
            self.assertFalse(main.check_service_probe(probe, 1.0)[0])
            request.assert_not_called()

    def test_api_redirect_is_not_followed(self):
        url = self.probe_for("/redirect")["url"]
        with mock.patch.object(main, "OPENAI_MODELS_URL", url), mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            self.assertFalse(main.check_service_probe({
                "url": url, "method": "GET", "api_key_env": "OPENAI_API_KEY",
            }, 1.0)[0])


if __name__ == "__main__":
    unittest.main()
