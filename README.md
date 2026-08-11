# Internet Checker

Кросплатформенная утилита для Windows и Linux, которая отслеживает:

- доступность интернета (`online/offline`);
- страну подключения (удобно для контроля VPN/маршрутизации);
- доступность нужных сайтов и сервисов отдельными проверками.

По умолчанию приложение пытается работать в системном трее. Клик по иконке открывает меню со статусом, ручной проверкой, открытием лога и выходом. Если на Linux нет GUI-сессии или зависимостей для трея, приложение автоматически работает без трея.

## Возможности

- Режим трея по умолчанию (`python main.py`) при наличии GUI.
- Защита от запуска второй копии: Windows mutex, Linux/macOS lock-файл.
- Периодическая проверка сети с несколькими endpoint и повторами.
- Параллельные быстрые проверки сети, страны и настроенных сервисов.
- Уведомления о смене статуса сети и страны.
- Отдельные уведомления только о недоступных сервисах.
- По умолчанию уведомления о стране ограничены переходами, связанными с Россией.
- Ротация логов.
- Runtime-файлы (`config.example.json`, `config.json`, `logs`) берутся из папки приложения, а при невозможности записи — из пользовательского каталога приложения.

## Требования

- Windows 10/11 или Linux
- Python 3.10+
- Для Linux-уведомлений: `notify-send` из `libnotify-bin`/`libnotify`.
- Для Linux-трея: установленный `tkinter`, доступная GUI-сессия (`DISPLAY` или `WAYLAND_DISPLAY`) и backend, поддерживаемый `pystray`.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Для Debian/Ubuntu при необходимости:

```bash
sudo apt install python3-tk libnotify-bin
```

## Настройка

Создайте локальный конфиг из примера:

```powershell
Copy-Item config.example.json config.json
```

Linux:

```bash
cp config.example.json config.json
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

Linux-команды аналогичны, обычно через `python3`:

```bash
python3 main.py --no-tray
python3 main.py --once
python3 main.py --config ./config.json
```

## Сборка EXE для Windows

Сборка standalone EXE:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Результат:

- `dist\InternetChecker.exe`

## Release через GitHub Actions

EXE можно собирать не локально, а в GitHub Actions. Workflow `.github/workflows/release.yml` собирает `dist\InternetChecker.exe` на Windows runner и публикует файл в GitHub Release.

Автоматический релиз по тегу:

```powershell
git tag v0.1.0
git push origin v0.1.0
```

Ручной релиз: GitHub -> Actions -> Release -> Run workflow, затем указать tag, например `v0.1.0`.

## Установка в автозапуск

Этот способ относится к Windows.

Копирует standalone EXE прямо в папку Startup:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup.ps1
```

Скрипт также останавливает старый процесс, удаляет устаревший `InternetChecker.lnk` и запускает новое приложение.

## Ключевые параметры `config.json`

- `check_interval_seconds`, `request_timeout_seconds`, `service_request_timeout_seconds`: интервал опроса, общий таймаут запросов и отдельный более терпимый таймаут проверки сервисов. Таймауты поддерживают дробные значения.
- `connectivity_urls`, `connectivity_attempts`: источники и надежность проверки онлайн-статуса. Поддерживаются HTTP(S) URL и TCP endpoints вида `tcp://8.8.8.8:53`.
- `country_lookup_urls`, `country_lookup_no_cache`: API и режим запрета кеша для гео-определения.
- `service_checks`, `service_success_confirmations`, `service_fail_confirmations`: отдельные HTTP-проверки доступности сервисов (`GET` или `HEAD`; `must_contain` проверяет маркер в ответе). По умолчанию краткий сбой не переводит сервис в `OFFLINE`: нужно несколько подряд неудачных проверок.
- `notify_only_russia_transitions`, `russia_country_codes`, `russia_country_names`: логика фильтрации уведомлений по РФ.
- `notify_on_service_status_change`: уведомлять ли о недоступности настроенных сервисов. Возврат сервиса в `ONLINE` отдельным уведомлением не показывается.
- `show_app_started_notification`, `app_started_title`, `app_started_message`: стартовое уведомление.
- `notification_cooldowns_seconds`, `dedup_window_seconds`: защита от спама уведомлениями.
- `single_instance_mutex_name`: имя Windows mutex и основа имени lock-файла на Linux/macOS.
- `log_file_path`, `log_max_bytes`, `log_backup_count`, `log_to_console`: параметры логирования.
- `tray_icon_tooltip`, `tray_show_status_label`, `tray_check_now_label`, `tray_open_log_label`, `tray_exit_label`: текст в трее.

Пример добавления сервиса:

```json
{
  "id": "example",
  "name": "Example",
  "probe_urls": [
    {
      "url": "https://example.com/",
      "method": "GET",
      "must_contain": "Example"
    }
  ]
}
```

Если все сервисы доступны, приложение показывает общий статус `Services: ONLINE`. Если какой-то сервис недоступен, в трее и уведомлении перечисляются только недоступные сервисы.
