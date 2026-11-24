import copy
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import main


class CheckPreferencesTest(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test.check_preferences")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "check-settings.json"
        self.preferences = main.CheckPreferences(self.path, self.logger)
        self.config = copy.deepcopy(main.DEFAULT_CONFIG)
        self.config["show_app_started_notification"] = False
        self.config["service_checks"] = [
            service for service in self.config["service_checks"]
            if service["id"] in {"chatgpt", "chatgpt-init"}
        ]
        self.status = main.StatusStore()

    def run_monitor_once(self):
        main.run_monitor_loop(self.config, self.logger, threading.Event(), True, self.status,
                              None, self.preferences)

    def test_preference_disables_only_requested_chatgpt_probe_and_survives_restart(self):
        self.preferences.save(frozenset({"service:chatgpt-init"}))
        self.preferences = main.CheckPreferences(self.path, self.logger)
        with mock.patch("main.check_connectivity", return_value=True), \
                mock.patch("main.fetch_country", return_value=("Netherlands", "NL", "test")), \
                mock.patch("main.sync_office_country", return_value=""), \
                mock.patch("main.check_service", return_value=True) as service, \
                mock.patch("main.notify") as notify:
            self.run_monitor_once()
        service.assert_called_once()
        self.assertEqual(service.call_args.args[0][0]["url"], "https://chatgpt.com/")
        notify.assert_not_called()
        self.assertEqual(main.tray_icon_state(self.status.snapshot()), "online")

    def test_live_update_skips_network_and_office_in_next_cycle(self):
        stop = threading.Event()
        checks = 0

        def update_preferences(*_args):
            nonlocal checks
            checks += 1
            if checks == 1:
                self.preferences.save(frozenset({
                    main.INTERNET_CHECK, main.COUNTRY_CHECK, "service:chatgpt", "service:chatgpt-init",
                }))
            else:
                stop.set()

        with mock.patch("main.check_connectivity", return_value=True) as internet, \
                mock.patch("main.fetch_country", return_value=("Russia", "RU", "test")) as country, \
                mock.patch("main.sync_office_country", return_value="") as office, \
                mock.patch("main.check_services", return_value={"chatgpt": True, "chatgpt-init": True}) as services, \
                mock.patch("main.wait_for_next_cycle", side_effect=update_preferences), \
                mock.patch("main.notify") as notify:
            main.run_monitor_loop(self.config, self.logger, stop, False, self.status, None, self.preferences)
        for check in (internet, country, office, services):
            check.assert_called_once()
        notify.assert_not_called()
        state = self.status.snapshot().state
        self.assertIsNone(state.online)
        self.assertIsNone(state.country_code)
        self.assertEqual(state.service_statuses, ())

    def test_service_alert_survives_disabled_internet_probe_and_office_is_independent(self):
        self.preferences.save(frozenset({main.INTERNET_CHECK, main.OFFICE_CHECK, "service:chatgpt"}))
        with mock.patch("main.check_connectivity") as connectivity, \
                mock.patch("main.fetch_country", return_value=("Netherlands", "NL", "test")), \
                mock.patch("main.sync_office_country") as office, \
                mock.patch("main.check_service", return_value=False), \
                mock.patch("main.notify") as notify:
            self.run_monitor_once()
        connectivity.assert_not_called()
        office.assert_not_called()
        self.assertEqual(self.status.snapshot().state.country_code, "NL")
        self.assertIn("ChatGPT: ab.chatgpt.com OFFLINE", notify.call_args.kwargs["message"])

    def test_reenabled_netology_runs_next_cycle_unless_rate_limited(self):
        for rate_limited in (False, True):
            with self.subTest(rate_limited=rate_limited):
                config = {**self.config, "service_checks": [
                    service for service in main.DEFAULT_CONFIG["service_checks"]
                    if service["id"] == "netology"
                ]}
                store = main.StatusStore()
                stop = threading.Event()
                snapshots = []
                requests = []

                def check_service(probes, timeout, logger, name, policy):
                    requests.append(name)
                    if rate_limited:
                        policy.pause_until_restart()
                    return not rate_limited

                def next_cycle(*_args):
                    snapshots.append(store.snapshot())
                    if len(snapshots) == 1:
                        self.preferences.save(frozenset({"service:netology"}))
                    elif len(snapshots) == 2:
                        self.preferences.save(frozenset())
                    else:
                        stop.set()

                with mock.patch("main.check_connectivity", return_value=True), \
                        mock.patch("main.fetch_country", return_value=("Netherlands", "NL", None)), \
                        mock.patch("main.sync_office_country", return_value=""), \
                        mock.patch("main.check_service", side_effect=check_service), \
                        mock.patch("main.wait_for_next_cycle", side_effect=next_cycle), \
                        mock.patch("main.notify"):
                    main.run_monitor_loop(config, self.logger, stop, False, store, None, self.preferences)
                self.assertEqual(requests, ["Netology"] if rate_limited else ["Netology", "Netology"])
                self.assertEqual(snapshots[1].state.service_statuses, ())
                self.assertEqual(main.tray_icon_state(snapshots[2]), "offline" if rate_limited else "online")


if __name__ == "__main__":
    unittest.main()
