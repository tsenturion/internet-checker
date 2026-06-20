# Internet Checker (Windows)

Утилита для Windows, которая отслеживает:

- доступность интернета (`online/offline`);
- страну и публичный IP (удобно для контроля VPN/маршрутизации);
- доступность ChatGPT отдельной проверкой.

По умолчанию приложение работает в системном трее. Клик по иконке открывает меню со статусом, ручной проверкой, открытием лога и выходом.

## Возможности

- Режим трея по умолчанию (`python main.py`).
- Защита от запуска второй копии (single instance через mutex).
- Периодическая проверка сети с несколькими endpoint и повторами.
- Параллельные быстрые проверки сети, страны/публичного IP и ChatGPT.
- Уведомления о смене статуса сети, страны и публичного IP.
- Отдельные уведомления о смене доступности ChatGPT.
- По умолчанию уведомления о стране ограничены переходами, связанными с Россией.
- Ротация логов.
- Runtime-файлы (`config.example.json`, `config.json`, `logs`) берутся из папки приложения, а не из рабочей директории автозапуска.

## Требования

- Windows 10/11
- Python 3.10+

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Настройка

Создайте локальный конфиг из примера:

```powershell
Copy-Item config.example.json config.json
```

Если `config.json` отсутствует, приложение запускается со значениями по умолчанию.

## Запуск

Обычный запуск (с иконкой в трее):

```powershell
python main.py
```

Запуск без трея (удобно для отладки в терминале):

```powershell
python main.py --no-tray
```

Один цикл проверки и выход:

```powershell
python main.py --once
```

Кастомный путь к конфигу:

```powershell
python main.py --config .\config.json
```

## Сборка EXE

Сборка standalone EXE:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Результат:

- `dist\InternetChecker.exe`

## Установка в автозапуск

Копирует standalone EXE прямо в папку Startup:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup.ps1
```

Скрипт также останавливает старый процесс, удаляет устаревший `InternetChecker.lnk` и запускает новое приложение.

## Ключевые параметры `config.json`

- `check_interval_seconds`, `request_timeout_seconds`: интервал опроса и таймаут запросов. Таймаут поддерживает дробные значения.
- `connectivity_urls`, `connectivity_attempts`: источники и надежность проверки онлайн-статуса.
- `country_lookup_urls`, `country_lookup_no_cache`: API и режим запрета кеша для гео-определения.
- `chatgpt_probe_urls`, `chatgpt_success_confirmations`, `chatgpt_fail_confirmations`: отдельная проверка доступности ChatGPT (`TCP`, `HEAD` или `GET`).
- `notify_only_russia_transitions`, `russia_country_codes`, `russia_country_names`: логика фильтрации уведомлений по РФ.
- `notify_on_public_ip_change`: уведомлять ли при смене публичного IP.
- `notify_on_chatgpt_status_change`: уведомлять ли при смене доступности ChatGPT.
- `show_app_started_notification`, `app_started_title`, `app_started_message`: стартовое уведомление.
- `notification_cooldowns_seconds`, `dedup_window_seconds`: защита от спама уведомлениями.
- `single_instance_mutex_name`: имя mutex для single instance.
- `log_file_path`, `log_max_bytes`, `log_backup_count`, `log_to_console`: параметры логирования.
- `tray_icon_tooltip`, `tray_show_status_label`, `tray_check_now_label`, `tray_open_log_label`, `tray_exit_label`: текст в трее.
