"""Чтение чека (скриншота, PDF или текста СМС) и банковских выписок через Claude.

Claude отвечает в JSON по заданной схеме (structured outputs), поэтому ответ не нужно
выискивать в тексте регулярными выражениями. Если модель схему не поддерживает,
тот же запрос уходит без неё, а JSON достаётся из текста ответа.
"""
import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from anthropic import AsyncAnthropic, BadRequestError

import config

log = logging.getLogger(__name__)

_client: Optional[AsyncAnthropic] = None

MAX_IMAGE_BYTES = 4_500_000    # у Claude предел 5 МБ на картинку; больше — пережимаем
MAX_IMAGE_SIDE = 2000          # при пережатии: длинная сторона в пикселях (текст чека остаётся читаемым)
MAX_PDF_BYTES = 20_000_000     # больше Telegram всё равно не отдаёт боту
AMOUNT_DIGITS = 5              # сумма хранится в тысячах рублей: 5 знаков после запятой = копейки

# ---------------------------------------------------------------- запросы к Claude

# Какие варианты запроса модель принимает. Узнаём при первой ошибке и запоминаем,
# чтобы не отправлять заведомо отклоняемый запрос на каждом чеке.
_fast_ok = True        # «быстрый режим»: без обдумывания, минимальное усилие
_schema_ok = True      # ответ строго по JSON-схеме


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, timeout=60.0)
    return _client


def _extra_body(schema: Optional[dict], fast: bool) -> dict:
    """Дополнительные параметры запроса. Передаются через extra_body, чтобы работать
    с любой версией библиотеки anthropic, даже если она ещё не знает этих полей."""
    body: dict = {}
    output_config: dict = {}
    if fast:
        body["thinking"] = {"type": "disabled"}
        output_config["effort"] = "low"
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if output_config:
        body["output_config"] = output_config
    return body


async def _send(kwargs: dict, stream: bool):
    client = _get_client()
    if stream:
        # Длинные ответы (выписки) получаем потоком: так запрос не упирается в таймаут
        async with client.messages.stream(**kwargs) as s:
            return await s.get_final_message()
    return await client.messages.create(**kwargs)


async def _create(content, schema: dict, max_tokens: int = 1000,
                  timeout: Optional[float] = None, stream: bool = False):
    """Запрос к Claude с откатом: если модель не приняла быстрый режим или схему,
    повторяем без них и запоминаем это до перезапуска бота."""
    global _fast_ok, _schema_ok
    while True:
        fast = getattr(config, "FAST_MODE", False) and _fast_ok
        kwargs = dict(
            model=config.MODEL, max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
            extra_body=_extra_body(schema if _schema_ok else None, fast),
        )
        if timeout:
            kwargs["timeout"] = timeout
        try:
            return await _send(kwargs, stream)
        except BadRequestError as e:
            # 400 бывает и из-за самого файла (битая картинка, слишком длинный PDF):
            # тогда режимы не отключаем, а показываем ошибку
            text = str(e).lower()
            if fast and ("thinking" in text or "effort" in text):
                _fast_ok = False
                log.warning("Модель не приняла быстрый режим (%s). Дальше работаю без него", e)
            elif _schema_ok and any(w in text for w in ("output_config", "format", "schema")):
                _schema_ok = False
                log.warning("Модель не приняла JSON-схему ответа (%s). Дальше беру JSON из текста", e)
            else:
                raise


def answer_text(response) -> str:
    return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")


def extract_json(text: str) -> dict:
    """JSON-объект из ответа модели (на случай, если вокруг него всё же оказался текст)."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("в ответе Claude нет JSON") from None
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("ответ Claude не JSON-объект")
    return data


# ---------------------------------------------------------------- разбор значений

def to_float(raw) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    for ch in (" ", "\xa0", " ", " "):
        s = s.replace(ch, "")
    s = re.sub(r"[^\d.,\-]", "", s)
    return float(s.replace(",", "."))


def parse_date(s) -> Optional[datetime]:
    s = str(s or "").strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def rub_to_thousands(rub: float) -> float:
    return round(rub / 1000.0, AMOUNT_DIGITS)


# ---------------------------------------------------------------- картинки

def media_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return None     # HEIC, BMP, TIFF и прочее: Claude их не принимает, нужно перекодировать


def prepare_image(data: bytes) -> Tuple[bytes, str]:
    """Готовит картинку для Claude: формат, который он понимает, и размер до 5 МБ.

    Скриншоты из Telegram обычно подходят как есть. Пережимаем только слишком большие
    файлы и непонятные форматы (например, HEIC с iPhone). Нужна библиотека Pillow
    (и pillow-heif для HEIC); без неё такие файлы получают понятную ошибку.
    """
    kind = media_type(data)
    if kind is not None and len(data) <= MAX_IMAGE_BYTES:
        return data, kind
    try:
        from PIL import Image, ImageOps
    except ImportError:
        if kind is None:
            raise ValueError("формат картинки не поддерживается (пришлите PNG или JPEG)") from None
        raise ValueError("файл больше 5 МБ") from None
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        raise ValueError("не удалось открыть картинку (пришлите PNG или JPEG)") from None
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    if img.mode != "RGB":
        img = img.convert("RGB")
    for quality in (90, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
    log.info("Картинка пережата: %d -> %d байт, %dx%d", len(data), buf.tell(), *img.size)
    return buf.getvalue(), "image/jpeg"


# ---------------------------------------------------------------- один чек

PROMPT = (
    "Сегодня {today}. Это скриншот или текст банковской операции (покупка, перевод, платёж).\n"
    "Заполни поля:\n"
    "amount_rub — сумма операции в рублях числом (копейки дробной частью, например 2898.5); "
    "если сумму найти не удалось, 0\n"
    "merchant — магазин или получатель\n"
    "bank — название банка, если видно (Альфа-Банк, Т-Банк, Банк Санкт-Петербург, Точка и т.д.), иначе пустая строка\n"
    "date — дата и время операции в формате ДД.ММ.ГГГГ ЧЧ:ММ; если написано «сегодня» или «вчера», "
    "пересчитай по сегодняшней дате; если времени нет, только ДД.ММ.ГГГГ; если даты нет, пустая строка\n"
    "operations — сколько всего операций в документе (для одного чека 1)\n"
    "Если операций несколько, описывай самую верхнюю."
)

PDF_EXTRA = "\nЭто PDF-документ (чек, справка об операции или выписка)."

EXPENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "amount_rub": {"type": "number"},
        "merchant": {"type": "string"},
        "bank": {"type": "string"},
        "date": {"type": "string"},
        "operations": {"type": "integer"},
    },
    "required": ["amount_rub", "merchant", "bank", "date", "operations"],
    "additionalProperties": False,
}

JSON_HINT = "\nОтветь только JSON-объектом с этими полями, без пояснений."


@dataclass
class ParsedExpense:
    amount: float = 0.0          # сумма в ТЫСЯЧАХ рублей (так ведётся таблица)
    merchant: str = "Банк / СМС"
    bank: str = ""
    date: Optional[datetime] = None
    error: str = ""              # непустое, если не удалось обратиться к Claude
    ops: int = 1                 # сколько операций нашлось в документе (для PDF-выписок)


def parse_expense_json(data: dict) -> ParsedExpense:
    result = ParsedExpense()
    try:
        rub = to_float(data.get("amount_rub", 0) or 0)
        if rub > 0:
            result.amount = rub_to_thousands(rub)
    except (TypeError, ValueError):
        pass
    merchant = str(data.get("merchant") or "").strip()
    if merchant:
        result.merchant = merchant[:200]
    result.bank = str(data.get("bank") or "").strip()[:60]
    result.date = parse_date(data.get("date"))
    try:
        result.ops = max(1, int(data.get("operations") or 1))
    except (TypeError, ValueError):
        pass
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
    if isinstance(e, ValueError):
        return str(e)
    return f"{type(e).__name__}: {str(e)[:120]}"


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
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode("ascii")}},
                {"type": "text", "text": prompt + PDF_EXTRA + JSON_HINT},
            ]
        elif image_bytes is not None:
            data, kind = prepare_image(image_bytes)
            content = [
                {"type": "image", "source": {
                    "type": "base64", "media_type": kind,
                    "data": base64.standard_b64encode(data).decode("ascii")}},
                {"type": "text", "text": prompt + JSON_HINT},
            ]
        else:
            content = [{"type": "text", "text": prompt + JSON_HINT + "\n\nТекст операции:\n" + (text or "")}]
        response = await _create(content, EXPENSE_SCHEMA)
        answer = answer_text(response)
        log.info("Ответ Claude: %s", answer.replace("\n", " "))
        return parse_expense_json(extract_json(answer))
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка обращения к Claude: %s", e)
        return ParsedExpense(error=friendly_error(e))


# ---------------------------------------------------------------- выписки

STATEMENT_PROMPT = (
    "Сегодня {today}. Это банковская выписка за период. Выпиши ВСЕ расходные операции: покупки, "
    "списания, платежи, переводы другим людям и организациям. Не включай поступления, пополнения, "
    "возвраты, проценты и кешбэк.\n"
    "Поля:\n"
    "bank — название банка (Альфа-Банк, Т-Банк, Банк Санкт-Петербург, Точка и т.д.), если видно, иначе пустая строка\n"
    "operations — список операций, у каждой:\n"
    "  date — ДД.ММ.ГГГГ ЧЧ:ММ (если времени в выписке нет, только ДД.ММ.ГГГГ)\n"
    "  amount_rub — сумма положительным числом в рублях, копейки дробной частью\n"
    "  merchant — получатель или описание операции"
)

STATEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "bank": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "amount_rub": {"type": "number"},
                    "merchant": {"type": "string"},
                },
                "required": ["date", "amount_rub", "merchant"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["bank", "operations"],
    "additionalProperties": False,
}

MAX_STATEMENT_TEXT = 200_000   # символов; больше не отправляем, чтобы не упереться в лимит модели
STATEMENT_MAX_TOKENS = 64_000  # потоковый запрос: длинная выписка помещается целиком


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


def parse_statement_json(data: dict) -> StatementResult:
    result = StatementResult(ops=[], bank=str(data.get("bank") or "").strip()[:60])
    for raw in data.get("operations") or []:
        if not isinstance(raw, dict):
            continue
        date = parse_date(raw.get("date"))
        try:
            rub = abs(to_float(raw.get("amount_rub")))
        except (TypeError, ValueError):
            continue
        if date is None or rub <= 0:
            continue
        merchant = str(raw.get("merchant") or "").strip()[:120]
        result.ops.append(StatementOp(date=date, rub=round(rub, 2), merchant=merchant, bank=result.bank))
    return result


async def read_statement(
    *, pdf_bytes: Optional[bytes] = None, text: Optional[str] = None, today: Optional[datetime] = None
) -> StatementResult:
    """Читает все расходные операции из выписки (PDF или текст таблицы)."""
    today = today or datetime.now()
    prompt = STATEMENT_PROMPT.format(today=today.strftime("%d.%m.%Y")) + JSON_HINT
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
        response = await _create(content, STATEMENT_SCHEMA, max_tokens=STATEMENT_MAX_TOKENS,
                                 timeout=600.0, stream=True)
        answer = answer_text(response)
        truncated = getattr(response, "stop_reason", "") == "max_tokens"
        log.info("Выписка: ответ Claude %d симв.%s", len(answer), " (обрезан)" if truncated else "")
        if truncated:
            return StatementResult(ops=[], truncated=True,
                                   error="выписка слишком длинная, пришли её по частям (по неделям)")
        result = parse_statement_json(extract_json(answer))
        return result
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка чтения выписки: %s", e)
        return StatementResult(ops=[], error=friendly_error(e))
