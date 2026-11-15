"""Запись трат: в CSV-файл (для проверки) или в Google Таблицу."""
import csv
import json
import logging
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

    def rows_between(self, start: datetime, end: datetime) -> List[tuple]:
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
    def __init__(self, credentials_file: Path, sheet_name: str):
        import gspread

        self._gspread = gspread
        self.label = f"Google Таблица «{sheet_name}»"
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

    def rows_between(self, start: datetime, end: datetime) -> List[tuple]:
        found = []
        for month in _month_titles(start, end):
            try:
                ws = self.book.worksheet(month_title(month))
            except self._gspread.exceptions.WorksheetNotFound:
                continue
            for r in ws.get_all_values():
                p = parse_registry_row(r)
                if p and start <= p[0] <= end:
                    found.append(p)
        return found

    def save(self, date: datetime, row: List) -> None:
        title = month_title(date)
        try:
            ws = self.book.worksheet(title)
        except self._gspread.exceptions.WorksheetNotFound:
            ws = self.book.add_worksheet(title=title, rows=1000, cols=len(HEADER))
            ws.append_row(HEADER)
        resp = ws.append_row(row)
        self._verify(ws, title, row, resp)

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
