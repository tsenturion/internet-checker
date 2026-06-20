import argparse
import concurrent.futures
import copy
import ctypes
from ctypes import wintypes
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem
try:
    from pystray._util import win32 as pystray_win32
except Exception:
    pystray_win32 = None
from windows_toasts import Toast, ToastDuration, WindowsToaster


DEFAULT_CONFIG = {
    "check_interval_seconds": 5,
    "request_timeout_seconds": 1.5,
    "connectivity_success_confirmations": 1,
    "connectivity_fail_confirmations": 1,
    "country_confirmations": 1,
    "chatgpt_success_confirmations": 1,
    "chatgpt_fail_confirmations": 1,
    "notify_on_chatgpt_status_change": True,
    "notify_only_russia_transitions": True,
    "russia_country_codes": ["RU"],
    "russia_country_names": [
        "russia",
        "Russia",
        "RUSSIA",
        "russian federation",
        "россия",
        "российская федерация",
    ],
    "notify_on_start": False,
    "show_app_started_notification": True,
    "app_started_title": "Internet Checker",
    "app_started_message": "Application started. Monitoring is active.",
    "toast_duration_seconds": 8,
    "notification_cooldowns_seconds": {
        "startup": 0,
        "internet_status": 5,
        "country_change": 0,
        "public_ip_change": 2,
        "chatgpt_status": 5,
    },
    "dedup_window_seconds": 30,
    "single_instance_mutex_name": "Global\\InternetCheckerMutex",
    "log_file_path": "logs/internet-checker.log",
    "log_max_bytes": 1_000_000,
    "log_backup_count": 5,
    "log_to_console": True,
    "connectivity_attempts": 1,
    "connectivity_urls": [
        "http://www.msftconnecttest.com/connecttest.txt",
        "https://cloudflare.com/cdn-cgi/trace",
        "https://clients3.google.com/generate_204",
    ],
    "country_lookup_urls": [
        "https://ipwho.is/",
        "http://ip-api.com/json/?fields=status,country,countryCode,query",
        "https://ipapi.co/json/",
    ],
    "chatgpt_probe_urls": [
        {"url": "https://chatgpt.com/", "method": "TCP"},
        {"url": "https://api.openai.com/v1/models", "method": "TCP"},
    ],
    "country_lookup_no_cache": True,
    "notify_on_public_ip_change": True,
    "tray_icon_tooltip": "Internet Checker",
    "tray_show_status_label": "Показать статус",
    "tray_check_now_label": "Проверить сейчас",
    "tray_open_log_label": "Открыть лог",
    "tray_exit_label": "Выход",
}


@dataclass
class NetworkState:
    online: bool
    country_name: Optional[str]
    country_code: Optional[str]
    public_ip: Optional[str]
    chatgpt_online: Optional[bool]
    checked_at: datetime


@dataclass
class NotificationEvent:
    event_type: str
    message: str
    fingerprint: str


@dataclass
class DebounceUpdate:
    online_changed: bool
    country_changed: bool
    chatgpt_changed: bool


@dataclass(frozen=True)
class StatusSnapshot:
    state: Optional[NetworkState]
    checking: bool
    updated_at: Optional[datetime]
    last_error: Optional[str]


class SingleInstance:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, mutex_name: str):
        self._mutex_name = mutex_name
        self._handle: Optional[int] = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool

        handle = create_mutex(None, False, self._mutex_name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
            close_handle(handle)
            return False

        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


class StateDebouncer:
    def __init__(
        self,
        online_success: int,
        online_fail: int,
        country_confirmations: int,
        chatgpt_success: int,
        chatgpt_fail: int,
    ):
        self._online_success_required = max(1, int(online_success))
        self._online_fail_required = max(1, int(online_fail))
        self._country_required = max(1, int(country_confirmations))
        self._chatgpt_success_required = max(1, int(chatgpt_success))
        self._chatgpt_fail_required = max(1, int(chatgpt_fail))

        self._stable_online: Optional[bool] = None
        self._stable_country_name: Optional[str] = None
        self._stable_country_code: Optional[str] = None
        self._stable_public_ip: Optional[str] = None
        self._stable_chatgpt_online: Optional[bool] = None

        self._online_success_streak = 0
        self._online_fail_streak = 0
        self._chatgpt_success_streak = 0
        self._chatgpt_fail_streak = 0

        self._country_candidate_key: Optional[str] = None
        self._country_candidate_name: Optional[str] = None
        self._country_candidate_code: Optional[str] = None
        self._country_candidate_streak = 0

    @property
    def stable_online(self) -> bool:
        return bool(self._stable_online)

    @property
    def has_stable_online(self) -> bool:
        return self._stable_online is not None

    @property
    def stable_country_name(self) -> Optional[str]:
        return self._stable_country_name

    @property
    def stable_country_code(self) -> Optional[str]:
        return self._stable_country_code

    @property
    def stable_public_ip(self) -> Optional[str]:
        return self._stable_public_ip

    @property
    def stable_chatgpt_online(self) -> Optional[bool]:
        return self._stable_chatgpt_online

    @property
    def has_stable_chatgpt(self) -> bool:
        return self._stable_chatgpt_online is not None

    @staticmethod
    def _country_key(country_name: Optional[str], country_code: Optional[str]) -> Optional[str]:
        if country_code:
            return f"code:{country_code.upper()}"
        if country_name:
            return f"name:{country_name.lower()}"
        return None

    @staticmethod
    def _normalize_country(
        country_name: Optional[str],
        country_code: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        normalized_name = country_name.strip() if isinstance(country_name, str) and country_name.strip() else None
        normalized_code = country_code.strip().upper() if isinstance(country_code, str) and country_code.strip() else None

        if not normalized_code and normalized_name and len(normalized_name) == 2 and normalized_name.isalpha():
            normalized_code = normalized_name.upper()
            normalized_name = None

        return normalized_name, normalized_code

    @staticmethod
    def _normalize_public_ip(public_ip: Optional[str]) -> Optional[str]:
        if not isinstance(public_ip, str):
            return None
        normalized = public_ip.strip()
        if not normalized:
            return None
        return normalized

    def _reset_country_candidate(self) -> None:
        self._country_candidate_key = None
        self._country_candidate_name = None
        self._country_candidate_code = None
        self._country_candidate_streak = 0

    def _update_chatgpt(self, raw_chatgpt_online: bool) -> bool:
        if raw_chatgpt_online:
            self._chatgpt_success_streak += 1
            self._chatgpt_fail_streak = 0
        else:
            self._chatgpt_fail_streak += 1
            self._chatgpt_success_streak = 0

        if self._stable_chatgpt_online is None:
            if raw_chatgpt_online and self._chatgpt_success_streak >= self._chatgpt_success_required:
                self._stable_chatgpt_online = True
            elif not raw_chatgpt_online and self._chatgpt_fail_streak >= self._chatgpt_fail_required:
                self._stable_chatgpt_online = False
            return False

        if (
            self._stable_chatgpt_online
            and not raw_chatgpt_online
            and self._chatgpt_fail_streak >= self._chatgpt_fail_required
        ):
            self._stable_chatgpt_online = False
            return True

        if (
            not self._stable_chatgpt_online
            and raw_chatgpt_online
            and self._chatgpt_success_streak >= self._chatgpt_success_required
        ):
            self._stable_chatgpt_online = True
            return True

        return False

    def update(
        self,
        raw_online: bool,
        raw_country_name: Optional[str],
        raw_country_code: Optional[str],
        raw_public_ip: Optional[str],
        raw_chatgpt_online: bool,
    ) -> DebounceUpdate:
        raw_country_name, raw_country_code = self._normalize_country(raw_country_name, raw_country_code)
        raw_public_ip = self._normalize_public_ip(raw_public_ip)

        online_changed = False
        country_changed = False
        chatgpt_changed = self._update_chatgpt(raw_chatgpt_online)

        if raw_online:
            self._online_success_streak += 1
            self._online_fail_streak = 0
        else:
            self._online_fail_streak += 1
            self._online_success_streak = 0

        if self._stable_online is None:
            if raw_online and self._online_success_streak >= self._online_success_required:
                self._stable_online = True
            elif not raw_online and self._online_fail_streak >= self._online_fail_required:
                self._stable_online = False
            else:
                return DebounceUpdate(
                    online_changed=False,
                    country_changed=False,
                    chatgpt_changed=chatgpt_changed,
                )
        elif self._stable_online and not raw_online and self._online_fail_streak >= self._online_fail_required:
            self._stable_online = False
            online_changed = True
        elif not self._stable_online and raw_online and self._online_success_streak >= self._online_success_required:
            self._stable_online = True
            online_changed = True

        if not self._stable_online:
            self._stable_country_name = None
            self._stable_country_code = None
            self._stable_public_ip = None
            self._reset_country_candidate()
            return DebounceUpdate(
                online_changed=online_changed,
                country_changed=False,
                chatgpt_changed=chatgpt_changed,
            )

        if raw_public_ip:
            self._stable_public_ip = raw_public_ip

        raw_key = self._country_key(raw_country_name, raw_country_code)
        stable_key = self._country_key(self._stable_country_name, self._stable_country_code)

        if raw_key is None:
            self._reset_country_candidate()
            return DebounceUpdate(
                online_changed=online_changed,
                country_changed=False,
                chatgpt_changed=chatgpt_changed,
            )

        if stable_key is None:
            self._stable_country_name = raw_country_name
            self._stable_country_code = raw_country_code
            self._reset_country_candidate()
            return DebounceUpdate(
                online_changed=online_changed,
                country_changed=False,
                chatgpt_changed=chatgpt_changed,
            )

        if raw_key == stable_key:
            if raw_country_name and raw_country_name != self._stable_country_name:
                self._stable_country_name = raw_country_name
            if raw_country_code and raw_country_code != self._stable_country_code:
                self._stable_country_code = raw_country_code
            self._reset_country_candidate()
            return DebounceUpdate(
                online_changed=online_changed,
                country_changed=False,
                chatgpt_changed=chatgpt_changed,
            )

        if raw_key == self._country_candidate_key:
            self._country_candidate_streak += 1
        else:
            self._country_candidate_key = raw_key
            self._country_candidate_name = raw_country_name
            self._country_candidate_code = raw_country_code
            self._country_candidate_streak = 1

        if self._country_candidate_streak >= self._country_required:
            self._stable_country_name = self._country_candidate_name
            self._stable_country_code = self._country_candidate_code
            self._reset_country_candidate()
            country_changed = True

        return DebounceUpdate(
            online_changed=online_changed,
            country_changed=country_changed,
            chatgpt_changed=chatgpt_changed,
        )


class NotificationPolicy:
    def __init__(self, cooldowns: dict[str, int], dedup_window_seconds: int):
        self._cooldowns = {key: max(0, int(value)) for key, value in cooldowns.items()}
        self._dedup_window_seconds = max(0, int(dedup_window_seconds))
        self._last_by_type: dict[str, float] = {}
        self._last_by_fingerprint: dict[str, float] = {}

    def _cleanup(self, now_ts: float) -> None:
        if self._dedup_window_seconds <= 0 or len(self._last_by_fingerprint) < 500:
            return
        cutoff = now_ts - (self._dedup_window_seconds * 2)
        self._last_by_fingerprint = {
            fingerprint: ts
            for fingerprint, ts in self._last_by_fingerprint.items()
            if ts >= cutoff
        }

    def should_send(self, event: NotificationEvent, now_ts: float) -> tuple[bool, Optional[str]]:
        cooldown_seconds = self._cooldowns.get(event.event_type, 0)
        last_type_ts = self._last_by_type.get(event.event_type)
        if last_type_ts is not None and cooldown_seconds > 0 and now_ts - last_type_ts < cooldown_seconds:
            return False, f"cooldown({cooldown_seconds}s)"

        if self._dedup_window_seconds > 0:
            last_fingerprint_ts = self._last_by_fingerprint.get(event.fingerprint)
            if last_fingerprint_ts is not None and now_ts - last_fingerprint_ts < self._dedup_window_seconds:
                return False, f"duplicate({self._dedup_window_seconds}s)"

        self._last_by_type[event.event_type] = now_ts
        if self._dedup_window_seconds > 0:
            self._last_by_fingerprint[event.fingerprint] = now_ts
            self._cleanup(now_ts)
        return True, None


class StatusStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Optional[NetworkState] = None
        self._checking = False
        self._updated_at: Optional[datetime] = None
        self._last_error: Optional[str] = None

    def set_checking(self, checking: bool) -> None:
        with self._lock:
            self._checking = checking
            if checking:
                self._last_error = None

    def set_state(self, state: NetworkState) -> None:
        with self._lock:
            self._state = state
            self._checking = False
            self._updated_at = state.checked_at
            self._last_error = None

    def set_error(self, message: str) -> None:
        with self._lock:
            self._checking = False
            self._last_error = message
            self._updated_at = datetime.now()

    def snapshot(self) -> StatusSnapshot:
        with self._lock:
            return StatusSnapshot(
                state=self._state,
                checking=self._checking,
                updated_at=self._updated_at,
                last_error=self._last_error,
            )


def normalize_probe_urls(value: object, default: list[dict[str, str]], default_method: str) -> list[dict[str, str]]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        value = []

    probes: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            url = item.strip()
            method = default_method
        elif isinstance(item, dict):
            raw_url = item.get("url")
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            raw_method = item.get("method", default_method)
            method = raw_method.strip().upper() if isinstance(raw_method, str) else default_method
        else:
            continue

        if not url:
            continue
        if method not in {"GET", "HEAD", "TCP"}:
            method = default_method

        probes.append({"url": url, "method": method})

    return probes or copy.deepcopy(default)


def load_config(path: Path) -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)

    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            custom = json.load(file)
        if not isinstance(custom, dict):
            raise ValueError("Config must be a JSON object.")
        config.update(custom)

    merged_cooldowns = dict(DEFAULT_CONFIG["notification_cooldowns_seconds"])
    if isinstance(config.get("notification_cooldowns_seconds"), dict):
        merged_cooldowns.update(config["notification_cooldowns_seconds"])
    config["notification_cooldowns_seconds"] = merged_cooldowns

    lookup_urls = config.get("country_lookup_urls")
    if isinstance(lookup_urls, str):
        lookup_urls = [lookup_urls]
    if not isinstance(lookup_urls, list):
        lookup_urls = []

    legacy_url = config.get("country_lookup_url")
    if isinstance(legacy_url, str) and legacy_url.strip() and legacy_url not in lookup_urls:
        lookup_urls.insert(0, legacy_url)

    clean_urls = [item.strip() for item in lookup_urls if isinstance(item, str) and item.strip()]
    config["country_lookup_urls"] = clean_urls or list(DEFAULT_CONFIG["country_lookup_urls"])

    connectivity_urls = config.get("connectivity_urls")
    if isinstance(connectivity_urls, str):
        connectivity_urls = [connectivity_urls]
    if not isinstance(connectivity_urls, list):
        connectivity_urls = []

    legacy_connectivity_url = config.get("connectivity_url")
    if (
        isinstance(legacy_connectivity_url, str)
        and legacy_connectivity_url.strip()
        and legacy_connectivity_url not in connectivity_urls
    ):
        connectivity_urls.insert(0, legacy_connectivity_url)

    clean_connectivity_urls = [
        item.strip()
        for item in connectivity_urls
        if isinstance(item, str) and item.strip()
    ]
    config["connectivity_urls"] = clean_connectivity_urls or list(DEFAULT_CONFIG["connectivity_urls"])
    config["chatgpt_probe_urls"] = normalize_probe_urls(
        config.get("chatgpt_probe_urls"),
        default=DEFAULT_CONFIG["chatgpt_probe_urls"],
        default_method="HEAD",
    )

    config["check_interval_seconds"] = max(1, int(config["check_interval_seconds"]))
    config["request_timeout_seconds"] = max(0.2, float(config["request_timeout_seconds"]))
    config["connectivity_attempts"] = max(1, int(config.get("connectivity_attempts", 1)))
    config["connectivity_success_confirmations"] = max(1, int(config["connectivity_success_confirmations"]))
    config["connectivity_fail_confirmations"] = max(1, int(config["connectivity_fail_confirmations"]))
    config["country_confirmations"] = max(1, int(config["country_confirmations"]))
    config["chatgpt_success_confirmations"] = max(1, int(config["chatgpt_success_confirmations"]))
    config["chatgpt_fail_confirmations"] = max(1, int(config["chatgpt_fail_confirmations"]))
    config["dedup_window_seconds"] = max(0, int(config["dedup_window_seconds"]))
    config["log_max_bytes"] = max(1024, int(config["log_max_bytes"]))
    config["log_backup_count"] = max(1, int(config["log_backup_count"]))
    config["toast_duration_seconds"] = max(1, int(config["toast_duration_seconds"]))
    config["tray_icon_tooltip"] = str(config.get("tray_icon_tooltip", "Internet Checker")).strip() or "Internet Checker"
    config["tray_show_status_label"] = (
        str(config.get("tray_show_status_label", "Показать статус")).strip() or "Показать статус"
    )
    config["tray_check_now_label"] = (
        str(config.get("tray_check_now_label", "Проверить сейчас")).strip() or "Проверить сейчас"
    )
    config["tray_open_log_label"] = (
        str(config.get("tray_open_log_label", "Открыть лог")).strip() or "Открыть лог"
    )
    config["tray_exit_label"] = str(config.get("tray_exit_label", "Exit")).strip() or "Exit"
    config["app_started_title"] = str(config.get("app_started_title", "Internet Checker")).strip() or "Internet Checker"
    config["app_started_message"] = (
        str(config.get("app_started_message", "Application started. Monitoring is active.")).strip()
        or "Application started. Monitoring is active."
    )

    codes = config.get("russia_country_codes", ["RU"])
    if not isinstance(codes, list):
        codes = ["RU"]
    config["russia_country_codes"] = [
        value.strip().upper()
        for value in codes
        if isinstance(value, str) and value.strip()
    ] or ["RU"]

    names = config.get(
        "russia_country_names",
        ["russia", "Russia", "RUSSIA", "russian federation", "россия", "российская федерация"],
    )
    if not isinstance(names, list):
        names = ["russia", "Russia", "RUSSIA", "russian federation", "россия", "российская федерация"]
    config["russia_country_names"] = [
        value.strip().casefold()
        for value in names
        if isinstance(value, str) and value.strip()
    ] or ["russia", "Russia", "RUSSIA", "russian federation", "россия", "российская федерация"]

    config["country_lookup_no_cache"] = bool(config.get("country_lookup_no_cache", True))
    config["notify_on_public_ip_change"] = bool(config.get("notify_on_public_ip_change", True))
    config["notify_on_chatgpt_status_change"] = bool(config.get("notify_on_chatgpt_status_change", True))

    return config


def get_app_base_dir() -> Path:
    # When packaged with PyInstaller, use folder of executable.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_fallback_data_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        fallback = Path(local_appdata) / "InternetChecker"
    else:
        fallback = Path.home() / "AppData" / "Local" / "InternetChecker"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def is_startup_dir(path: Path) -> bool:
    startup_candidates: list[Path] = []

    appdata = os.environ.get("APPDATA")
    if appdata:
        startup_candidates.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")

    programdata = os.environ.get("ProgramData")
    if programdata:
        startup_candidates.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")

    try:
        normalized = path.resolve()
    except Exception:
        normalized = path

    for candidate in startup_candidates:
        try:
            if normalized == candidate.resolve():
                return True
        except Exception:
            if str(normalized).lower() == str(candidate).lower():
                return True

    return False


def resolve_data_dir(app_base_dir: Path) -> Path:
    # If exe is placed directly into Startup folder, keep runtime files out of Startup.
    if is_startup_dir(app_base_dir):
        return get_fallback_data_dir()

    # Prefer app directory; if not writable, fallback to LocalAppData.
    try:
        app_base_dir.mkdir(parents=True, exist_ok=True)
        probe = app_base_dir / ".write_test"
        with probe.open("w", encoding="utf-8") as file:
            file.write("ok")
        probe.unlink(missing_ok=True)
        return app_base_dir
    except Exception:
        return get_fallback_data_dir()

def resolve_config_path(config_arg: str, data_dir: Path) -> Path:
    candidate = Path(config_arg)
    if candidate.is_absolute():
        return candidate
    return data_dir / candidate


def finalize_runtime_paths(config: dict, data_dir: Path) -> dict:
    result = dict(config)
    log_path = Path(str(result.get("log_file_path", DEFAULT_CONFIG["log_file_path"])))
    if not log_path.is_absolute():
        log_path = data_dir / log_path
    result["log_file_path"] = str(log_path)
    return result


def save_example_config(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", encoding="utf-8") as file:
        json.dump(DEFAULT_CONFIG, file, indent=2)


def setup_logging(config: dict) -> logging.Logger:
    logger = logging.getLogger("internet_checker")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    log_path = Path(config["log_file_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = RotatingFileHandler(
        filename=log_path,
        maxBytes=int(config["log_max_bytes"]),
        backupCount=int(config["log_backup_count"]),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if bool(config.get("log_to_console", True)):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return logger


def build_request_timeout(timeout_seconds: float) -> tuple[float, float]:
    timeout = max(0.2, float(timeout_seconds))
    return min(0.75, timeout), timeout


def tcp_probe_url(url: str, timeout_seconds: float) -> tuple[bool, str]:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False, f"TCP {url} -> invalid URL"

    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    use_tls = scheme == "https"
    timeout = max(0.2, float(timeout_seconds))

    raw_socket = None
    try:
        raw_socket = socket.create_connection((host, port), timeout=timeout)
        if use_tls:
            context = ssl.create_default_context()
            with context.wrap_socket(raw_socket, server_hostname=host):
                raw_socket = None
                return True, f"TCP {host}:{port} TLS -> connected"
        raw_socket.close()
        raw_socket = None
        return True, f"TCP {host}:{port} -> connected"
    except OSError as exc:
        return False, f"TCP {host}:{port} -> {type(exc).__name__}"
    finally:
        if raw_socket is not None:
            try:
                raw_socket.close()
            except OSError:
                pass


def http_probe(url: str, timeout_seconds: float, method: str = "HEAD") -> tuple[bool, str]:
    method = method.upper()
    if method == "TCP":
        return tcp_probe_url(url, timeout_seconds)

    try:
        response = requests.request(
            method=method,
            url=url,
            timeout=build_request_timeout(timeout_seconds),
            allow_redirects=True,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        return response.status_code < 500, f"{method} {url} -> HTTP {response.status_code}"
    except requests.RequestException as exc:
        tcp_ok, tcp_detail = tcp_probe_url(url, timeout_seconds)
        if tcp_ok:
            return True, tcp_detail
        return False, f"{method} {url} -> {type(exc).__name__}; {tcp_detail}"


def check_connectivity(urls: list[str], timeout_seconds: float, attempts: int) -> bool:
    urls = [url for url in urls if isinstance(url, str) and url.strip()]
    if not urls:
        return False

    for attempt_index in range(max(1, attempts)):
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(urls), 6),
            thread_name_prefix="connectivity-probe",
        )
        futures = [executor.submit(http_probe, url, timeout_seconds, "TCP") for url in urls]
        try:
            for future in concurrent.futures.as_completed(futures, timeout=float(timeout_seconds) + 0.5):
                ok, _detail = future.result()
                if ok:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return True
        except concurrent.futures.TimeoutError:
            pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if attempt_index < attempts - 1:
            time.sleep(0.1)

    return False


def parse_country_payload(data: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not isinstance(data, dict):
        return None, None, None

    status = data.get("status")
    if isinstance(status, str) and status.lower() in {"fail", "error"}:
        return None, None, None

    success = data.get("success")
    if success is False:
        return None, None, None

    country_name = data.get("country_name")
    country_code = data.get("country_code")

    if not country_name:
        country_name = data.get("country")
    if not country_code:
        country_code = data.get("countryCode")

    if not country_code and isinstance(data.get("country"), str) and len(data["country"]) == 2:
        country_code = data.get("country")
        if not country_name:
            country_name = None

    public_ip = data.get("ip")
    if not public_ip:
        public_ip = data.get("query")
    if not public_ip:
        public_ip = data.get("ipAddress")

    if isinstance(country_code, str):
        country_code = country_code.strip().upper() or None
    else:
        country_code = None

    if isinstance(country_name, str):
        country_name = country_name.strip() or None
    else:
        country_name = None

    if isinstance(public_ip, str):
        public_ip = public_ip.strip() or None
    else:
        public_ip = None

    if country_code is None and country_name is None and public_ip is None:
        return None, None, None

    return country_name, country_code, public_ip


def build_country_request_url(url: str, no_cache: bool) -> str:
    if not no_cache:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_ts={int(time.time() * 1000)}"


def fetch_country_from_url(
    url: str,
    timeout_seconds: float,
    no_cache: bool,
) -> tuple[Optional[str], Optional[str], Optional[str], str, Optional[str]]:
    headers = {"User-Agent": "InternetChecker/1.0"}
    if no_cache:
        headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
    request_url = build_country_request_url(url, no_cache=no_cache)
    try:
        response = requests.get(
            request_url,
            timeout=max(0.2, float(timeout_seconds)),
            headers=headers,
        )
        response.raise_for_status()
        country_name, country_code, public_ip = parse_country_payload(response.json())
        if country_name or country_code or public_ip:
            return country_name, country_code, public_ip, url, None
        return None, None, None, url, "empty response"
    except (requests.RequestException, ValueError) as exc:
        return None, None, None, url, type(exc).__name__


def fetch_country(
    urls: list[str],
    timeout_seconds: float,
    logger: logging.Logger,
    no_cache: bool,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    urls = [url for url in urls if isinstance(url, str) and url.strip()]
    if not urls:
        return None, None, None, None

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(urls), 6),
        thread_name_prefix="country-probe",
    )
    futures = [
        executor.submit(fetch_country_from_url, url, timeout_seconds, no_cache)
        for url in urls
    ]
    errors: list[str] = []

    try:
        for future in concurrent.futures.as_completed(futures, timeout=float(timeout_seconds) + 0.75):
            country_name, country_code, public_ip, source_url, error = future.result()
            if country_name or country_code or public_ip:
                executor.shutdown(wait=False, cancel_futures=True)
                return country_name, country_code, public_ip, source_url
            if error:
                errors.append(f"{source_url} ({error})")
    except concurrent.futures.TimeoutError:
        errors.append("timeout")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if errors:
        logger.info("Country lookup failed on all APIs: %s", "; ".join(errors))
    return None, None, None, None


def check_chatgpt(probes: list[dict[str, str]], timeout_seconds: float) -> bool:
    if not probes:
        return False

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(probes), 4),
        thread_name_prefix="chatgpt-probe",
    )
    futures = [
        executor.submit(http_probe, probe["url"], timeout_seconds, probe.get("method", "HEAD"))
        for probe in probes
        if probe.get("url")
    ]

    try:
        for future in concurrent.futures.as_completed(futures, timeout=float(timeout_seconds) + 0.5):
            ok, _detail = future.result()
            if ok:
                executor.shutdown(wait=False, cancel_futures=True)
                return True
    except concurrent.futures.TimeoutError:
        pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return False


def snapshot_text(state: NetworkState) -> str:
    if not state.online:
        return f"Internet: OFFLINE | ChatGPT: {format_chatgpt_status(state.chatgpt_online)}"
    country = state.country_name or state.country_code or "Unknown"
    chatgpt = format_chatgpt_status(state.chatgpt_online)
    ip_suffix = f" | IP: {state.public_ip}" if state.public_ip else ""
    return f"Internet: ONLINE | Country: {country} | ChatGPT: {chatgpt}{ip_suffix}"


def format_chatgpt_status(value: Optional[bool]) -> str:
    if value is None:
        return "UNKNOWN"
    return "ONLINE" if value else "OFFLINE"


def is_russia_country(
    country_name: Optional[str],
    country_code: Optional[str],
    russia_codes: set[str],
    russia_names: set[str],
) -> bool:
    if isinstance(country_code, str) and country_code.strip().upper() in russia_codes:
        return True

    if not isinstance(country_name, str):
        return False

    normalized = country_name.strip().casefold()
    if not normalized:
        return False

    if normalized in russia_names:
        return True

    return "russia" in normalized or "росси" in normalized


def collect_events(
    prev: Optional[NetworkState],
    current: NetworkState,
    notify_on_start: bool,
    notify_only_russia_transitions: bool,
    notify_on_public_ip_change: bool,
    notify_on_chatgpt_status_change: bool,
    russia_codes: set[str],
    russia_names: set[str],
) -> list[NotificationEvent]:
    if prev is None:
        if notify_on_start and not notify_only_russia_transitions:
            status = "ONLINE" if current.online else "OFFLINE"
            return [
                NotificationEvent(
                    event_type="startup",
                    message=snapshot_text(current),
                    fingerprint=f"startup:{status}:{current.country_code or 'none'}",
                )
            ]
        return []

    events: list[NotificationEvent] = []

    if (
        notify_on_chatgpt_status_change
        and prev.chatgpt_online is not None
        and current.chatgpt_online is not None
        and prev.chatgpt_online != current.chatgpt_online
    ):
        status = format_chatgpt_status(current.chatgpt_online)
        events.append(
            NotificationEvent(
                event_type="chatgpt_status",
                message=f"ChatGPT connection changed: {status}",
                fingerprint=f"chatgpt_status:{status}",
            )
        )

    if prev.online != current.online:
        if not notify_only_russia_transitions:
            status = "ONLINE" if current.online else "OFFLINE"
            events.append(
                NotificationEvent(
                    event_type="internet_status",
                    message=f"Internet status changed: {status}\n{snapshot_text(current)}",
                    fingerprint=f"internet_status:{status}",
                )
            )
        return events

    prev_country_key = prev.country_code or prev.country_name
    current_country_key = current.country_code or current.country_name

    prev_is_russia = False
    current_is_russia = False
    if notify_only_russia_transitions:
        prev_is_russia = is_russia_country(prev.country_name, prev.country_code, russia_codes, russia_names)
        current_is_russia = is_russia_country(current.country_name, current.country_code, russia_codes, russia_names)

    if current.online and prev_country_key != current_country_key:
        if notify_only_russia_transitions and not (prev_is_russia or current_is_russia):
            return events

        before = prev.country_name or prev.country_code or "Unknown"
        after = current.country_name or current.country_code or "Unknown"
        events.append(
            NotificationEvent(
                event_type="country_change",
                message=f"Country changed: {before} -> {after}",
                fingerprint=f"country_change:{prev_country_key or before}->{current_country_key or after}",
            )
        )

    if (
        notify_on_public_ip_change
        and current.online
        and prev.public_ip
        and current.public_ip
        and prev.public_ip != current.public_ip
        and prev_country_key == current_country_key
    ):
        if not (notify_only_russia_transitions and not (prev_is_russia or current_is_russia)):
            country = current.country_name or current.country_code or "Unknown"
            events.append(
                NotificationEvent(
                    event_type="public_ip_change",
                    message=f"Public IP changed: {prev.public_ip} -> {current.public_ip}\nCountry: {country}",
                    fingerprint=f"public_ip_change:{prev.public_ip}->{current.public_ip}:{current_country_key or country}",
                )
            )

    return events


def notify(toaster: WindowsToaster, title: str, message: str, duration_seconds: int) -> None:
    toast = Toast()
    toast.duration = ToastDuration.Short if duration_seconds <= 7 else ToastDuration.Long
    toast.text_fields = [title, message]
    toaster.show_toast(toast)


def create_tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=(32, 109, 221, 255), outline=(255, 255, 255, 255), width=2)
    draw.ellipse((20, 20, 44, 44), fill=(255, 255, 255, 255))
    return image


class ClickMenuIcon(Icon):
    def _show_click_menu(self) -> bool:
        if pystray_win32 is None or not self._menu_handle:
            return False

        self.update_menu()
        pystray_win32.SetForegroundWindow(self._hwnd)

        point = wintypes.POINT()
        pystray_win32.GetCursorPos(ctypes.byref(point))

        hmenu, descriptors = self._menu_handle
        index = pystray_win32.TrackPopupMenuEx(
            hmenu,
            pystray_win32.TPM_RIGHTALIGN | pystray_win32.TPM_BOTTOMALIGN | pystray_win32.TPM_RETURNCMD,
            point.x,
            point.y,
            self._menu_hwnd,
            None,
        )
        if index > 0:
            descriptors[index - 1](self)
        return True

    def _on_notify(self, wparam, lparam):
        if pystray_win32 is not None and lparam in {pystray_win32.WM_LBUTTONUP, pystray_win32.WM_RBUTTONUP}:
            if self._show_click_menu():
                return
        return super()._on_notify(wparam, lparam)


def tray_status_lines(snapshot: StatusSnapshot) -> list[str]:
    if snapshot.last_error:
        return [f"Ошибка: {snapshot.last_error}"]

    if snapshot.state is None:
        return ["Статус: проверяется..." if snapshot.checking else "Статус: нет данных"]

    state = snapshot.state
    internet = "ONLINE" if state.online else "OFFLINE"
    country = state.country_name or state.country_code or "Unknown"
    checked_at = state.checked_at.strftime("%H:%M:%S")
    lines = [
        f"Интернет: {internet}",
        f"Страна: {country}",
        f"ChatGPT: {format_chatgpt_status(state.chatgpt_online)}",
    ]
    if state.public_ip:
        lines.append(f"IP: {state.public_ip}")
    lines.append(f"Обновлено: {checked_at}")
    if snapshot.checking:
        lines.append("Проверка: выполняется")
    return lines


def tray_tooltip(base_title: str, snapshot: StatusSnapshot) -> str:
    if snapshot.state is None:
        return f"{base_title} - checking" if snapshot.checking else base_title
    state = snapshot.state
    internet = "ONLINE" if state.online else "OFFLINE"
    chatgpt = format_chatgpt_status(state.chatgpt_online)
    country = state.country_name or state.country_code or "Unknown"
    return f"{base_title} - Internet {internet}, ChatGPT {chatgpt}, {country}"


def make_tray_menu(
    config: dict,
    logger: logging.Logger,
    status_store: StatusStore,
    check_now_event: threading.Event,
    stop_event: threading.Event,
) -> Menu:
    def items():
        snapshot = status_store.snapshot()
        for line in tray_status_lines(snapshot):
            yield MenuItem(line, None, enabled=False)
        yield Menu.SEPARATOR
        yield MenuItem(
            str(config["tray_show_status_label"]),
            lambda icon, item: _on_tray_show_status(icon, logger, status_store),
        )
        yield MenuItem(
            str(config["tray_check_now_label"]),
            lambda icon, item: _on_tray_check_now(icon, logger, status_store, check_now_event),
        )
        yield MenuItem(
            str(config["tray_open_log_label"]),
            lambda icon, item: _on_tray_open_log(logger, Path(str(config["log_file_path"]))),
        )
        yield Menu.SEPARATOR
        yield MenuItem(
            str(config["tray_exit_label"]),
            lambda icon, item: _on_tray_exit(icon, logger, stop_event),
        )

    return Menu(items)


def wait_for_next_cycle(
    stop_event: threading.Event,
    check_now_event: Optional[threading.Event],
    interval_seconds: int,
) -> None:
    deadline = time.monotonic() + max(1, int(interval_seconds))
    while not stop_event.is_set() and time.monotonic() < deadline:
        if check_now_event is not None and check_now_event.is_set():
            check_now_event.clear()
            return
        time.sleep(0.1)


def future_result(future: concurrent.futures.Future, default: object, logger: logging.Logger, label: str) -> object:
    try:
        return future.result()
    except Exception as exc:
        logger.info("%s failed: %s", label, exc)
        return default


def run_monitor_loop(
    config: dict,
    logger: logging.Logger,
    stop_event: threading.Event,
    run_once: bool,
    status_store: StatusStore,
    check_now_event: Optional[threading.Event],
) -> None:
    toaster = WindowsToaster("Internet Checker")
    debouncer = StateDebouncer(
        online_success=int(config["connectivity_success_confirmations"]),
        online_fail=int(config["connectivity_fail_confirmations"]),
        country_confirmations=int(config["country_confirmations"]),
        chatgpt_success=int(config["chatgpt_success_confirmations"]),
        chatgpt_fail=int(config["chatgpt_fail_confirmations"]),
    )
    notification_policy = NotificationPolicy(
        cooldowns=config["notification_cooldowns_seconds"],
        dedup_window_seconds=int(config["dedup_window_seconds"]),
    )

    previous_state: Optional[NetworkState] = None
    last_country_source: Optional[str] = None

    logger.info("Internet checker started.")
    if bool(config.get("show_app_started_notification", True)):
        try:
            notify(
                toaster=toaster,
                title=str(config["app_started_title"]),
                message=str(config["app_started_message"]),
                duration_seconds=int(config["toast_duration_seconds"]),
            )
            logger.info("Startup notification sent.")
        except Exception as exc:
            logger.error("Startup notification failed: %s", exc)

    try:
        while not stop_event.is_set():
            now = datetime.now()
            now_ts = time.time()
            timeout = float(config["request_timeout_seconds"])
            if check_now_event is not None:
                check_now_event.clear()
            status_store.set_checking(True)

            country_urls = list(config["country_lookup_urls"])
            if last_country_source and last_country_source in country_urls:
                country_urls = [last_country_source] + [url for url in country_urls if url != last_country_source]

            with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="monitor-cycle") as executor:
                online_future = executor.submit(
                    check_connectivity,
                    urls=config["connectivity_urls"],
                    timeout_seconds=timeout,
                    attempts=int(config["connectivity_attempts"]),
                )
                country_future = executor.submit(
                    fetch_country,
                    urls=country_urls,
                    timeout_seconds=timeout,
                    logger=logger,
                    no_cache=bool(config.get("country_lookup_no_cache", True)),
                )
                chatgpt_future = executor.submit(
                    check_chatgpt,
                    probes=config["chatgpt_probe_urls"],
                    timeout_seconds=timeout,
                )

                raw_country_name, raw_country_code, raw_public_ip, country_source = future_result(
                    country_future,
                    (None, None, None, None),
                    logger,
                    "Country lookup",
                )
                raw_chatgpt_online = bool(future_result(chatgpt_future, False, logger, "ChatGPT check"))
                raw_connectivity_online = bool(future_result(online_future, False, logger, "Connectivity check"))

            if country_source and country_source != last_country_source:
                logger.info("Country API source selected: %s", country_source)
                last_country_source = country_source

            raw_online = raw_connectivity_online or raw_chatgpt_online or bool(
                raw_country_name or raw_country_code or raw_public_ip
            )

            debouncer.update(
                raw_online,
                raw_country_name,
                raw_country_code,
                raw_public_ip,
                raw_chatgpt_online,
            )
            if not debouncer.has_stable_online:
                logger.info("No notification. State: warming up connectivity checks.")
                if run_once:
                    break
                wait_for_next_cycle(stop_event, check_now_event, int(config["check_interval_seconds"]))
                continue

            current_state = NetworkState(
                online=debouncer.stable_online,
                country_name=debouncer.stable_country_name if debouncer.stable_online else None,
                country_code=debouncer.stable_country_code if debouncer.stable_online else None,
                public_ip=debouncer.stable_public_ip if debouncer.stable_online else None,
                chatgpt_online=debouncer.stable_chatgpt_online if debouncer.has_stable_chatgpt else None,
                checked_at=now,
            )
            status_store.set_state(current_state)

            candidate_events = collect_events(
                prev=previous_state,
                current=current_state,
                notify_on_start=bool(config["notify_on_start"]),
                notify_only_russia_transitions=bool(config["notify_only_russia_transitions"]),
                notify_on_public_ip_change=bool(config.get("notify_on_public_ip_change", True)),
                notify_on_chatgpt_status_change=bool(config.get("notify_on_chatgpt_status_change", True)),
                russia_codes=set(config["russia_country_codes"]),
                russia_names=set(config["russia_country_names"]),
            )

            approved_messages: list[str] = []
            for event in candidate_events:
                allowed, reason = notification_policy.should_send(event, now_ts)
                if allowed:
                    approved_messages.append(event.message)
                else:
                    logger.info(
                        "Notification suppressed (%s) [%s]: %s",
                        reason,
                        event.event_type,
                        event.message.replace("\n", " | "),
                    )

            if approved_messages:
                message = "\n".join(approved_messages)
                try:
                    notify(
                        toaster=toaster,
                        title="Internet Checker",
                        message=message,
                        duration_seconds=int(config["toast_duration_seconds"]),
                    )
                    logger.info("Notification: %s", message.replace("\n", " | "))
                except Exception as exc:
                    logger.error("Toast notification failed: %s", exc)
            else:
                logger.info("No notification. State: %s", snapshot_text(current_state))

            previous_state = current_state
            if run_once:
                break
            wait_for_next_cycle(stop_event, check_now_event, int(config["check_interval_seconds"]))
    except Exception as exc:
        status_store.set_error(type(exc).__name__)
        logger.exception("Fatal error in monitor loop.")
    finally:
        logger.info("Internet checker stopped.")
        stop_event.set()


def run_with_tray(
    config: dict,
    logger: logging.Logger,
    stop_event: threading.Event,
    monitor_thread: threading.Thread,
    status_store: StatusStore,
    check_now_event: threading.Event,
) -> None:
    tray_icon = ClickMenuIcon(
        name="internet-checker",
        icon=create_tray_image(),
        title=tray_tooltip(str(config["tray_icon_tooltip"]), status_store.snapshot()),
        menu=make_tray_menu(
            config=config,
            logger=logger,
            status_store=status_store,
            check_now_event=check_now_event,
            stop_event=stop_event,
        ),
    )

    def watch_monitor() -> None:
        monitor_thread.join()
        if stop_event.is_set():
            try:
                tray_icon.stop()
            except Exception:
                pass

    def refresh_tooltip() -> None:
        while not stop_event.is_set():
            try:
                tray_icon.title = tray_tooltip(str(config["tray_icon_tooltip"]), status_store.snapshot())
            except Exception:
                pass
            time.sleep(1)

    watcher = threading.Thread(target=watch_monitor, name="tray-monitor-watcher", daemon=True)
    watcher.start()
    refresher = threading.Thread(target=refresh_tooltip, name="tray-tooltip-refresher", daemon=True)
    refresher.start()

    tray_icon.run()
    stop_event.set()
    monitor_thread.join()


def _on_tray_show_status(icon: Icon, logger: logging.Logger, status_store: StatusStore) -> None:
    snapshot = status_store.snapshot()
    if snapshot.state is not None:
        message = snapshot_text(snapshot.state)
    elif snapshot.last_error:
        message = f"Error: {snapshot.last_error}"
    elif snapshot.checking:
        message = "Checking..."
    else:
        message = "No data yet."

    try:
        icon.notify(message, "Internet Checker")
    except Exception as exc:
        logger.info("Tray status notification failed: %s", exc)


def _on_tray_check_now(
    icon: Icon,
    logger: logging.Logger,
    status_store: StatusStore,
    check_now_event: threading.Event,
) -> None:
    logger.info("Manual check requested from tray.")
    status_store.set_checking(True)
    check_now_event.set()
    try:
        icon.update_menu()
        icon.title = "Internet Checker - checking"
    except Exception:
        pass


def _on_tray_open_log(logger: logging.Logger, log_path: Path) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.touch()
        os.startfile(str(log_path))
    except Exception as exc:
        logger.info("Opening log file failed: %s", exc)


def _on_tray_exit(icon: Icon, logger: logging.Logger, stop_event: threading.Event) -> None:
    logger.info("Tray exit requested.")
    stop_event.set()
    icon.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Internet and VPN country monitor for Windows notifications.")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file (JSON). If missing, defaults will be used.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check cycle and exit.",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Run without tray icon (useful for terminal/debug runs).",
    )
    return parser.parse_args()


def resolve_preliminary_mutex_name(config_path: Path) -> str:
    mutex_name = str(DEFAULT_CONFIG["single_instance_mutex_name"])

    if not config_path.exists():
        return mutex_name

    try:
        with config_path.open("r", encoding="utf-8") as file:
            raw_config = json.load(file)
        if isinstance(raw_config, dict):
            custom_mutex = raw_config.get("single_instance_mutex_name")
            if isinstance(custom_mutex, str) and custom_mutex.strip():
                mutex_name = custom_mutex.strip()
    except Exception:
        # Keep default mutex when config is invalid/unreadable during pre-lock stage.
        pass

    return mutex_name


def main() -> None:
    args = parse_args()
    app_base_dir = get_app_base_dir()
    data_dir = resolve_data_dir(app_base_dir)
    config_path = resolve_config_path(args.config, data_dir)

    single_instance = SingleInstance(resolve_preliminary_mutex_name(config_path))
    if not single_instance.acquire():
        return

    stop_event = threading.Event()
    check_now_event = threading.Event()
    status_store = StatusStore()
    logger: Optional[logging.Logger] = None

    try:
        save_example_config(data_dir / "config.example.json")
        config = finalize_runtime_paths(load_config(config_path), data_dir)

        logger = setup_logging(config)
        if config_path.exists():
            logger.info("Config loaded from: %s", config_path.resolve())
        else:
            logger.info("Config file not found (%s). Using defaults.", config_path.resolve())

        if args.once or args.no_tray:
            run_monitor_loop(
                config=config,
                logger=logger,
                stop_event=stop_event,
                run_once=args.once,
                status_store=status_store,
                check_now_event=check_now_event,
            )
        else:
            monitor_thread = threading.Thread(
                target=run_monitor_loop,
                kwargs={
                    "config": config,
                    "logger": logger,
                    "stop_event": stop_event,
                    "run_once": False,
                    "status_store": status_store,
                    "check_now_event": check_now_event,
                },
                name="internet-monitor",
                daemon=True,
            )
            monitor_thread.start()
            run_with_tray(
                config=config,
                logger=logger,
                stop_event=stop_event,
                monitor_thread=monitor_thread,
                status_store=status_store,
                check_now_event=check_now_event,
            )
    except KeyboardInterrupt:
        if logger:
            logger.info("Stopping on keyboard interrupt.")
        stop_event.set()
    finally:
        single_instance.release()


if __name__ == "__main__":
    main()


