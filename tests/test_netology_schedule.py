import copy
import logging
import threading
import unittest
from unittest import mock

import main


class NetologyScheduleTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.logger = logging.getLogger("test.netology")
        self.policy = main.NetologyCheckPolicy(self.logger, clock=lambda: self.now)
        self.config = copy.deepcopy(main.DEFAULT_CONFIG)
        self.config["service_checks"] = [service for service in self.config["service_checks"]
                                         if service["id"] in {"netology", "github"}]
        self.services = self.config["service_checks"]

    def selected_ids(self, manual=False):
        return {service["id"] for service in self.policy.select_services(self.services, manual)}

    def test_first_check_then_exactly_30_minutes(self):
        self.assertEqual(self.selected_ids(), {"netology", "github"})
        for timestamp in (8, 60, 1799.99):
            self.now = timestamp
            self.assertEqual(self.selected_ids(), {"github"})
        self.now = 1800
        self.assertEqual(self.selected_ids(), {"netology", "github"})

    def test_manual_check_bypasses_interval_and_reschedules(self):
        self.selected_ids()
        self.now = 100
        self.assertIn("netology", self.selected_ids(manual=True))
        self.now = 1800
        self.assertNotIn("netology", self.selected_ids())
        self.now = 1900
        self.assertIn("netology", self.selected_ids())

    def test_429_blocks_automatic_and_manual_checks_until_restart(self):
        self.selected_ids()
        with self.assertLogs(self.logger, level="WARNING") as logs:
            self.policy.pause_until_restart()
            self.policy.pause_until_restart()
        self.assertEqual(len(logs.output), 1)
        self.now = 86400
        self.assertEqual(self.selected_ids(), {"github"})
        self.assertEqual(self.selected_ids(manual=True), {"github"})
        restarted = main.NetologyCheckPolicy(self.logger, clock=lambda: self.now)
        self.assertEqual(len(restarted.select_services(self.services)), 2)

    def test_cycle_runs_only_due_services_and_keeps_other_checks(self):
        with mock.patch("main.check_connectivity", return_value=True) as connectivity, \
                mock.patch("main.fetch_country", return_value=("Italy", "IT", "test")) as country, \
                mock.patch("main.check_service", return_value=True) as check:
            for timestamp, expected in ((0, {"netology", "github"}), (8, {"github"}),
                                        (1800, {"netology", "github"})):
                self.now = timestamp
                result = main.run_cycle_checks(self.config, self.logger, [], self.policy)
                self.assertEqual(set(result[4]), expected)
            self.assertEqual(check.call_count, 5)
            self.assertEqual(connectivity.call_count, 3)
            self.assertEqual(country.call_count, 3)

    def test_skipped_checks_keep_status_without_adding_confirmations(self):
        debouncer = main.StateDebouncer(1, 1, 1, 1, 3)
        debouncer.update(True, None, None, {"netology": True})
        debouncer.update(True, None, None, {"netology": False})
        for _ in range(5):
            debouncer.update(True, None, None, {"github": True})
        self.assertTrue(debouncer.stable_service_online("netology"))
        debouncer.update(True, None, None, {"netology": False})
        self.assertTrue(debouncer.stable_service_online("netology"))
        debouncer.update(True, None, None, {"netology": False})
        self.assertFalse(debouncer.stable_service_online("netology"))
        debouncer.update(True, None, None, {})
        self.assertFalse(debouncer.stable_service_online("netology"))

    def test_manual_event_survives_wait_and_reaches_cycle(self):
        event = threading.Event()
        event.set()
        stop = threading.Event()
        main.wait_for_next_cycle(stop, event, 5)
        self.assertTrue(event.is_set())
        config = copy.deepcopy(self.config)
        config["show_app_started_notification"] = False
        with mock.patch("main.run_cycle_checks", return_value=(True, "Italy", "IT", "test", {})) as cycle, \
                mock.patch("main.notify"):
            main.run_monitor_loop(config, self.logger, stop, True, main.StatusStore(), event)
        self.assertTrue(cycle.call_args.kwargs["manual_check"])
        self.assertFalse(event.is_set())

    def test_429_is_shown_offline_immediately_even_with_custom_confirmations(self):
        config = copy.deepcopy(self.config)
        config["show_app_started_notification"] = False
        for service in config["service_checks"]:
            service["fail_confirmations"] = 3
        store = main.StatusStore()

        def rate_limited_cycle(**kwargs):
            kwargs["netology_policy"].pause_until_restart()
            return True, "Italy", "IT", "test", {"netology": False, "github": True}

        with mock.patch("main.run_cycle_checks", side_effect=rate_limited_cycle), mock.patch("main.notify"):
            main.run_monitor_loop(config, self.logger, threading.Event(), True, store, None)
        netology = next(service for service in store.snapshot().state.service_statuses
                        if service.service_id == "netology")
        self.assertFalse(netology.online)
        self.assertIn("429", netology.detail)

    def test_manual_request_during_running_check_starts_the_next_cycle(self):
        stop = threading.Event()
        event = threading.Event()
        config = copy.deepcopy(self.config)
        config["show_app_started_notification"] = False
        manual_flags = []

        def cycle(**kwargs):
            manual_flags.append(kwargs["manual_check"])
            if len(manual_flags) == 1:
                event.set()
            else:
                stop.set()
            return True, "Italy", "IT", "test", {}

        with mock.patch("main.run_cycle_checks", side_effect=cycle), mock.patch("main.notify"):
            main.run_monitor_loop(config, self.logger, stop, False, main.StatusStore(), event)
        self.assertEqual(manual_flags, [False, True])

    def test_existing_config_without_netology_override_uses_one_confirmation(self):
        configured = copy.deepcopy(self.services)
        configured[0].pop("fail_confirmations", None)
        normalized = main.normalize_service_checks(configured, [])
        netology = next(service for service in normalized if service["id"] == "netology")
        self.assertEqual(netology["fail_confirmations"], 1)


if __name__ == "__main__":
    unittest.main()
