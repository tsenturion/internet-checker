"""Окно подробной статистики сетевых проверок."""

from __future__ import annotations

import csv
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, ttk

from network_metrics import empty_totals, format_bytes, merge_totals


def overview_text(snapshot: dict) -> str:
    total = snapshot["totals"]
    elapsed = snapshot["elapsed"]
    today = empty_totals()
    for values in snapshot["days"].get(datetime.now().date().isoformat(), {}).values():
        merge_totals(today, values)
    percent = total["failures"] / max(1, total["requests"]) * 100
    average = total["duration"] / max(1, total["requests"]) * 1000
    rate = snapshot["rx_rate"] + snapshot["tx_rate"]
    return (
        f"Сбор с {snapshot['started_at']:%d.%m.%Y %H:%M:%S} · длительность {elapsed / 60:.1f} мин\n\n"
        f"За запуск: приём ≈ {format_bytes(total['rx'])} · отправка ≈ {format_bytes(total['tx'])}\n"
        f"Итого ≈ {format_bytes(total['rx'] + total['tx'])}\n"
        f"За сегодня, включая прошлые запуски: ≈ {format_bytes(today['rx'] + today['tx'])}\n\n"
        f"Средняя скорость за последние 60 с (по завершённым проверкам):\n"
        f"  Приём ≈ {format_bytes(snapshot['rx_rate'])}/с · отправка ≈ {format_bytes(snapshot['tx_rate'])}/с\n"
        f"Средняя скорость за запуск: ≈ {format_bytes((total['rx'] + total['tx']) / elapsed)}/с\n"
        f"Пик секундного учёта за последнюю минуту: приём ≈ {format_bytes(snapshot['rx_peak'])}/с, "
        f"отправка ≈ {format_bytes(snapshot['tx_peak'])}/с\n"
        f"При сохранении текущего темпа: ≈ {format_bytes(rate * 3600)}/час · "
        f"≈ {format_bytes(rate * 86400)}/сутки\n\n"
        f"Попыток проверки: {total['requests']} · успешно: {total['successes']} · "
        f"неуспешно: {total['failures']} ({percent:.1f}%)\n"
        f"Таймаутов: {total['timeouts']} · HTTP-редиректов: {total['redirects']} · "
        f"сейчас выполняется: {snapshot['active']}\n"
        f"Частота: {total['requests'] / elapsed * 60:.1f} проверок/мин\n"
        f"Среднее время проверки: {average:.0f} мс · максимум: {total['longest'] * 1000:.0f} мс\n\n"
        f"Прочитанные тела HTTP-ответов и данные MTProto: {format_bytes(total['rx_body'])}\n"
        f"Тела отправленных HTTP-запросов и данные MTProto: {format_bytes(total['tx_body'])}\n"
        f"HTTP-заголовки: приём ≈ {format_bytes(total['rx_headers'])}, "
        f"отправка ≈ {format_bytes(total['tx_headers'])}\n"
        f"Неуспешных попыток без доступного счётчика полученных данных: {total['unmeasured']}\n\n"
        "Это трафик Internet Checker. Сбор использует уже выполняемые проверки.\n"
        "HTTP-тела учитываются до распаковки; заголовки оценены по их длине.\n"
        "TLS, DNS, обмен с прокси, TCP/IP и повторная передача пакетов не учитываются.\n"
        "Данные, оставшиеся в сокете после раннего закрытия ответа, могут не попасть в счётчик.\n"
        "Поэтому фактический расход соединения может быть выше показанного.\n"
        "История сохраняется каждые 30 с и при обычном выходе; хранится до 30 дней."
    )


class MetricsWindow:
    def __init__(self, root, collector, logger):
        self.collector = collector
        self.logger = logger
        self.window = tk.Toplevel(root)
        self.window.title("Internet Checker — использование сети")
        width = min(1100, max(760, root.winfo_screenwidth() - 80))
        height = min(650, max(480, root.winfo_screenheight() - 100))
        self.window.geometry(f"{width}x{height}+40+40")
        self.window.minsize(760, 480)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.window.bind("<Escape>", lambda _event: self.window.withdraw())
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        overview = ttk.Frame(notebook)
        resources = ttk.Frame(notebook)
        history = ttk.Frame(notebook)
        notebook.add(overview, text="Обзор")
        notebook.add(resources, text="По ресурсам")
        notebook.add(history, text="История по дням")
        self.overview = self._text(overview)
        self.resource_tree = self._tree(resources, (
            ("resource", "Ресурс", 280), ("requests", "Попытки", 65),
            ("failures", "Ошибки", 65), ("timeouts", "Таймауты", 70),
            ("rx", "Приём ≈", 95), ("tx", "Отправка ≈", 95),
            ("share", "Доля", 60), ("average", "Среднее, мс", 90),
            ("p95", "P95, мс", 70), ("longest", "Макс., мс", 80),
        ), height=11)
        self.resource_tree.bind("<<TreeviewSelect>>", lambda _event: self._resource_details())
        self.details = self._text(resources, height=9)
        self.history_tree = self._tree(history, (
            ("day", "Дата", 130), ("requests", "Попытки", 90),
            ("failures", "Ошибки", 90), ("timeouts", "Таймауты", 90),
            ("rx", "Приём ≈", 130), ("tx", "Отправка ≈", 130),
            ("total", "Итого ≈", 130),
        ))
        ttk.Label(history, text="Выберите день: ниже показан расход по ресурсам.").pack(anchor="w", padx=8)
        self.history_details = self._text(history, height=10)
        self.history_tree.bind("<<TreeviewSelect>>", lambda _event: self._history_details())
        ttk.Button(self.window, text="Сохранить историю по ресурсам в CSV…", command=self._export).pack(
            anchor="e", padx=10, pady=(0, 10))
        self.snapshot = None
        self._refresh()

    @staticmethod
    def _text(parent, height=None):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=5, pady=5)
        text = tk.Text(frame, wrap="word", font=("Segoe UI", 10), relief="flat", height=height or 20)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        return text

    @staticmethod
    def _tree(parent, columns, height=10):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=5, pady=5)
        tree = ttk.Treeview(frame, columns=[item[0] for item in columns], show="headings", height=height)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=55, anchor="w" if key in {"resource", "day"} else "e")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        return tree

    @staticmethod
    def _set_text(widget, value):
        if widget.get("1.0", "end-1c") == value:
            return
        position = widget.yview()
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")
        if position:
            widget.yview_moveto(position[0])

    @staticmethod
    def _update_rows(tree, rows):
        keys = {key for key, _values in rows}
        for key in tree.get_children():
            if key not in keys:
                tree.delete(key)
        for position, (key, values) in enumerate(rows):
            if tree.exists(key):
                tree.item(key, values=values)
                tree.move(key, "", position)
            else:
                tree.insert("", "end", iid=key, values=values)

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def _refresh(self):
        if self.window.winfo_viewable():
            self.snapshot = self.collector.snapshot()
            self._set_text(self.overview, overview_text(self.snapshot))
            total_bytes = max(1, self.snapshot["totals"]["rx"] + self.snapshot["totals"]["tx"])
            rows = []
            for resource, values in sorted(self.snapshot["session"].items(),
                                           key=lambda item: item[1]["rx"] + item[1]["tx"], reverse=True):
                latencies = self.snapshot["latencies"].get(resource, [])
                p95 = latencies[max(0, (len(latencies) * 95 + 99) // 100 - 1)] if latencies else 0
                rows.append((resource, (resource, values["requests"], values["failures"], values["timeouts"],
                            format_bytes(values["rx"]), format_bytes(values["tx"]),
                            f"{(values['rx'] + values['tx']) / total_bytes * 100:.1f}%",
                            f"{values['duration'] / max(1, values['requests']) * 1000:.0f}",
                            f"{p95 * 1000:.0f}", f"{values['longest'] * 1000:.0f}")))
            self._update_rows(self.resource_tree, rows)
            history_rows = []
            for day, resources in sorted(self.snapshot["days"].items(), reverse=True):
                values = empty_totals()
                for resource_values in resources.values():
                    merge_totals(values, resource_values)
                history_rows.append((day, (day, values["requests"], values["failures"], values["timeouts"],
                                    format_bytes(values["rx"]), format_bytes(values["tx"]),
                                    format_bytes(values["rx"] + values["tx"]))))
            self._update_rows(self.history_tree, history_rows)
            self._resource_details()
            self._history_details()
        self.window.after(1000, self._refresh)

    def _resource_details(self):
        selected = self.resource_tree.selection()
        if not selected or self.snapshot is None:
            self._set_text(self.details, "Выберите ресурс для детализации. P95 рассчитан по последним 200 проверкам ресурса.")
            return
        resource = selected[0]
        values = self.snapshot["session"].get(resource)
        if values is None:
            return
        latencies = self.snapshot["latencies"].get(resource, [])
        middle = len(latencies) // 2
        median = ((latencies[middle] + latencies[(len(latencies) - 1) // 2]) / 2) if latencies else 0
        minimum = latencies[0] if latencies else 0
        self._set_text(self.details,
            f"{resource}\n"
            f"Успешно: {values['successes']} · ошибок: {values['failures']} · таймаутов: {values['timeouts']}\n"
            f"Прочитанные данные: {format_bytes(values['rx_body'])} · отправленные тела: {format_bytes(values['tx_body'])}\n"
            f"Заголовки: приём ≈ {format_bytes(values['rx_headers'])} · отправка ≈ {format_bytes(values['tx_headers'])}\n"
            f"Редиректов: {values['redirects']} · сбоев без измерения приёма: {values['unmeasured']}\n"
            f"В среднем на попытку ≈ {format_bytes((values['rx'] + values['tx']) / max(1, values['requests']))}\n"
            f"Последние 200 попыток: минимум {minimum * 1000:.0f} мс · медиана {median * 1000:.0f} мс\n"
            "Время включает соединение, ожидание ответа и проверку его содержимого; P95 — время, в которое уложились 95% попыток.")

    def _history_details(self):
        selected = self.history_tree.selection()
        if not selected or self.snapshot is None:
            self._set_text(self.history_details, "История включает предыдущие запуски приложения.")
            return
        day = selected[0]
        lines = [f"{day} — расход по ресурсам:"]
        for resource, values in sorted(self.snapshot["days"].get(day, {}).items(),
                                      key=lambda item: item[1]["rx"] + item[1]["tx"], reverse=True):
            lines.append(f"{resource}: приём ≈ {format_bytes(values['rx'])}, отправка ≈ {format_bytes(values['tx'])}; "
                         f"попыток {values['requests']}, ошибок {values['failures']}")
        self._set_text(self.history_details, "\n".join(lines))

    def _export(self):
        path = filedialog.asksaveasfilename(parent=self.window, title="Сохранить статистику",
                                           defaultextension=".csv", initialfile="сетевой-трафик.csv",
                                           filetypes=[("Таблица CSV", "*.csv")])
        if not path:
            return
        try:
            snapshot = self.collector.snapshot()
            with open(path, "w", newline="", encoding="utf-8-sig") as output:
                writer = csv.writer(output, delimiter=";")
                writer.writerow(("Дата", "Ресурс", "Попытки", "Успешно", "Ошибки", "Таймауты", "Приём оценка Б",
                                 "Отправка оценка Б", "Принятые тела Б", "Отправленные тела Б", "Редиректы"))
                for day, resources in sorted(snapshot["days"].items()):
                    for resource, values in sorted(resources.items()):
                        writer.writerow((day, resource, *(values[key] for key in (
                            "requests", "successes", "failures", "timeouts", "rx", "tx", "rx_body", "tx_body", "redirects"))))
            self.logger.info("История сетевого трафика экспортирована в CSV")
        except OSError:
            self.logger.exception("Не удалось экспортировать статистику сети")
