"""Запись трат: в CSV-файл (для проверки) или в Google Таблицу."""
import csv
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import config

log = logging.getLogger("expense_bot")

HEADER = [
    "Дата / Время", "Сумма (тыс.)", "Получатель / Магазин", "Категория",
    "Подкатегория", "Уточнение", "Комментарий", "Исходное СМС / Пуш", "Банк",
]

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def month_title(date: datetime) -> str:
    """Название листа, например 'October 2026' (как в старой таблице)."""
    return f"{MONTHS[date.month - 1]} {date.year}"


class StorageError(Exception):
    """Понятная ошибка подключения хранилища (текст можно показывать пользователю)."""


def service_account_email(path) -> str:
    """Адрес служебного аккаунта из credentials.json (ему нужно открыть доступ к таблице)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("client_email", "")
    except Exception:  # noqa: BLE001
        return ""


def parse_registry_row(row) -> Optional[tuple]:
    """Строка реестра -> (дата, сумма в рублях, получатель) или None, если это не трата (шапка и т.п.)."""
    try:
        raw_date, raw_amount = str(row[0]).strip(), row[1]
        amount = float(str(raw_amount).replace("\xa0", "").replace(" ", "").replace(",", ".")) * 1000.0
    except (ValueError, IndexError):
        return None
    date = None
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            date = datetime.strptime(raw_date, fmt)
            break
        except ValueError:
            continue
    if date is None:
        return None
    return date, amount, str(row[2]) if len(row) > 2 else ""


def _month_titles(start: datetime, end: datetime) -> List[datetime]:
    months, cur = [], datetime(start.year, start.month, 1)
    while cur <= end:
        months.append(cur)
        cur = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return months


class CsvStorage:
    """Простой файл для проверки. Открывается в Excel."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.label = f"файл {self.path.name}"

    def rows_between(self, start: datetime, end: datetime, fresh: bool = False) -> List[tuple]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            parsed = [parse_registry_row(r) for r in csv.reader(f, delimiter=";")]
        return [p for p in parsed if p and start <= p[0] <= end]

    def save(self, date: datetime, row: List) -> None:
        new = not self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            if new:
                writer.writerow(HEADER)
            writer.writerow([str(v).replace(".", ",") if isinstance(v, float) else v for v in row])


class SheetsStorage:
    """Google Таблица.

    Чтобы не упираться в лимит Google (60 чтений в минуту) и не ждать сети на каждом чеке:
    - листы ищутся один раз и запоминаются;
    - строки месяца для проверки на повтор хранятся в памяти и дополняются при каждой записи,
      а целиком перечитываются не чаще раза в ROWS_TTL секунд (вдруг таблицу правили вручную).
    """

    ROWS_TTL = 300   # секунд

    def __init__(self, credentials_file: Path, sheet_name: str):
        import gspread

        self._gspread = gspread
        self.label = f"Google Таблица «{sheet_name}»"
        self._lock = threading.Lock()      # методы вызываются из разных потоков (asyncio.to_thread)
        self._sheets = {}                  # название листа -> лист
        self._rows = {}                    # название листа -> (когда прочитан, [(дата, рубли, получатель)])
        if not Path(credentials_file).exists():
            raise StorageError(
                f"Не найден файл {Path(credentials_file).name} в папке бота. "
                "Скопируйте его туда (он лежит рядом со старым ботом на сервере)."
            )
        email = service_account_email(credentials_file)
        try:
            self.book = gspread.service_account(filename=str(credentials_file)).open(sheet_name)
        except gspread.exceptions.SpreadsheetNotFound:
            raise StorageError(
                f"Таблица «{sheet_name}» не найдена. Проверьте название в settings.txt "
                f"и откройте таблицу с правами редактора для адреса: {email}"
            ) from None
        except gspread.exceptions.APIError as e:
            raise StorageError(
                f"Google не пустил к таблице «{sheet_name}»: {str(e)[:150]}. "
                "Проверьте, что в Google Cloud включены Google Sheets API и Google Drive API."
            ) from None

    def _worksheet(self, title: str, create: bool = False):
        """Лист по названию (из памяти, если уже искали). None, если листа нет и create=False."""
        with self._lock:
            ws = self._sheets.get(title)
        if ws is not None:
            return ws
        try:
            ws = self.book.worksheet(title)
        except self._gspread.exceptions.WorksheetNotFound:
            if not create:
                return None
            ws = self.book.add_worksheet(title=title, rows=1000, cols=len(HEADER))
            ws.append_row(HEADER)
        with self._lock:
            self._sheets[title] = ws
        return ws

    def _forget(self, title: str) -> None:
        with self._lock:
            self._sheets.pop(title, None)
            self._rows.pop(title, None)

    def _month_rows(self, title: str, fresh: bool) -> List[tuple]:
        with self._lock:
            cached = self._rows.get(title)
        if cached is not None and not fresh and time.monotonic() - cached[0] < self.ROWS_TTL:
            return cached[1]
        ws = self._worksheet(title)
        if ws is None:
            rows = []      # листа нет: не запоминаем, чтобы заметить, когда его создадут вручную
        else:
            try:
                values = ws.get_all_values()
            except self._gspread.exceptions.APIError:
                self._forget(title)   # лист могли удалить или переименовать: в следующий раз ищем заново
                raise
            rows = [p for p in (parse_registry_row(r) for r in values) if p]
            with self._lock:
                self._rows[title] = (time.monotonic(), rows)
        return rows

    def rows_between(self, start: datetime, end: datetime, fresh: bool = False) -> List[tuple]:
        """Траты за период. fresh=True перечитывает таблицу (для сверки), иначе может взять из памяти."""
        found = []
        for month in _month_titles(start, end):
            found.extend(p for p in self._month_rows(month_title(month), fresh) if start <= p[0] <= end)
        return found

    def save(self, date: datetime, row: List) -> None:
        title = month_title(date)
        ws = self._worksheet(title, create=True)
        try:
            resp = ws.append_row(row)
        except self._gspread.exceptions.APIError as e:
            # Лист могли удалить или переименовать, пока он был запомнен: Google отвечает 400/404,
            # и строка точно не записана. Ищем лист заново и пробуем ещё раз. Другие ошибки
            # (лимит, сбой Google) не повторяем, чтобы не получить две одинаковые строки.
            self._forget(title)
            if getattr(getattr(e, "response", None), "status_code", None) not in (400, 404):
                raise
            ws = self._worksheet(title, create=True)
            resp = ws.append_row(row)
        self._verify(ws, title, row, resp)
        parsed = parse_registry_row(row)
        if parsed:
            with self._lock:
                cached = self._rows.get(title)
                if cached is not None:
                    cached[1].append(parsed)

    def _verify(self, ws, title: str, row: List, resp) -> None:
        """Перечитывает только что записанную строку: «Записано» только если она реально есть в таблице."""
        try:
            rng = (resp or {}).get("updates", {}).get("updatedRange", "")
        except AttributeError:
            rng = ""
        log.info("Запись в лист «%s», диапазон %s", title, rng or "неизвестен")
        if not rng:
            return  # Google не сообщил диапазон: проверить нечем, не считаем это ошибкой
        try:
            got = ws.get(rng.split("!")[-1])
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось перечитать строку %s: %s", rng, e)
            return
        if not got or not got[0] or len(got[0]) < 3 or str(got[0][2]).strip() != str(row[2]).strip():
            raise StorageError(
                f"строка не появилась в листе «{title}» (диапазон {rng}). Проверьте таблицу"
            )


_storage: Optional[object] = None


def get_storage():
    global _storage
    if _storage is None:
        if config.STORAGE == "sheets":
            _storage = SheetsStorage(config.GOOGLE_CREDENTIALS_FILE, config.SHEET_NAME)
        else:
            _storage = CsvStorage(config.CSV_FILE)
    return _storage


def save_fallback(date: datetime, row: List) -> None:
    """Резервная запись, если таблица недоступна."""
    CsvStorage(config.FALLBACK_FILE).save(date, row)
