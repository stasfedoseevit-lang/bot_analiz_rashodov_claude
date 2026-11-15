"""Чтение чека (скриншота или текста СМС) через Claude."""
import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from anthropic import AsyncAnthropic, BadRequestError

import config

log = logging.getLogger(__name__)

_client: Optional[AsyncAnthropic] = None

MAX_IMAGE_BYTES = 5_000_000
MAX_PDF_BYTES = 20_000_000   # больше Telegram всё равно не отдаёт боту

PDF_EXTRA = (
    "\nЭто PDF-документ (чек, справка об операции или выписка). Добавь пятую строку:\n"
    "OPS: сколько всего операций в документе (целое число; для одного чека или справки 1)\n"
    "Если операций несколько, в первых четырёх строках опиши самую верхнюю."
)

PROMPT = (
    "Сегодня {today}. Это скриншот или текст банковской операции (покупка, перевод, платёж).\n"
    "Ответь строго в четыре строки:\n"
    "SUM: сумма операции в рублях, только число без пробелов и знака валюты, "
    "дробная часть через точку (например 2898.50); если сумму найти не удалось, напиши 0\n"
    "MERCHANT: магазин или получатель\n"
    "BANK: название банка, если видно (Альфа-Банк, Т-Банк, Банк Санкт-Петербург, Точка и т.д.), иначе оставь пусто\n"
    "DATE: дата и время операции в формате ДД.ММ.ГГГГ ЧЧ:ММ; если на скриншоте написано "
    "«сегодня» или «вчера», пересчитай по сегодняшней дате; если времени нет, укажи только дату; "
    "если даты нет совсем, оставь пусто\n"
    "Если операций несколько, бери самую верхнюю. Больше ничего не пиши."
)


@dataclass
class ParsedExpense:
    amount: float = 0.0          # сумма в ТЫСЯЧАХ рублей (так ведётся таблица)
    merchant: str = "Банк / СМС"
    bank: str = ""
    date: Optional[datetime] = None
    error: str = ""              # непустое, если не удалось обратиться к Claude
    ops: int = 1                 # сколько операций нашлось в документе (для PDF-выписок)


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, timeout=60.0)
    return _client


def media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"


def _to_float(raw: str) -> float:
    s = raw.strip()
    for ch in (" ", "\xa0", " "):
        s = s.replace(ch, "")
    return float(s.replace(",", "."))


def _parse_date(s: str) -> Optional[datetime]:
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_answer(text: str) -> ParsedExpense:
    """Разбирает ответ модели в четыре строки."""
    result = ParsedExpense()
    for line in text.split("\n"):
        clean = line.strip(" *_`")
        up = clean.upper()
        value = clean.split(":", 1)[1].strip(" *_`") if ":" in clean else ""
        if up.startswith("SUM"):
            m = re.search(r"\d[\d\s \xa0.,]*", value)
            if m:
                try:
                    result.amount = round(_to_float(m.group(0)) / 1000.0, 3)
                except ValueError:
                    pass
        elif up.startswith("MERCHANT"):
            if value:
                result.merchant = value
        elif up.startswith("BANK"):
            result.bank = value
        elif up.startswith("DATE"):
            result.date = _parse_date(value)
        elif up.startswith("OPS"):
            m = re.search(r"\d+", value)
            if m:
                result.ops = max(1, int(m.group(0)))
    return result


def friendly_error(e: Exception) -> str:
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    if "credit balance" in text:
        return "на балансе API закончились средства"
    if status == 401:
        return "неверный ключ API"
    if status == 403:
        return "доступ запрещён (проверьте регион и права ключа)"
    if status == 404:
        return "модель не найдена, проверьте CLAUDE_MODEL в settings.txt"
    if status == 429:
        return "превышен лимит запросов, подождите минуту"
    return f"{type(e).__name__}: {str(e)[:120]}"


async def _create(messages, max_tokens: int = 1000, timeout: Optional[float] = None):
    """Запрос к Claude.

    В быстром режиме отключаем «обдумывание» (у Haiku 5.5 оно включено по умолчанию)
    и ставим минимальное усилие: для чтения суммы с чека этого достаточно,
    а ответ приходит заметно быстрее. Если модель параметры не приняла, повторяем обычным запросом.
    """
    client = _get_client()
    extra = {"timeout": timeout} if timeout else {}
    if getattr(config, "FAST_MODE", False):
        try:
            return await client.messages.create(
                model=config.MODEL,
                max_tokens=max_tokens,
                messages=messages,
                extra_body={"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}},
                **extra,
            )
        except BadRequestError as e:
            log.warning("Модель не приняла быстрый режим (%s), повторяю обычным запросом", e)
    return await client.messages.create(model=config.MODEL, max_tokens=max_tokens, messages=messages, **extra)


# ---------------------------------------------------------------- выписки

STATEMENT_PROMPT = (
    "Сегодня {today}. Это банковская выписка за период. Выпиши ВСЕ расходные операции: покупки, "
    "списания, платежи, переводы другим людям и организациям. Не включай поступления, пополнения, "
    "возвраты, проценты и кешбэк.\n"
    "Ответ строго в таком виде:\n"
    "первая строка: BANK: название банка (Альфа-Банк, Т-Банк, Банк Санкт-Петербург, Точка и т.д.), если видно, иначе пусто\n"
    "далее по одной операции на строку:\n"
    "ДД.ММ.ГГГГ ЧЧ:ММ | сумма числом без пробелов, дробная часть через точку | получатель или описание\n"
    "Если времени в выписке нет, пиши только дату ДД.ММ.ГГГГ. Сумму указывай положительным числом в рублях. "
    "Ничего, кроме этих строк, не пиши."
)

MAX_STATEMENT_TEXT = 200_000   # символов; больше не отправляем, чтобы не упереться в лимит модели


@dataclass
class StatementOp:
    date: datetime
    rub: float          # сумма в рублях
    merchant: str
    bank: str = ""


@dataclass
class StatementResult:
    ops: list
    bank: str = ""
    error: str = ""
    truncated: bool = False


def parse_statement(text: str) -> StatementResult:
    result = StatementResult(ops=[])
    for line in text.split("\n"):
        clean = line.strip(" *`")
        if clean.upper().startswith("BANK"):
            result.bank = clean.split(":", 1)[1].strip(" *`") if ":" in clean else ""
            continue
        parts = [p.strip() for p in clean.split("|")]
        if len(parts) < 3:
            continue
        date = _parse_date(parts[0])
        try:
            rub = _to_float(re.sub(r"[^\d.,\s\xa0]", "", parts[1]) or "x")
        except ValueError:
            continue
        if date is None or rub <= 0:
            continue
        result.ops.append(StatementOp(date=date, rub=round(rub, 2), merchant=" | ".join(parts[2:])[:120]))
    return result


async def read_statement(
    *, pdf_bytes: Optional[bytes] = None, text: Optional[str] = None, today: Optional[datetime] = None
) -> StatementResult:
    """Читает все расходные операции из выписки (PDF или текст таблицы)."""
    today = today or datetime.now()
    prompt = STATEMENT_PROMPT.format(today=today.strftime("%d.%m.%Y"))
    try:
        if pdf_bytes is not None:
            if len(pdf_bytes) > MAX_PDF_BYTES:
                return StatementResult(ops=[], error="PDF больше 20 МБ")
            content = [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf_bytes).decode("ascii")}},
                {"type": "text", "text": prompt},
            ]
        else:
            if len(text or "") > MAX_STATEMENT_TEXT:
                return StatementResult(ops=[], error="файл слишком большой, пришли выписку за меньший период")
            content = [{"type": "text", "text": prompt + "\n\nВыписка:\n" + (text or "")}]
        response = await _create([{"role": "user", "content": content}], max_tokens=16000, timeout=240.0)
        answer = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        log.info("Выписка: ответ Claude %d симв.", len(answer))
        result = parse_statement(answer)
        for op in result.ops:
            op.bank = result.bank
        result.truncated = getattr(response, "stop_reason", "") == "max_tokens"
        return result
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка чтения выписки: %s", e)
        return StatementResult(ops=[], error=friendly_error(e))


async def read_expense(
    *, image_bytes: Optional[bytes] = None, text: Optional[str] = None,
    pdf_bytes: Optional[bytes] = None, today: Optional[datetime] = None
) -> ParsedExpense:
    today = today or datetime.now()
    prompt = PROMPT.format(today=today.strftime("%d.%m.%Y"))
    try:
        if pdf_bytes is not None:
            if len(pdf_bytes) > MAX_PDF_BYTES:
                return ParsedExpense(error="PDF больше 20 МБ")
            content = [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": prompt + PDF_EXTRA},
            ]
        elif image_bytes is not None:
            if len(image_bytes) > MAX_IMAGE_BYTES:
                return ParsedExpense(error="файл больше 5 МБ")
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type(image_bytes),
                        "data": base64.standard_b64encode(image_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": prompt},
            ]
        else:
            content = [{"type": "text", "text": prompt + "\n\nТекст операции:\n" + (text or "")}]
        response = await _create([{"role": "user", "content": content}])
        answer = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        log.info("Ответ Claude: %s", answer.replace("\n", " | "))
        return parse_answer(answer)
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка обращения к Claude: %s", e)
        return ParsedExpense(error=friendly_error(e))
