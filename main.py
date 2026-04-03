import argparse
import copy
import ctypes
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem
from windows_toasts import Toast, ToastDuration, WindowsToaster


DEFAULT_CONFIG = {
    "check_interval_seconds": 5,
    "request_timeout_seconds": 3,
    "connectivity_success_confirmations": 1,
    "connectivity_fail_confirmations": 2,
    "country_confirmations": 1,
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
    },
    "dedup_window_seconds": 30,
    "single_instance_mutex_name": "Global\\InternetCheckerMutex",
    "log_file_path": "logs/internet-checker.log",
    "log_max_bytes": 1_000_000,
    "log_backup_count": 5,
    "log_to_console": True,
    "connectivity_attempts": 2,
    "connectivity_urls": [
        "https://clients3.google.com/generate_204",
        "https://www.msftconnecttest.com/connecttest.txt",
        "https://cloudflare.com/cdn-cgi/trace",
    ],
    "country_lookup_urls": [
        "https://ipapi.co/json/",
        "http://ip-api.com/json/?fields=status,country,countryCode,query",
        "https://ipwho.is/",
    ],
    "country_lookup_no_cache": True,
    "notify_on_public_ip_change": True,
    "tray_icon_tooltip": "Internet Checker",
    "tray_exit_label": "Exit",
}


@dataclass
class NetworkState:
    online: bool
    country_name: Optional[str]
    country_code: Optional[str]
    public_ip: Optional[str]
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
    def __init__(self, online_success: int, online_fail: int, country_confirmations: int):
        self._online_success_required = max(1, int(online_success))
        self._online_fail_required = max(1, int(online_fail))
        self._country_required = max(1, int(country_confirmations))

        self._stable_online: Optional[bool] = None
        self._stable_country_name: Optional[str] = None
        self._stable_country_code: Optional[str] = None
        self._stable_public_ip: Optional[str] = None

        self._online_success_streak = 0
        self._online_fail_streak = 0

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

    def update(
        self,
        raw_online: bool,
        raw_country_name: Optional[str],
        raw_country_code: Optional[str],
        raw_public_ip: Optional[str],
    ) -> DebounceUpdate:
        raw_country_name, raw_country_code = self._normalize_country(raw_country_name, raw_country_code)
        raw_public_ip = self._normalize_public_ip(raw_public_ip)

        online_changed = False
        country_changed = False

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
                return DebounceUpdate(online_changed=False, country_changed=False)
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
            return DebounceUpdate(online_changed=online_changed, country_changed=False)

        if raw_public_ip:
            self._stable_public_ip = raw_public_ip

        raw_key = self._country_key(raw_country_name, raw_country_code)
        stable_key = self._country_key(self._stable_country_name, self._stable_country_code)

        if raw_key is None:
            self._reset_country_candidate()
            return DebounceUpdate(online_changed=online_changed, country_changed=False)

        if stable_key is None:
            self._stable_country_name = raw_country_name
            self._stable_country_code = raw_country_code
            self._reset_country_candidate()
            return DebounceUpdate(online_changed=online_changed, country_changed=False)

        if raw_key == stable_key:
            if raw_country_name and raw_country_name != self._stable_country_name:
                self._stable_country_name = raw_country_name
            if raw_country_code and raw_country_code != self._stable_country_code:
                self._stable_country_code = raw_country_code
            self._reset_country_candidate()
            return DebounceUpdate(online_changed=online_changed, country_changed=False)

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

        return DebounceUpdate(online_changed=online_changed, country_changed=country_changed)


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

    config["check_interval_seconds"] = max(1, int(config["check_interval_seconds"]))
    config["request_timeout_seconds"] = max(1, int(config["request_timeout_seconds"]))
    config["connectivity_attempts"] = max(1, int(config.get("connectivity_attempts", 1)))
    config["connectivity_success_confirmations"] = max(1, int(config["connectivity_success_confirmations"]))
    config["connectivity_fail_confirmations"] = max(1, int(config["connectivity_fail_confirmations"]))
    config["country_confirmations"] = max(1, int(config["country_confirmations"]))
    config["dedup_window_seconds"] = max(0, int(config["dedup_window_seconds"]))
    config["log_max_bytes"] = max(1024, int(config["log_max_bytes"]))
    config["log_backup_count"] = max(1, int(config["log_backup_count"]))
    config["toast_duration_seconds"] = max(1, int(config["toast_duration_seconds"]))
    config["tray_icon_tooltip"] = str(config.get("tray_icon_tooltip", "Internet Checker")).strip() or "Internet Checker"
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


def check_connectivity(
    session: requests.Session,
    urls: list[str],
    timeout_seconds: int,
    attempts: int,
) -> bool:
    for attempt_index in range(max(1, attempts)):
        for url in urls:
            try:
                response = session.get(url, timeout=timeout_seconds)
                if response.status_code < 500:
                    return True
            except requests.RequestException:
                pass
        if attempt_index < attempts - 1:
            time.sleep(0.2)
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


def fetch_country(
    session: requests.Session,
    urls: list[str],
    timeout_seconds: int,
    logger: logging.Logger,
    no_cache: bool,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    errors: list[str] = []
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"} if no_cache else None

    for url in urls:
        request_url = build_country_request_url(url, no_cache=no_cache)
        try:
            response = session.get(request_url, timeout=timeout_seconds, headers=headers)
            response.raise_for_status()
            data = response.json()
            country_name, country_code, public_ip = parse_country_payload(data)
            if country_name or country_code or public_ip:
                return country_name, country_code, public_ip, url
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url} ({type(exc).__name__})")

    if errors:
        logger.info("Country lookup failed on all APIs: %s", "; ".join(errors))
    return None, None, None, None


def snapshot_text(state: NetworkState) -> str:
    if not state.online:
        return "Internet: OFFLINE"
    country = state.country_name or state.country_code or "Unknown"
    ip_suffix = f" | IP: {state.public_ip}" if state.public_ip else ""
    return f"Internet: ONLINE | Country: {country}{ip_suffix}"


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

    if prev.online != current.online:
        if notify_only_russia_transitions:
            return events
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


def run_monitor_loop(config: dict, logger: logging.Logger, stop_event: threading.Event, run_once: bool) -> None:
    session = requests.Session()
    toaster = WindowsToaster("Internet Checker")
    debouncer = StateDebouncer(
        online_success=int(config["connectivity_success_confirmations"]),
        online_fail=int(config["connectivity_fail_confirmations"]),
        country_confirmations=int(config["country_confirmations"]),
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
            timeout = int(config["request_timeout_seconds"])

            raw_online = check_connectivity(
                session=session,
                urls=config["connectivity_urls"],
                timeout_seconds=timeout,
                attempts=int(config["connectivity_attempts"]),
            )
            raw_country_name, raw_country_code, raw_public_ip, country_source = (None, None, None, None)
            if raw_online:
                country_urls = list(config["country_lookup_urls"])
                if last_country_source and last_country_source in country_urls:
                    country_urls = [last_country_source] + [url for url in country_urls if url != last_country_source]

                raw_country_name, raw_country_code, raw_public_ip, country_source = fetch_country(
                    session=session,
                    urls=country_urls,
                    timeout_seconds=timeout,
                    logger=logger,
                    no_cache=bool(config.get("country_lookup_no_cache", True)),
                )
                if country_source and country_source != last_country_source:
                    logger.info("Country API source selected: %s", country_source)
                    last_country_source = country_source

            debouncer.update(raw_online, raw_country_name, raw_country_code, raw_public_ip)
            if not debouncer.has_stable_online:
                logger.info("No notification. State: warming up connectivity checks.")
                if run_once:
                    break
                stop_event.wait(int(config["check_interval_seconds"]))
                continue

            current_state = NetworkState(
                online=debouncer.stable_online,
                country_name=debouncer.stable_country_name if debouncer.stable_online else None,
                country_code=debouncer.stable_country_code if debouncer.stable_online else None,
                public_ip=debouncer.stable_public_ip if debouncer.stable_online else None,
                checked_at=now,
            )

            candidate_events = collect_events(
                prev=previous_state,
                current=current_state,
                notify_on_start=bool(config["notify_on_start"]),
                notify_only_russia_transitions=bool(config["notify_only_russia_transitions"]),
                notify_on_public_ip_change=bool(config.get("notify_on_public_ip_change", True)),
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
            stop_event.wait(int(config["check_interval_seconds"]))
    except Exception:
        logger.exception("Fatal error in monitor loop.")
    finally:
        session.close()
        logger.info("Internet checker stopped.")
        stop_event.set()


def run_with_tray(
    config: dict,
    logger: logging.Logger,
    stop_event: threading.Event,
    monitor_thread: threading.Thread,
) -> None:
    tray_icon = Icon(
        name="internet-checker",
        icon=create_tray_image(),
        title=str(config["tray_icon_tooltip"]),
        menu=Menu(
            MenuItem(
                str(config["tray_exit_label"]),
                lambda icon, item: _on_tray_exit(icon, logger, stop_event),
                default=True,
            )
        ),
    )

    def watch_monitor() -> None:
        monitor_thread.join()
        if stop_event.is_set():
            try:
                tray_icon.stop()
            except Exception:
                pass

    watcher = threading.Thread(target=watch_monitor, name="tray-monitor-watcher", daemon=True)
    watcher.start()

    tray_icon.run()
    stop_event.set()
    monitor_thread.join()


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
            run_monitor_loop(config=config, logger=logger, stop_event=stop_event, run_once=args.once)
        else:
            monitor_thread = threading.Thread(
                target=run_monitor_loop,
                kwargs={
                    "config": config,
                    "logger": logger,
                    "stop_event": stop_event,
                    "run_once": False,
                },
                name="internet-monitor",
                daemon=True,
            )
            monitor_thread.start()
            run_with_tray(config=config, logger=logger, stop_event=stop_event, monitor_thread=monitor_thread)
    except KeyboardInterrupt:
        if logger:
            logger.info("Stopping on keyboard interrupt.")
        stop_event.set()
    finally:
        single_instance.release()


if __name__ == "__main__":
    main()


