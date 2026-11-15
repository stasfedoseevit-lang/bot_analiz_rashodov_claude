"""Запись трат: в CSV-файл (для проверки) или в Google Таблицу."""
import csv
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

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

HINT_DAYS = 92     # подсказка категории по тратам за последние ~3 месяца


class Record(NamedTuple):
    """Строка реестра (уже разобранная)."""
    date: datetime
    rub: float           # сумма в рублях
    merchant: str
    category: str = ""
    sub: str = ""
    ref: str = ""


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


def parse_amount_cell(value) -> float:
    """Ячейка «Сумма (тыс.)» -> тысячи рублей. Понимает и число, и текст «2,8985»."""
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))


def parse_date_cell(value) -> Optional[datetime]:
    raw = str(value).strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_registry_row(row) -> Optional[Record]:
    """Строка реестра -> Record или None, если это не трата (шапка и т.п.)."""
    try:
        amount = parse_amount_cell(row[1]) * 1000.0
    except (ValueError, IndexError):
        return None
    date = parse_date_cell(row[0])
    if date is None:
        return None
    cells = [str(c).strip() if c is not None else "" for c in list(row[2:6])]
    cells += [""] * (4 - len(cells))
    return Record(date, amount, *cells)


def normalize_merchant(name: str) -> str:
    """«ПЯТЁРОЧКА 1234 СПб» и «Пятерочка 5678 спб» -> «пятерочка спб» (для подсказки категории)."""
    s = str(name or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^a-zа-я]+", " ", s).split())


def _month_titles(start: datetime, end: datetime) -> List[datetime]:
    months, cur = [], datetime(start.year, start.month, 1)
    while cur <= end:
        months.append(cur)
        cur = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return months


def _same_row(got: List, row: List) -> bool:
    """Строка в таблице совпадает с нашей (по дате и получателю)."""
    return (len(got) >= 3 and str(got[0]).strip() == str(row[0]).strip()
            and str(got[2]).strip() == str(row[2]).strip())


class _Common:
    """Общее для CSV и Google Таблицы: подсказки категорий и итоги месяца."""

    def rows_between(self, start: datetime, end: datetime, fresh: bool = False) -> List[Record]:
        raise NotImplementedError

    def category_hints(self, now: datetime) -> Dict[str, Tuple[str, str, str]]:
        """Получатель (нормализованный) -> (категория, подкатегория, уточнение) из последней такой траты."""
        hints: Dict[str, Tuple[str, str, str]] = {}
        for r in sorted(self.rows_between(now - timedelta(days=HINT_DAYS), now + timedelta(days=1)),
                        key=lambda r: r.date):
            key = normalize_merchant(r.merchant)
            if key and r.category:
                hints[key] = (r.category, r.sub, r.ref)
        return hints

    def month_records(self, month: datetime) -> List[Record]:
        start = datetime(month.year, month.month, 1)
        end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1) - timedelta(seconds=1)
        return self.rows_between(start, end, fresh=True)


class CsvStorage(_Common):
    """Простой файл для проверки. Открывается в Excel."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.label = f"файл {self.path.name}"
        self._lock = threading.Lock()

    def _read(self) -> List[List[str]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f, delimiter=";"))

    @staticmethod
    def _cells(row: List) -> List:
        return [str(v).replace(".", ",") if isinstance(v, float) else v for v in row]

    def rows_between(self, start: datetime, end: datetime, fresh: bool = False) -> List[Record]:
        with self._lock:
            parsed = [parse_registry_row(r) for r in self._read()]
        return [p for p in parsed if p and start <= p.date <= end]

    def save(self, date: datetime, row: List):
        with self._lock:
            new = not self.path.exists()
            with open(self.path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
                writer = csv.writer(f, delimiter=";")
                if new:
                    writer.writerow(HEADER)
                writer.writerow(self._cells(row))
        return "csv"

    def delete(self, ref, row: List) -> bool:
        """Удаляет последнюю строку, совпадающую с row. True, если удалили."""
        target = [str(c) for c in self._cells(row)]
        with self._lock:
            rows = self._read()
            for i in range(len(rows) - 1, 0, -1):
                if rows[i][:len(target)] == target:
                    del rows[i]
                    self._rewrite(rows)
                    return True
        return False

    def _rewrite(self, rows: List[List[str]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f, delimiter=";").writerows(rows)
        os.replace(tmp, self.path)


class SheetsStorage(_Common):
    """Google Таблица.

    Чтобы не упираться в лимит Google (60 чтений в минуту) и не ждать сети на каждом чеке:
    - листы ищутся один раз и запоминаются;
    - строки месяца хранятся в памяти и дополняются при каждой записи, а целиком
      перечитываются не чаще раза в ROWS_TTL секунд для текущего месяца (вдруг таблицу
      правили вручную) и раз в час для прошлых.
    """

    ROWS_TTL = 300            # секунд, лист текущего месяца
    OLD_ROWS_TTL = 3600       # секунд, листы прошлых месяцев

    def __init__(self, credentials_file: Path, sheet_name: str):
        import gspread

        self._gspread = gspread
        self.label = f"Google Таблица «{sheet_name}»"
        self._lock = threading.Lock()      # методы вызываются из разных потоков (asyncio.to_thread)
        self._sheets = {}                  # название листа -> лист
        self._rows = {}                    # название листа -> (когда прочитан, [Record])
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

    def _month_rows(self, title: str, fresh: bool) -> List[Record]:
        ttl = self.ROWS_TTL if title == month_title(datetime.now()) else self.OLD_ROWS_TTL
        with self._lock:
            cached = self._rows.get(title)
        if cached is not None and not fresh and time.monotonic() - cached[0] < ttl:
            return list(cached[1])
        ws = self._worksheet(title)
        if ws is None:
            return []      # листа нет: не запоминаем, чтобы заметить, когда его создадут вручную
        try:
            values = ws.get_all_values()
        except self._gspread.exceptions.APIError:
            self._forget(title)   # лист могли удалить или переименовать: в следующий раз ищем заново
            raise
        rows = [p for p in (parse_registry_row(r) for r in values) if p]
        with self._lock:
            self._rows[title] = (time.monotonic(), rows)
        return list(rows)

    def rows_between(self, start: datetime, end: datetime, fresh: bool = False) -> List[Record]:
        """Траты за период. fresh=True перечитывает таблицу (для сверки), иначе может взять из памяти."""
        found = []
        for month in _month_titles(start, end):
            found.extend(p for p in self._month_rows(month_title(month), fresh) if start <= p.date <= end)
        return found

    def save(self, date: datetime, row: List):
        """Дописывает строку. Возвращает (лист, номер строки) для отмены записи."""
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
        number = self._verify(ws, title, row, resp)
        parsed = parse_registry_row(row)
        if parsed:
            with self._lock:
                cached = self._rows.get(title)
                if cached is not None:
                    cached[1].append(parsed)
        return (title, number)

    def delete(self, ref, row: List) -> bool:
        """Удаляет записанную строку, если на её месте всё ещё она (таблицу могли сдвинуть)."""
        title, number = ref
        if not number:
            return False
        ws = self._worksheet(title)
        if ws is None:
            return False
        got = ws.get(f"A{number}:C{number}")
        if not got or not _same_row(got[0], row):
            return False
        ws.delete_rows(number)
        parsed = parse_registry_row(row)
        with self._lock:
            cached = self._rows.get(title)
            if cached is not None and parsed in cached[1]:
                cached[1].remove(parsed)
        return True

    def _verify(self, ws, title: str, row: List, resp) -> Optional[int]:
        """Перечитывает только что записанную строку: «Записано» только если она реально есть в таблице.

        Возвращает номер строки (None, если Google его не сообщил)."""
        try:
            rng = (resp or {}).get("updates", {}).get("updatedRange", "")
        except AttributeError:
            rng = ""
        log.info("Запись в лист «%s», диапазон %s", title, rng or "неизвестен")
        if not rng:
            return None  # Google не сообщил диапазон: проверить нечем, не считаем это ошибкой
        cells = rng.split("!")[-1]
        m = re.match(r"[A-Z]+(\d+)", cells)
        number = int(m.group(1)) if m else None
        try:
            got = ws.get(cells)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось перечитать строку %s: %s", rng, e)
            return number
        if not got or not got[0] or len(got[0]) < 3 or str(got[0][2]).strip() != str(row[2]).strip():
            raise StorageError(
                f"строка не появилась в листе «{title}» (диапазон {rng}). Проверьте таблицу"
            )
        return number


_storage: Optional[object] = None


def get_storage():
    global _storage
    if _storage is None:
        if config.STORAGE == "sheets":
            _storage = SheetsStorage(config.GOOGLE_CREDENTIALS_FILE, config.SHEET_NAME)
        else:
            _storage = CsvStorage(config.CSV_FILE)
    return _storage


# ---------------------------------------------------------------- резервный файл

_fallback_lock = threading.Lock()


def save_fallback(date: datetime, row: List) -> None:
    """Резервная запись, если таблица недоступна."""
    with _fallback_lock:
        CsvStorage(config.FALLBACK_FILE).save(date, row)


def flush_fallback(storage) -> Tuple[int, int]:
    """Переносит строки из unsaved_expenses.csv в таблицу.

    Возвращает (сколько перенесено, сколько осталось). Строки, которые уже есть в таблице
    (например, их перенесли вручную), второй раз не пишутся. При первой же ошибке
    останавливается: оставшиеся строки ждут следующей попытки.
    """
    path = Path(config.FALLBACK_FILE)
    if not _fallback_lock.acquire(blocking=False):
        return 0, 0                       # уже переносим в другом потоке
    try:
        if not path.exists():
            return 0, 0
        fallback = CsvStorage(path)
        rows = fallback._read()
        header, body = (rows[:1], rows[1:]) if rows and rows[0] == HEADER else ([HEADER], rows)
        moved, left = 0, []
        for i, raw in enumerate(body):
            date = parse_date_cell(raw[0]) if raw else None
            try:
                amount = parse_amount_cell(raw[1])
            except (ValueError, IndexError):
                amount = None
            if date is None or amount is None:
                left.append(raw)          # непонятная строка: оставляем человеку
                continue
            row = [raw[0], amount] + raw[2:]
            try:
                exists = any(
                    abs(r.rub - amount * 1000) <= 1 and r.merchant.strip() == str(raw[2]).strip()
                    for r in storage.rows_between(date - timedelta(minutes=1), date + timedelta(minutes=1),
                                                  fresh=(moved == 0))
                )
                if not exists:
                    storage.save(date, row)
                moved += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Перенос из %s остановлен: %s", path.name, e)
                left.extend(body[i:])
                break
        if moved:
            if left:
                fallback._rewrite(header + left)
            else:
                path.unlink()
            log.info("Из %s перенесено строк: %d, осталось: %d", path.name, moved, len(left))
        return moved, len(left)
    finally:
        _fallback_lock.release()
