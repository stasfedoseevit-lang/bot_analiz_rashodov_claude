"""Чтение чека (скриншота, PDF или текста СМС) и банковских выписок через DeepSeek.

DeepSeek работает через OpenAI-совместимый API (библиотека openai, свой base_url).
Особенности по сравнению с Claude:
- картинки понимает только модель deepseek-flash (JPEG, PNG, GIF, WebP);
- PDF напрямую не принимает: из PDF достаём текст сами (pypdfium2), а если это скан
  без текста, отправляем страницы картинками;
- строгой JSON-схемы нет, есть «JSON-режим» (response_format=json_object): модель
  гарантированно отвечает JSON-объектом, а поля мы описываем в запросе и проверяем сами;
- длинные выписки режем на части, чтобы ответ не упирался в предел длины.
"""
import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from openai import AsyncOpenAI, BadRequestError

import config

log = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None

MAX_IMAGE_BYTES = 4_500_000    # больше — пережимаем (меньше трафика, быстрее ответ)
MAX_IMAGE_SIDE = 2000          # при пережатии: длинная сторона в пикселях (текст чека остаётся читаемым)
MAX_PDF_BYTES = 20_000_000     # больше Telegram всё равно не отдаёт боту
AMOUNT_DIGITS = 5              # сумма хранится в тысячах рублей: 5 знаков после запятой = копейки
PDF_RENDER_SCALE = 2.0         # скан PDF -> картинка: масштаб (2 = ~144 dpi, текст читается)
PDF_MIN_TEXT = 40              # если текста в PDF меньше, считаем его сканом
RECEIPT_PDF_PAGES = 2          # для чека смотрим первые страницы
STATEMENT_SCAN_PAGES = 15      # для скана выписки: не больше стольких страниц
STATEMENT_CHUNK_CHARS = 12_000  # текст выписки отправляем частями такого размера

# ---------------------------------------------------------------- запросы к DeepSeek

# Какие варианты запроса модель принимает. Узнаём при первой ошибке и запоминаем,
# чтобы не отправлять заведомо отклоняемый запрос на каждом чеке.
_fast_ok = True        # «быстрый режим»: без обдумывания
_json_ok = True        # JSON-режим (response_format=json_object)


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL, timeout=60.0)
    return _client


async def _create(content, max_tokens: int = 1000, timeout: Optional[float] = None):
    """Запрос к DeepSeek с откатом: если модель не приняла быстрый режим или JSON-режим,
    повторяем без них и запоминаем это до перезапуска бота."""
    global _fast_ok, _json_ok
    while True:
        fast = getattr(config, "FAST_MODE", False) and _fast_ok
        kwargs = dict(model=config.MODEL, max_tokens=max_tokens,
                      messages=[{"role": "user", "content": content}])
        if _json_ok:
            kwargs["response_format"] = {"type": "json_object"}
        if fast:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        if timeout:
            kwargs["timeout"] = timeout
        try:
            return await _get_client().chat.completions.create(**kwargs)
        except BadRequestError as e:
            # 400 бывает и из-за самого файла (битая картинка): тогда режимы не отключаем
            text = str(e).lower()
            if fast and "thinking" in text:
                _fast_ok = False
                log.warning("Модель не приняла быстрый режим (%s). Дальше работаю без него", e)
            elif _json_ok and ("response_format" in text or "json" in text):
                _json_ok = False
                log.warning("Модель не приняла JSON-режим (%s). Дальше беру JSON из текста", e)
            else:
                raise


def answer_text(response) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def finish_reason(response) -> str:
    try:
        return response.choices[0].finish_reason or ""
    except (AttributeError, IndexError):
        return ""


def extract_json(text: str) -> dict:
    """JSON-объект из ответа модели (на случай, если вокруг него всё же оказался текст)."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("в ответе DeepSeek нет JSON") from None
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("ответ DeepSeek не JSON-объект")
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
    return None     # HEIC, BMP, TIFF и прочее: DeepSeek их не принимает, нужно перекодировать


def prepare_image(data: bytes) -> Tuple[bytes, str]:
    """Готовит картинку: формат, который понимает модель, и разумный размер.

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
    return _encode_jpeg(img), "image/jpeg"


def _encode_jpeg(img) -> bytes:
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    if img.mode != "RGB":
        img = img.convert("RGB")
    for quality in (90, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
    return buf.getvalue()


def image_part(data: bytes, kind: str) -> dict:
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{kind};base64,{b64}"}}


# ---------------------------------------------------------------- PDF

def pdf_pages_text(data: bytes) -> List[str]:
    """Текст каждой страницы PDF (пустая строка для страниц-сканов)."""
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(data)
    except Exception:  # noqa: BLE001
        raise ValueError("не удалось открыть PDF (файл повреждён или защищён паролем)") from None
    try:
        pages = []
        for i in range(len(doc)):
            page = doc[i]
            textpage = page.get_textpage()
            pages.append(textpage.get_text_range().replace("\r\n", "\n").strip())
        return pages
    finally:
        doc.close()


def pdf_page_images(data: bytes, limit: int) -> List[bytes]:
    """Первые limit страниц PDF картинками JPEG (для сканов без текста)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(data)
    try:
        images = []
        for i in range(min(limit, len(doc))):
            images.append(_encode_jpeg(doc[i].render(scale=PDF_RENDER_SCALE).to_pil()))
        return images
    finally:
        doc.close()


def pdf_is_scan(pages: List[str]) -> bool:
    return sum(len(p) for p in pages) < PDF_MIN_TEXT


# ---------------------------------------------------------------- один чек

PROMPT = (
    "Сегодня {today}. Это скриншот, PDF или текст банковской операции (покупка, перевод, платёж).\n"
    "Ответь JSON-объектом с полями:\n"
    "amount_rub — сумма операции в рублях числом (копейки дробной частью, например 2898.5); "
    "если сумму найти не удалось, 0\n"
    "merchant — магазин или получатель (строка)\n"
    "bank — название банка, если видно (Альфа-Банк, Т-Банк, Банк Санкт-Петербург, Точка и т.д.), иначе пустая строка\n"
    "date — дата и время операции строкой в формате ДД.ММ.ГГГГ ЧЧ:ММ; если написано «сегодня» или «вчера», "
    "пересчитай по сегодняшней дате; если времени нет, только ДД.ММ.ГГГГ; если даты нет, пустая строка\n"
    "operations — сколько всего операций в документе (целое число, для одного чека 1)\n"
    "Если операций несколько, описывай самую верхнюю.\n"
    'Пример ответа: {{"amount_rub": 2898.5, "merchant": "Пятёрочка", "bank": "Т-Банк", '
    '"date": "15.11.2026 12:40", "operations": 1}}\n'
    "Ответь только JSON, без пояснений."
)


@dataclass
class ParsedExpense:
    amount: float = 0.0          # сумма в ТЫСЯЧАХ рублей (так ведётся таблица)
    merchant: str = "Банк / СМС"
    bank: str = ""
    date: Optional[datetime] = None
    error: str = ""              # непустое, если не удалось обратиться к модели
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
    if status == 402 or "insufficient balance" in text:
        return "на балансе DeepSeek закончились средства"
    if status == 401:
        return "неверный ключ API DeepSeek"
    if status == 403:
        return "доступ запрещён (проверьте ключ и права)"
    if status == 404 or "model not exist" in text:
        return "модель не найдена, проверьте DEEPSEEK_MODEL в settings.txt"
    if status == 429:
        return "превышен лимит запросов, подождите минуту"
    if status == 503:
        return "DeepSeek перегружен, попробуйте чуть позже"
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
            pages = pdf_pages_text(pdf_bytes)
            if pdf_is_scan(pages):
                images = pdf_page_images(pdf_bytes, RECEIPT_PDF_PAGES)
                content = [image_part(img, "image/jpeg") for img in images] + [{"type": "text", "text": prompt}]
            else:
                body = "\n\n".join(pages[:RECEIPT_PDF_PAGES])
                content = prompt + f"\n\nТекст PDF (страниц в документе: {len(pages)}):\n" + body
        elif image_bytes is not None:
            data, kind = prepare_image(image_bytes)
            content = [image_part(data, kind), {"type": "text", "text": prompt}]
        else:
            content = prompt + "\n\nТекст операции:\n" + (text or "")
        response = await _create(content)
        answer = answer_text(response)
        log.info("Ответ DeepSeek: %s", answer.replace("\n", " "))
        return parse_expense_json(extract_json(answer))
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка обращения к DeepSeek: %s", e)
        return ParsedExpense(error=friendly_error(e))


# ---------------------------------------------------------------- выписки

STATEMENT_PROMPT = (
    "Сегодня {today}. Это банковская выписка (или её часть). Выпиши ВСЕ расходные операции: покупки, "
    "списания, платежи, переводы другим людям и организациям. Не включай поступления, пополнения, "
    "возвраты, проценты и кешбэк.\n"
    "Ответь JSON-объектом с полями:\n"
    "bank — название банка (Альфа-Банк, Т-Банк, Банк Санкт-Петербург, Точка и т.д.), если видно, иначе пустая строка\n"
    "operations — список операций, у каждой:\n"
    "  date — строка ДД.ММ.ГГГГ ЧЧ:ММ (если времени в выписке нет, только ДД.ММ.ГГГГ)\n"
    "  amount_rub — сумма положительным числом в рублях, копейки дробной частью\n"
    "  merchant — получатель или описание операции\n"
    "Если расходных операций нет, operations — пустой список.\n"
    'Пример ответа: {{"bank": "Альфа-Банк", "operations": [{{"date": "01.11.2026 10:00", '
    '"amount_rub": 150.0, "merchant": "Кофейня"}}]}}\n'
    "Ответь только JSON, без пояснений."
)

MAX_STATEMENT_TEXT = 200_000   # символов; больше не отправляем, чтобы не платить за огромные файлы
STATEMENT_MAX_TOKENS = 8_000   # на одну часть выписки


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


def split_text(text: str, limit: Optional[int] = None) -> List[str]:
    """Режет текст выписки на части по границам строк (операция не разрывается пополам).

    Первые строки (шапка таблицы, название банка) добавляются к каждой части, чтобы модель
    понимала, где какие колонки."""
    limit = limit or STATEMENT_CHUNK_CHARS
    lines = text.split("\n")
    head = "\n".join(lines[:5])[:1500]
    chunks, buf, size = [], [], 0
    for line in lines:
        if buf and size + len(line) + 1 > limit:
            chunks.append("\n".join(buf))
            buf, size = [head, "..."], len(head) + 4
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


async def _read_part(content, label: str) -> StatementResult:
    response = await _create(content, max_tokens=STATEMENT_MAX_TOKENS, timeout=300.0)
    answer = answer_text(response)
    if finish_reason(response) == "length":
        log.info("Выписка (%s): ответ обрезан", label)
        return StatementResult(ops=[], truncated=True,
                               error="выписка слишком длинная, пришли её по частям (по неделям)")
    log.info("Выписка (%s): ответ DeepSeek %d симв.", label, len(answer))
    return parse_statement_json(extract_json(answer))


async def read_statement(
    *, pdf_bytes: Optional[bytes] = None, text: Optional[str] = None, today: Optional[datetime] = None
) -> StatementResult:
    """Читает все расходные операции из выписки (PDF или текст таблицы).

    Длинная выписка отправляется частями, результаты складываются."""
    today = today or datetime.now()
    prompt = STATEMENT_PROMPT.format(today=today.strftime("%d.%m.%Y"))
    try:
        parts = []    # содержимое запросов
        if pdf_bytes is not None:
            if len(pdf_bytes) > MAX_PDF_BYTES:
                return StatementResult(ops=[], error="PDF больше 20 МБ")
            pages = pdf_pages_text(pdf_bytes)
            if pdf_is_scan(pages):
                if len(pages) > STATEMENT_SCAN_PAGES:
                    return StatementResult(ops=[], error=f"скан больше {STATEMENT_SCAN_PAGES} страниц, "
                                                         "пришли выписку за меньший период или в Excel")
                for img in pdf_page_images(pdf_bytes, STATEMENT_SCAN_PAGES):
                    parts.append([image_part(img, "image/jpeg"), {"type": "text", "text": prompt}])
            else:
                text = "\n".join(pages)
        if not parts:
            if len(text or "") > MAX_STATEMENT_TEXT:
                return StatementResult(ops=[], error="файл слишком большой, пришли выписку за меньший период")
            parts = [prompt + "\n\nВыписка:\n" + chunk for chunk in split_text(text or "")]
        result = StatementResult(ops=[])
        for i, content in enumerate(parts, 1):
            part = await _read_part(content, f"часть {i} из {len(parts)}")
            if part.error:
                return part
            result.bank = result.bank or part.bank
            result.ops.extend(part.ops)
        for op in result.ops:
            op.bank = result.bank
        return result
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка чтения выписки: %s", e)
        return StatementResult(ops=[], error=friendly_error(e))
