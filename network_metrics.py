"""Учёт трафика проверок без дополнительных сетевых запросов."""

from __future__ import annotations

import copy
import functools
import json
import logging
import math
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests


_collector = None
_local = threading.local()


def configure_metrics(collector) -> None:
    global _collector
    _collector = collector


def empty_totals() -> dict:
    return dict(requests=0, successes=0, failures=0, timeouts=0, redirects=0,
                rx=0, tx=0, rx_body=0, tx_body=0, rx_headers=0, tx_headers=0,
                duration=0.0, longest=0.0, unmeasured=0)


def merge_totals(target: dict, source: dict) -> None:
    for key in target:
        target[key] = max(target[key], source[key]) if key == "longest" else target[key] + source[key]


@dataclass
class Measurement:
    rx_body: int = 0
    tx_body: int = 0
    rx_headers: int = 0
    tx_headers: int = 0
    redirects: int = 0
    responses: int = 0
    error: str = ""


class TrafficCollector:
    def __init__(self, state_path: Path | None = None, labels: dict | None = None,
                 logger: logging.Logger | None = None, clock=time.monotonic):
        self.path = state_path
        self.labels = labels or {}
        self.logger = logger or logging.getLogger("internet_checker")
        self.clock = clock
        self.started = clock()
        self.started_at = datetime.now()
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.session = {}
        self.daily = {}
        self.events = deque(maxlen=5000)
        self.latencies = {}
        self.active = 0
        self._load()

    def _prune(self) -> None:
        cutoff = (datetime.now().date() - timedelta(days=29)).isoformat()
        today = datetime.now().date().isoformat()
        self.daily = {day: values for day, values in self.daily.items() if cutoff <= day <= today}

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("days"), dict):
                raise ValueError("Некорректный формат статистики")
            for day, resources in data["days"].items():
                datetime.strptime(day, "%Y-%m-%d")
                if not isinstance(resources, dict):
                    raise ValueError("Некорректная статистика ресурсов")
                for key, totals in resources.items():
                    if not isinstance(key, str) or not isinstance(totals, dict):
                        raise ValueError("Некорректная запись статистики")
                    if any(not isinstance(totals.get(field), (int, float)) or not math.isfinite(totals[field])
                           or totals[field] < 0
                           for field in empty_totals()):
                        raise ValueError("Некорректный счётчик статистики")
            self.daily = data["days"]
            self._prune()
        except (OSError, ValueError, TypeError):
            self.logger.warning("Не удалось прочитать историю сетевого трафика; начата новая статистика")

    def save(self) -> None:
        if not self.path:
            return
        with self.save_lock:
            with self.lock:
                self._prune()
                data = {"version": 1, "days": copy.deepcopy(self.daily)}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                self.logger.warning("Не удалось сохранить историю сетевого трафика")

    def resource(self, category: str, url: str) -> str:
        parsed = urlparse(url)
        host = parsed.hostname or "неизвестный адрес"
        label = self.labels.get(host, host)
        return f"{category} · {label} ({host})" if label != host else f"{category} · {host}"

    def begin(self) -> None:
        with self.lock:
            self.active += 1

    def finish(self, resource: str, measured: Measurement, success: bool, duration: float,
               timed_out: bool = False) -> None:
        totals = empty_totals()
        totals.update(requests=1, successes=int(success), failures=int(not success),
                      timeouts=int(timed_out), redirects=measured.redirects,
                      rx_body=measured.rx_body, tx_body=measured.tx_body,
                      rx_headers=measured.rx_headers, tx_headers=measured.tx_headers,
                      rx=measured.rx_body + measured.rx_headers,
                      tx=measured.tx_body + measured.tx_headers,
                      duration=duration, longest=duration,
                      unmeasured=int(not success and measured.responses == 0 and measured.rx_body == 0))
        now = self.clock()
        today = datetime.now().date().isoformat()
        with self.lock:
            self.active = max(0, self.active - 1)
            merge_totals(self.session.setdefault(resource, empty_totals()), totals)
            merge_totals(self.daily.setdefault(today, {}).setdefault(resource, empty_totals()), totals)
            self.latencies.setdefault(resource, deque(maxlen=200)).append(duration)
            self.events.append((now, resource, totals["rx"], totals["tx"]))

    def snapshot(self) -> dict:
        now = self.clock()
        with self.lock:
            self._prune()
            session = copy.deepcopy(self.session)
            days = copy.deepcopy(self.daily)
            active = self.active
            events = list(self.events)
            latencies = {key: sorted(values) for key, values in self.latencies.items()}
        totals = empty_totals()
        for values in session.values():
            merge_totals(totals, values)
        elapsed = max(0.001, now - self.started)
        recent = [(stamp, rx, tx) for stamp, _key, rx, tx in events if now - stamp <= 60]
        rx_rate = sum(rx for _stamp, rx, _tx in recent) / min(60, elapsed)
        tx_rate = sum(tx for _stamp, _rx, tx in recent) / min(60, elapsed)
        buckets = {}
        for stamp, rx, tx in recent:
            pair = buckets.setdefault(int(stamp), [0, 0])
            pair[0] += rx
            pair[1] += tx
        return dict(session=session, days=days, totals=totals, active=active, elapsed=elapsed,
                    started_at=self.started_at, rx_rate=rx_rate, tx_rate=tx_rate,
                    rx_peak=max((pair[0] for pair in buckets.values()), default=0),
                    tx_peak=max((pair[1] for pair in buckets.values()), default=0),
                    latencies=latencies)

    def run(self, stop_event: threading.Event) -> None:
        self.logger.info("Учёт сетевого трафика включён; история хранится не более 30 дней")
        while not stop_event.wait(30):
            self.save()


def tracked_probe(category: str):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(first, *args, **kwargs):
            collector = _collector
            if collector is None:
                return function(first, *args, **kwargs)
            url = first.get("url", "") if isinstance(first, dict) else first
            measurement = Measurement()
            previous = getattr(_local, "measurement", None)
            _local.measurement = measurement
            started = collector.clock()
            collector.begin()
            success = False
            detail = ""
            try:
                result = function(first, *args, **kwargs)
                success = bool(result[0] or result[1]) if category == "Страна" else bool(result[0])
                detail = str(result[-1] or "")
                return result
            except Exception as exc:
                measurement.error = type(exc).__name__
                raise
            finally:
                timed_out = bool(re.search(r"timeout|лимит|медленный", measurement.error + detail, re.I))
                collector.finish(collector.resource(category, url), measurement, success,
                                 max(0, collector.clock() - started), timed_out)
                _local.measurement = previous
        return wrapped
    return decorate


def _size(value) -> int:
    if isinstance(value, bytes):
        return len(value)
    return len(value.encode("utf-8")) if isinstance(value, str) else 0


def _record_response(response, measurement: Measurement) -> None:
    for item in [*response.history, response]:
        measurement.responses += 1
        measurement.rx_body += int(item.raw.tell())
        request = item.request
        measurement.tx_body += _size(request.body)
        request_headers = sum(_size(f"{key}: {value}\r\n") for key, value in request.headers.items())
        if "Host" not in request.headers:
            request_headers += _size(f"Host: {urlparse(request.url).netloc}\r\n")
        measurement.tx_headers += request_headers + _size(f"{request.method} {request.path_url} HTTP/1.1\r\n\r\n")
        measurement.rx_headers += sum(_size(f"{key}: {value}\r\n") for key, value in item.headers.items())
        measurement.rx_headers += _size(f"HTTP/1.1 {item.status_code} {item.reason}\r\n\r\n")
    measurement.redirects += len(response.history)


@contextmanager
def tracked_request(method: str, url: str, **kwargs):
    response = None
    measurement = getattr(_local, "measurement", None)
    try:
        response = requests.request(method, url, **kwargs)
        yield response
    except Exception as exc:
        if measurement is not None:
            measurement.error = type(exc).__name__
        raise
    finally:
        if response is not None:
            if measurement is not None:
                try:
                    _record_response(response, measurement)
                except (AttributeError, OSError, TypeError, ValueError):
                    logging.getLogger("internet_checker").warning("Не удалось учесть часть HTTP-трафика")
            response.close()


def record_protocol_bytes(rx: int = 0, tx: int = 0) -> None:
    measurement = getattr(_local, "measurement", None)
    if measurement is not None:
        measurement.rx_body += rx
        measurement.tx_body += tx


def format_bytes(value: float) -> str:
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if abs(value) < 1024 or unit == "ТиБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{value:.0f} Б"
        value /= 1024


def summary_lines(snapshot: dict) -> list[str]:
    totals = snapshot["totals"]
    return [f"Трафик приложения ≈ {format_bytes(totals['rx'] + totals['tx'])} за запуск",
            f"Приём ≈ {format_bytes(snapshot['rx_rate'])}/с · отправка ≈ {format_bytes(snapshot['tx_rate'])}/с"]
