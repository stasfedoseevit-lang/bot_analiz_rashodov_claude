"""Сверка выписок с реестром: какие операции из банка не внесены в таблицу."""
import io
import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import config
from claude_parse import StatementOp

log = logging.getLogger(__name__)

AMOUNT_TOLERANCE_RUB = 1.0     # допуск по сумме при сравнении с таблицей
DATE_TOLERANCE_DAYS = 3        # в скриншоте дата операции, в выписке бывает дата проведения
IGNORED_KEEP_DAYS = 183        # «не нужно» старше полугода забываем: такие выписки уже не сверяют


def op_key(op: StatementOp) -> str:
    """Ключ операции из выписки: дата, время и сумма.

    Название получателя в ключ не входит: Claude может записать его по-разному
    («OZON» / «Ozon.ru»), и тогда отклонённая операция всплывала бы снова.
    Дата, время и сумма переписываются из выписки цифрами и не меняются.
    """
    return f"{op.date:%d.%m.%Y %H:%M}|{op.rub:.2f}"


def _normalize_key(key: str) -> str:
    """Старые ключи были вида «дата|сумма|получатель»: отбрасываем получателя."""
    return "|".join(str(key).split("|")[:2])


def _key_date(key: str) -> Optional[datetime]:
    try:
        return datetime.strptime(key.split("|")[0], "%d.%m.%Y %H:%M")
    except ValueError:
        return None


# ---------------------------------------------------------------- «не нужно» (игнорируемые)

def load_ignored() -> set:
    try:
        with open(config.IGNORED_FILE, encoding="utf-8") as f:
            return {_normalize_key(k) for k in json.load(f)}
    except (OSError, ValueError, TypeError):
        return set()


def add_ignored(keys, now: Optional[datetime] = None) -> None:
    keys = {_normalize_key(k) for k in keys}
    if not keys:
        return
    now = now or datetime.now()
    oldest = now - timedelta(days=IGNORED_KEEP_DAYS)
    all_keys = {k for k in load_ignored() | keys if (_key_date(k) or now) >= oldest}
    tmp = config.IGNORED_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(all_keys), f, ensure_ascii=False, indent=0)
        os.replace(tmp, config.IGNORED_FILE)
    except OSError as e:
        log.warning("Не удалось сохранить список игнорируемых операций: %s", e)


# ---------------------------------------------------------------- файлы -> текст

def file_kind(name: str, mime: Optional[str]) -> str:
    """pdf | xlsx | csv | other"""
    low = (name or "").lower()
    if low.endswith(".pdf") or mime == "application/pdf":
        return "pdf"
    if low.endswith(".xlsx") or low.endswith(".xlsm"):
        return "xlsx"
    if low.endswith(".csv") or low.endswith(".txt") or mime in ("text/csv", "text/plain"):
        return "csv"
    return "other"


def csv_to_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def xlsx_to_text(data: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError("не установлена библиотека openpyxl (pip install openpyxl)") from None
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in row]
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines)


# ---------------------------------------------------------------- сравнение

def find_missing(ops: List[StatementOp], registry: List[tuple]) -> Tuple[List[StatementOp], int]:
    """Возвращает (операции, которых нет в реестре, сколько нашлось)."""
    free = list(registry)           # (дата, рубли, получатель); каждая строка реестра сопоставляется один раз
    missing, matched = [], 0
    for op in sorted(ops, key=lambda o: o.date):
        best, best_gap = None, None
        for row in free:
            gap = abs(row[0] - op.date)
            if abs(row[1] - op.rub) <= AMOUNT_TOLERANCE_RUB and gap <= timedelta(days=DATE_TOLERANCE_DAYS):
                if best is None or gap < best_gap:
                    best, best_gap = row, gap
        if best is not None:
            free.remove(best)
            matched += 1
        else:
            missing.append(op)
    return missing, matched


def parse_picks(text: str, total: int) -> Optional[List[int]]:
    """«1,3, 5-7», «все», «нет» -> список индексов (с 0); None, если не поняли."""
    t = text.strip().lower().replace(";", ",")
    if t in ("нет", "ничего", "не надо", "никакие", "0", "-"):
        return []
    if t in ("все", "всё", "все.", "всё."):
        return list(range(total))
    picks = set()
    for part in t.replace(" ", ",").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                picks.update(range(int(a), int(b) + 1))
            else:
                picks.add(int(part))
        except ValueError:
            return None
    if not picks or any(n < 1 or n > total for n in picks):
        return None
    return sorted(n - 1 for n in picks)
