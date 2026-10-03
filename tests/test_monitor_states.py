import copy
import logging
import os
import socketserver
import struct
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import main


def make_state(online=True, services=()):
    return main.NetworkState(online, "Italy" if online else None, "IT" if online else None,
                             tuple(services), datetime.now())


def events_for(previous, current):
    return main.collect_events(previous, current, False, True, True, {"RU"}, {"russia"})


class MonitorStatesTest(unittest.TestCase):
    def test_general_internet_is_independent_of_country_and_services(self):
        config = copy.deepcopy(main.DEFAULT_CONFIG)
        with mock.patch("main.check_connectivity", return_value=False), \
                mock.patch("main.fetch_country", return_value=("Italy", "IT", "test")), \
                mock.patch("main.check_services", return_value={"chatgpt": True, "openai-api": True}):
            result = main.run_cycle_checks(config, logging.getLogger("test"), [])
        self.assertFalse(result[0])
        self.assertTrue(result[4]["openai-api"])
        lines = main.tray_status_lines(main.StatusSnapshot(make_state(False), False, None, None))
        self.assertEqual(lines[0], "Нет интернета")

    def test_all_chatgpt_component_states(self):
        for site in (True, False, None):
            for initialization in (True, False, None):
                with self.subTest(site=site, initialization=initialization):
                    grouped = main.group_service_statuses((
                        main.ServiceStatus("chatgpt", "ChatGPT", site),
                        main.ServiceStatus("chatgpt-init", "ab.chatgpt.com", initialization),
                        main.ServiceStatus("openai-api", "OpenAI API", False, notify=False),
                    ))
                    self.assertEqual(len(grouped), 2)
                    expected = False if False in (site, initialization) else (
                        True if site is True and initialization is True else None
                    )
                    self.assertIs(grouped[0].online, expected)
                    self.assertEqual(grouped[1].service_id, "openai-api")
                    state = make_state(services=grouped)
                    lines = main.tray_status_lines(main.StatusSnapshot(state, False, None, None))
                    tooltip = main.tray_tooltip("Internet Checker", main.StatusSnapshot(state, False, None, None))
                    self.assertNotIn("ONLINE", " | ".join(lines) + tooltip)
                    self.assertNotIn("UNKNOWN", " | ".join(lines) + tooltip)
                    self.assertEqual(any(line.startswith("ChatGPT:") for line in lines), expected is False)
                    self.assertIn("OpenAI API: OFFLINE", lines)
                    healthy = make_state(services=[main.ServiceStatus("telegram", "Telegram Desktop", True)])
                    self.assertEqual(main.snapshot_text(healthy), "Italy")

    def test_api_failure_has_no_notification(self):
        previous = make_state(services=[main.ServiceStatus("openai-api", "OpenAI API", True)])
        current = make_state(services=[main.ServiceStatus("openai-api", "OpenAI API", False)])
        self.assertEqual(events_for(previous, current), [])

    def test_outage_notifies_once_across_time_restart_and_network_change(self):
        services = [main.ServiceStatus("chatgpt", "ChatGPT", False),
                    main.ServiceStatus("telegram", "Telegram Desktop", False)]
        current = make_state(services=services)
        candidates = events_for(None, current)
        self.assertEqual(len(candidates), 2)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notification-state.json"
            policy = main.NotificationPolicy({"service_status": 300}, 30, state_path)
            policy.observe_state(current)
            self.assertTrue(all(policy.should_send(event, 0)[0] for event in candidates))
            for timestamp in (60, 3600, 86400 * 365):
                self.assertFalse(any(policy.should_send(event, timestamp)[0] for event in candidates))
            restarted = main.NotificationPolicy({}, 0, state_path)
            restarted.observe_state(current)
            self.assertFalse(any(restarted.should_send(event, 86400 * 365)[0] for event in candidates))
            disconnected = make_state(False, services)
            restarted.observe_state(disconnected)
            internet_event = events_for(current, disconnected)[0]
            self.assertTrue(restarted.should_send(internet_event, 1)[0])
            self.assertFalse(restarted.should_send(internet_event, 86400 * 365)[0])
            restarted.observe_state(current)
            self.assertFalse(any(restarted.should_send(event, 2)[0] for event in candidates))
            recovered = make_state(services=[main.ServiceStatus("chatgpt", "ChatGPT", True),
                                            main.ServiceStatus("telegram", "Telegram Desktop", False)])
            restarted.observe_state(recovered)
            chatgpt_event = next(event for event in candidates if ":chatgpt:" in event.fingerprint)
            telegram_event = next(event for event in candidates if ":telegram:" in event.fingerprint)
            self.assertTrue(restarted.should_send(chatgpt_event, 3)[0])
            self.assertFalse(restarted.should_send(telegram_event, 3)[0])


class MTProtoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(1.0)
        mode = self.server.mode
        data = bytearray()
        while len(data) < 42:
            chunk = self.request.recv(42 - len(data))
            if not chunk:
                return
            data.extend(chunk)
        self.server.request_valid = data[:2] == b"\xef\x0a" and data[22:26] == struct.pack("<I", 0xBE7E8EF1)
        if mode == "silent":
            time.sleep(0.4)
            return
        nonce = data[26:42] if mode != "wrong-nonce" else b"\xff" * 16
        key_count = 60 if mode == "extended" else 1
        body = (struct.pack("<I", 0x05162463) + nonce + b"\x01" * 16
                + b"\x08" + b"\x01" * 8 + b"\x00" * 3
                + struct.pack("<II", 0x1CB5C415, key_count) + b"\x00" * 8 * key_count)
        payload = struct.pack("<QQI", 0, 1, len(body)) + body
        words = len(payload) // 4
        header = bytes([words]) if words < 127 else b"\x7f" + words.to_bytes(3, "little")
        if mode == "oversized":
            header, payload = b"\x7f\xff\xff\xff", b""
        try:
            for offset in range(0, len(header + payload), 7):
                self.request.sendall((header + payload)[offset:offset + 7])
        except ConnectionError:
            pass


class MTProtoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy_environment = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"})
        cls.proxy_environment.start()
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), MTProtoHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"mtproto://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.proxy_environment.stop()

    def test_nonce_response_and_fragmented_frames(self):
        for mode in ("valid", "extended"):
            self.server.mode = mode
            self.assertTrue(main.check_mtproto_probe(self.url, 1.0)[0])
            self.assertTrue(self.server.request_valid)

    def test_open_socket_without_protocol_response_is_offline(self):
        self.server.mode = "silent"
        started_at = time.monotonic()
        self.assertFalse(main.check_mtproto_probe(self.url, 0.1)[0])
        self.assertLess(time.monotonic() - started_at, 0.3)

    def test_wrong_nonce_and_oversized_packet_are_offline(self):
        for mode in ("wrong-nonce", "oversized"):
            self.server.mode = mode
            self.assertFalse(main.check_mtproto_probe(self.url, 1.0)[0])


if __name__ == "__main__":
    unittest.main()
