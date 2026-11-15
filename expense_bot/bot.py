"""Telegram-бот учёта трат: скриншот или текст -> DeepSeek читает сумму -> вы выбираете категорию -> запись."""
import asyncio
import html
import json
import logging
import os
import socket
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import recon
from categories import CATEGORIES, OTHER, WORK
from ai_parse import (
    AMOUNT_DIGITS, ParsedExpense, StatementOp, friendly_error, read_expense, read_statement, rub_to_thousands,
)
from storage import flush_fallback, get_storage, normalize_merchant, save_fallback

log = logging.getLogger("expense_bot")

CATS = list(CATEGORIES.keys())
ANSWER_STAGES = {"amount", "object", "refine", "comment"}
SKIP_WORDS = {"-", "—", "–"}

router = Router()
router.message.filter(F.from_user.id.in_(config.ALLOWED_USER_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ALLOWED_USER_IDS))


# ---------------------------------------------------------------- данные

@dataclass
class Item:
    kind: str                      # "photo", "pdf", "text" или "op" (операция из выписки)
    file_id: Optional[str] = None
    text: Optional[str] = None
    name: str = ""                 # имя PDF-файла
    parsed: Optional[ParsedExpense] = None   # для "op": данные уже известны
    key: str = ""                  # для "op": ключ, по которому запоминаем «не нужно»
    force: bool = False            # не проверять на повтор (нажали «Всё равно внести»)
    raw: str = ""                  # для "op": что писать в колонку «Исходное»
    queued: bool = False           # ждал в очереди (тогда при показе повторяем фото чека)
    task: Optional["asyncio.Task"] = None   # фоновое распознавание (см. submit)


@dataclass
class Expense:
    eid: int
    date: datetime
    amount: float                  # в тысячах рублей
    merchant: str
    bank: str
    raw: str
    photo_id: Optional[str]
    category: str = ""
    sub: str = "—"
    ref: str = "—"
    comment: str = ""
    stage: str = "category"        # category | sub | amount | object | refine | comment
    skip_key: str = ""             # для трат из выписки
    hint: Optional[Tuple[str, str, str]] = None   # подсказка: категория, подкатегория, уточнение


@dataclass
class ReconFile:
    file_id: str
    name: str
    kind: str                      # pdf | xlsx | csv


@dataclass
class ReconState:
    files: List[ReconFile] = field(default_factory=list)
    stage: str = "collect"         # collect (ждём файлы) | working (читаю) | pick (жду номера)
    missing: list = field(default_factory=list)


@dataclass
class UserState:
    chat_id: int = 0
    current: Optional[Expense] = None
    queue: Deque[Item] = field(default_factory=deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    recon: Optional[ReconState] = None
    dups: Dict[int, "Expense"] = field(default_factory=dict)   # пропущенные повторы (для «Всё равно внести»)
    pump: Optional["asyncio.Task"] = None   # обработчик очереди (см. pump_queue)
    saved: Dict[int, tuple] = field(default_factory=dict)      # последние записи (для «Отменить запись»)


STATES: Dict[int, UserState] = {}
_last_eid = 0      # номер последней траты (в кнопках); сохраняется между перезапусками


def new_eid() -> int:
    global _last_eid
    _last_eid += 1
    return _last_eid


def get_state(user_id: int, chat_id: int) -> UserState:
    st = STATES.setdefault(user_id, UserState())
    st.chat_id = chat_id
    return st


# ---------------------------------------------------------------- вспомогательное

def esc(value) -> str:
    return html.escape(str(value))


def now_local() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None)


def fmt_rub(thousands: float) -> str:
    rub = round(thousands * 1000, 2)
    if abs(rub - round(rub)) < 0.005:
        text = f"{int(round(rub)):,}".replace(",", " ")
    else:
        text = f"{rub:,.2f}".replace(",", " ")
    return text + " ₽"


def parse_amount_input(text: str) -> Optional[float]:
    """Сумма, введённая вручную. По умолчанию в рублях; с буквой «т» или «k» в тысячах. Возвращает тысячи."""
    t = text.strip().lower().replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    thousands = False
    for suffix in ("тыс", "т", "k", "к"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            thousands = True
            break
    try:
        value = float(t)
    except ValueError:
        return None
    if value <= 0:
        return None
    return round(value if thousands else value / 1000.0, AMOUNT_DIGITS)


def plausible(date: Optional[datetime], now: datetime) -> bool:
    return date is not None and now - timedelta(days=400) <= date <= now + timedelta(days=1)


def question_text(exp: Expense) -> str:
    lines = []
    if not exp.photo_id:
        lines.append(f"📄 <code>{esc(exp.raw[:120])}</code>")
    lines.append(f"💳 <b>{fmt_rub(exp.amount)}</b> · {esc(exp.merchant)}")
    extra = [exp.date.strftime("%d.%m.%Y %H:%M")]
    if exp.bank:
        extra.insert(0, esc(exp.bank))
    lines.append(" · ".join(extra))
    lines.append("\nВыбери категорию:")
    return "\n".join(lines)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _grid(buttons, per_row=2):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def hint_label(hint: Tuple[str, str, str]) -> str:
    cat, sub, ref = hint
    parts = [cat] + [p for p in (sub, ref) if p and p != "—"]
    label = " / ".join(parts)
    return label if len(label) <= 40 else label[:39] + "…"


def valid_hint(hint) -> Optional[Tuple[str, str, str]]:
    """Подсказка из таблицы, если такая категория и подкатегория ещё есть в боте."""
    if not hint:
        return None
    cat, sub, ref = (str(x or "").strip() for x in hint)
    if cat == WORK:
        return (cat, "—", ref or "—")
    if cat in CATEGORIES and sub in CATEGORIES[cat]:
        return (cat, sub, (ref or "—") if sub == OTHER else "—")
    return None


def main_kb(exp: Expense) -> InlineKeyboardMarkup:
    rows = []
    if exp.hint:
        rows.append([_btn(f"⭐ {hint_label(exp.hint)}", f"h:{exp.eid}")])
    rows += _grid([_btn(cat, f"c:{exp.eid}:{i}") for i, cat in enumerate(CATS)])
    rows.append([_btn("✏️ Изменить сумму", f"e:{exp.eid}")])
    rows.append([_btn("❌ Не трата", f"x:{exp.eid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sub_kb(exp: Expense) -> InlineKeyboardMarkup:
    subs = CATEGORIES[exp.category]
    rows = _grid([_btn(s, f"s:{exp.eid}:{i}") for i, s in enumerate(subs)])
    rows.append([_btn("⬅️ Назад", f"b:{exp.eid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_back_kb(exp: Expense) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("⏩ Пропустить", f"k:{exp.eid}")],
        [_btn("⬅️ Назад", f"b:{exp.eid}")],
    ])


def amount_kb(exp: Expense) -> InlineKeyboardMarkup:
    rows = []
    if exp.amount > 0:
        rows.append([_btn("⬅️ Назад", f"b:{exp.eid}")])
    rows.append([_btn("❌ Не трата", f"x:{exp.eid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


AMOUNT_HINT = "Введи сумму цифрами в рублях, например <code>1500</code> или <code>1500.50</code> (тысячи: <code>3.5т</code>)."


async def edit(bot: Bot, msg: Message, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await bot.edit_message_text(
            text=text, chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=kb
        )
    except TelegramBadRequest as e:
        log.debug("edit_message_text: %s", e)


async def fetch_image(bot: Bot, file_id: str) -> bytes:
    buf = await bot.download(file_id)
    return buf.read()


# ---------------------------------------------------------------- основной поток
#
# Как идут чеки:
#   1. submit() сразу запускает распознавание в фоне (DeepSeek читает чек, пока вы отвечаете
#      на вопросы по предыдущему) и кладёт чек в очередь.
#   2. pump_queue() — один на пользователя. Когда текущей траты нет, он берёт первый чек
#      из очереди, дожидается распознавания (уже без блокировки: кнопки и ответы работают)
#      и показывает вопрос о категории.
#   3. finalize() / «Не трата» освобождают место и снова будят pump_queue().

RECOGNIZE_PARALLEL = 3          # сколько чеков одновременно читает DeepSeek
_recognize_sem: Optional[asyncio.Semaphore] = None


def _recognize_limit() -> asyncio.Semaphore:
    global _recognize_sem
    if _recognize_sem is None:          # создаём внутри работающего цикла событий
        _recognize_sem = asyncio.Semaphore(RECOGNIZE_PARALLEL)
    return _recognize_sem


@dataclass
class Recognized:
    parsed: ParsedExpense
    raw: str                       # что писать в колонку «Исходное»
    photo_id: Optional[str]
    t_download: float = 0.0
    t_ai: float = 0.0


async def keep_typing(bot: Bot, chat_id: int) -> None:
    """Показывает «печатает…», пока идёт обработка (индикатор Telegram живёт около 5 секунд)."""
    try:
        for _ in range(75):     # не дольше 5 минут, даже если что-то зависло
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.debug("typing: %s", e)


async def recognize(bot: Bot, item: Item) -> Recognized:
    """Скачивает чек и отдаёт его DeepSeek. Ошибки не выбрасывает: они попадают в parsed.error."""
    if item.kind == "op":
        return Recognized(item.parsed, item.raw or "Из выписки", item.file_id)
    async with _recognize_limit():
        t0 = time.monotonic()
        t_download = 0.0
        now = now_local()
        try:
            if item.kind == "photo":
                data = await fetch_image(bot, item.file_id)
                t_download = time.monotonic() - t0
                parsed = await read_expense(image_bytes=data, today=now)
                raw, photo_id = "Скриншот", item.file_id
            elif item.kind == "pdf":
                data = await fetch_image(bot, item.file_id)
                t_download = time.monotonic() - t0
                parsed = await read_expense(pdf_bytes=data, today=now)
                raw, photo_id = (f"PDF: {item.name}" if item.name else "PDF"), None
            else:
                parsed = await read_expense(text=item.text, today=now)
                raw, photo_id = item.text or "", None
        except Exception as e:  # noqa: BLE001
            log.exception("Не удалось обработать чек")
            parsed = ParsedExpense(error=friendly_error(e))
            raw = {"photo": "Скриншот", "pdf": "PDF"}.get(item.kind, item.text or "")
            photo_id = item.file_id if item.kind == "photo" else None
        return Recognized(parsed, raw, photo_id, t_download, time.monotonic() - t0)


def start_recognition(bot: Bot, item: Item) -> None:
    if item.task is None:
        item.task = asyncio.create_task(recognize(bot, item))


def kick(bot: Bot, st: UserState) -> None:
    """Будит обработчик очереди (если он уже работает, второй не запускается)."""
    if st.pump is None or st.pump.done():
        st.pump = asyncio.create_task(pump_queue(bot, st))


def waiting_count(st: UserState) -> int:
    """Сколько чеков ждут своей очереди (без того, что сейчас готовится к показу)."""
    return len(st.queue) - (1 if st.current is None and st.queue else 0)


async def submit(bot: Bot, st: UserState, item: Item) -> None:
    """Принимает чек. Вызывается под st.lock и не ждёт DeepSeek."""
    start_recognition(bot, item)
    busy = st.current is not None or bool(st.queue)
    st.queue.append(item)
    if busy:
        item.queued = True
        await bot.send_message(st.chat_id, f"📥 Добавлено в очередь. В очереди: {waiting_count(st)}")
    kick(bot, st)


async def pump_queue(bot: Bot, st: UserState) -> None:
    try:
        while True:
            async with st.lock:
                if st.current is not None or not st.queue:
                    return
                item = st.queue[0]
                start_recognition(bot, item)
            # Ждём DeepSeek без блокировки: в это время работают кнопки, /cancel и приём новых чеков
            typing = None if item.task.done() else asyncio.create_task(keep_typing(bot, st.chat_id))
            try:
                await asyncio.wait({item.task})
            finally:
                if typing is not None:
                    typing.cancel()
            if item.task.cancelled():
                continue                     # очередь сбросили через /cancel
            rec = item.task.result()
            now = now_local()
            dup = None
            # Операции из сверки только что сравнили с таблицей: повторно не проверяем
            if (rec.parsed.amount > 0 and plausible(rec.parsed.date, now)
                    and not item.force and not item.key):
                dup = await find_duplicate(rec.parsed.date, rec.parsed.amount)
            hint = None if dup is not None else await find_hint(rec.parsed.merchant, now)
            async with st.lock:
                if st.current is not None or not st.queue or st.queue[0] is not item:
                    continue                 # пока ждали, очередь изменили
                st.queue.popleft()
                try:
                    await present(bot, st, item, rec, dup, now, hint)
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось показать трату")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Ошибка в обработке очереди")


async def present(bot: Bot, st: UserState, item: Item, rec: Recognized, dup, now: datetime,
                  hint: Optional[Tuple[str, str, str]] = None) -> None:
    """Показывает распознанный чек: вопрос о категории, просьбу ввести сумму или «уже есть». Под st.lock."""
    chat_id = st.chat_id
    parsed = rec.parsed
    date = parsed.date if plausible(parsed.date, now) else now
    exp = Expense(
        eid=new_eid(), date=date, amount=parsed.amount, merchant=parsed.merchant,
        bank=parsed.bank, raw=rec.raw, photo_id=rec.photo_id, skip_key=item.key, hint=hint,
    )
    if item.kind != "op":
        log.info("Время: скачивание %.1f с, DeepSeek %.1f с", rec.t_download, rec.t_ai - rec.t_download)

    # Повтор: такая же трата уже есть в таблице -> не спрашиваем, сообщаем и идём дальше
    if dup is not None:
        st.dups[exp.eid] = exp
        while len(st.dups) > 100:
            st.dups.pop(next(iter(st.dups)))
        d, rub, merchant = dup
        await bot.send_message(
            chat_id,
            f"🔁 Уже есть в таблице: {d:%d.%m.%Y %H:%M} · {fmt_rub(rub / 1000)} · {esc(merchant)}. Пропускаю.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("➕ Всё равно внести", f"d:{exp.eid}")]]),
        )
        log.info("Повтор пропущен: %s %.2f %s", exp.date, exp.amount * 1000, exp.merchant)
        return

    st.current = exp
    if rec.photo_id and item.queued:
        # фото повторяем только для трат из очереди, чтобы было понятно, к какому чеку вопрос
        await bot.send_photo(chat_id, rec.photo_id, caption="📷 Чек")

    if parsed.ops > 1:
        await bot.send_message(
            chat_id,
            f"ℹ️ В файле {parsed.ops} операций. Беру только первую (самую верхнюю). "
            "Для целых выписок есть сверка: команда /reconcile."
        )
    if exp.amount <= 0:
        exp.stage = "amount"
        reason = f" ({esc(parsed.error)})" if parsed.error else ""
        await bot.send_message(
            chat_id, f"⚠️ Не смог прочитать сумму{reason}.\n{AMOUNT_HINT}", reply_markup=amount_kb(exp)
        )
        return
    await show_question(bot, st)


DUP_MINUTES = 10   # чек с временем: повтор, если время совпадает с точностью до 10 минут


async def find_duplicate(date: datetime, amount: float):
    """Ищет в таблице такую же трату: сумма до рубля и то же время (или тот же день, если времени нет).

    amount — в тысячах рублей. Строки таблицы берутся из памяти хранилища, если они свежие.
    """
    try:
        rows = await asyncio.to_thread(
            get_storage().rows_between, date - timedelta(days=1), date + timedelta(days=1))
    except Exception as e:  # noqa: BLE001
        log.warning("Проверка на повтор не удалась: %s", e)
        return None
    rub = amount * 1000
    for r in rows:
        if abs(r.rub - rub) > 1.0:
            continue
        if (r.date.hour, r.date.minute) == (0, 0) or (date.hour, date.minute) == (0, 0):
            same = r.date.date() == date.date()
        else:
            same = abs(r.date - date) <= timedelta(minutes=DUP_MINUTES)
        if same:
            return r.date, r.rub, r.merchant
    return None


async def find_hint(merchant: str, now: datetime) -> Optional[Tuple[str, str, str]]:
    """Категория, которую выбирали для этого получателя в прошлый раз (за последние ~3 месяца)."""
    key = normalize_merchant(merchant)
    if not key:
        return None
    try:
        hints = await asyncio.to_thread(get_storage().category_hints, now)
    except Exception as e:  # noqa: BLE001
        log.warning("Подсказка категории не удалась: %s", e)
        return None
    return valid_hint(hints.get(key))


async def show_question(bot: Bot, st: UserState) -> None:
    exp = st.current
    exp.stage = "category"
    await bot.send_message(st.chat_id, question_text(exp), reply_markup=main_kb(exp))


async def ask_comment_new(bot: Bot, st: UserState) -> None:
    exp = st.current
    exp.stage = "comment"
    await bot.send_message(st.chat_id, "💬 Напиши комментарий к трате (или «-», чтобы пропустить):",
                           reply_markup=skip_back_kb(exp))


SAVED_KEEP = 20    # сколько последних записей можно отменить кнопкой


async def finalize(bot: Bot, st: UserState) -> None:
    exp = st.current
    row = [
        exp.date.strftime("%d.%m.%Y %H:%M"), exp.amount, exp.merchant, exp.category,
        exp.sub, exp.ref, exp.comment, exp.raw, exp.bank,
    ]
    note, ref = "", None
    try:
        ref = await asyncio.to_thread(get_storage().save, exp.date, row)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка записи")
        save_fallback(exp.date, row)
        note = (f"\n⚠️ Таблица недоступна ({esc(str(e)[:100])}). "
                f"Строка сохранена в файл unsaved_expenses.csv и будет дописана в таблицу, "
                f"когда она снова заработает")
    st.current = None
    where = exp.category + (f" / {exp.sub}" if exp.sub not in ("—", "") else "")
    if exp.category == WORK and exp.ref != "—":
        where += f" / {exp.ref}"
    kb = None
    if ref is not None:
        st.saved[exp.eid] = (ref, row, where, exp.amount)
        while len(st.saved) > SAVED_KEEP:
            st.saved.pop(next(iter(st.saved)))
        kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("↩️ Отменить запись", f"u:{exp.eid}")]])
        schedule_flush(bot)
    await bot.send_message(st.chat_id, f"✅ <b>Записано:</b> {esc(where)} · {fmt_rub(exp.amount)}{note}",
                           reply_markup=kb)
    kick(bot, st)


_flush_task: Optional["asyncio.Task"] = None


def schedule_flush(bot: Bot) -> None:
    """Если есть строки, не записанные из-за сбоя таблицы, дописывает их в фоне."""
    global _flush_task
    if not config.FALLBACK_FILE.exists():
        return
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.create_task(flush_unsaved(bot))


async def flush_unsaved(bot: Bot) -> None:
    try:
        moved, left = await asyncio.to_thread(flush_fallback, get_storage())
    except Exception:  # noqa: BLE001
        log.exception("Перенос unsaved_expenses.csv")
        return
    if not moved:
        return
    tail = f" Осталось: {left}, попробую ещё раз позже." if left else ""
    for uid in config.ALLOWED_USER_IDS:
        try:
            await bot.send_message(uid, f"📤 Дописал в таблицу строки, которые не удалось записать раньше: {moved}.{tail}")
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось сообщить о переносе: %s", e)


async def handle_answer(bot: Bot, st: UserState, text: str) -> None:
    exp = st.current
    if exp.stage == "amount":
        value = parse_amount_input(text)
        if value is None:
            await bot.send_message(st.chat_id, f"⚠️ Не понял сумму.\n{AMOUNT_HINT}")
            return
        exp.amount = value
        await bot.send_message(st.chat_id, f"✅ Сумма: <b>{fmt_rub(value)}</b>")
        await show_question(bot, st)
    elif exp.stage in ("object", "refine"):
        exp.ref = "—" if text in SKIP_WORDS else text
        await ask_comment_new(bot, st)
    elif exp.stage == "comment":
        exp.comment = "" if text in SKIP_WORDS else text
        await finalize(bot, st)


# ---------------------------------------------------------------- команды

WEEKDAYS_DAT = ["понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям"]
MONTHS_RU = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def welcome_text() -> str:
    if config.RECON_ENABLED:
        when = f"сама напоминает по {WEEKDAYS_DAT[config.RECON_WEEKDAY % 7]} в {config.RECON_HOUR}:00"
        recon_line = f"/reconcile — сверка с банковскими выписками ({when})\n"
    else:
        recon_line = "/reconcile — сверка с банковскими выписками\n"
    return (
        "Привет! Пришли скриншот траты (фото или файлом), PDF-чек или текст СМС, и я прочитаю сумму и магазин. "
        "Потом выберешь категорию (⭐ — как в прошлый раз у этого получателя).\n\n"
        + recon_line +
        "/stats — траты за месяц по категориям (/stats 10 — за октябрь)\n"
        "/cancel — сбросить текущую трату, очередь и сверку\n"
        "/check — проверить связь с DeepSeek и таблицей"
    )


@router.message(Command("start", "help"))
async def cmd_start(message: Message, bot: Bot) -> None:
    await bot.send_message(message.chat.id, welcome_text())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, bot: Bot) -> None:
    st = get_state(message.from_user.id, message.chat.id)
    async with st.lock:
        st.current = None
        for item in st.queue:
            if item.task is not None:
                item.task.cancel()
        st.queue.clear()
        st.recon = None
    await bot.send_message(message.chat.id, "🔄 Очередь, текущая трата и сверка сброшены.")


def stats_month(arg: str, now: datetime) -> Optional[datetime]:
    """«» -> текущий месяц, «10» -> октябрь (последний прошедший), «10.2025» -> октябрь 2025."""
    arg = arg.strip()
    if not arg:
        return datetime(now.year, now.month, 1)
    try:
        if "." in arg:
            m, y = arg.split(".", 1)
            month, year = int(m), int(y)
        else:
            month, year = int(arg), now.year
            if month > now.month:
                year -= 1
    except ValueError:
        return None
    if not 1 <= month <= 12 or year < 2000:
        return None
    return datetime(year, month, 1)


def stats_text(month: datetime, records) -> str:
    title = f"{MONTHS_RU[month.month - 1]} {month.year}"
    if not records:
        return f"📊 {title}: трат пока нет."
    total = sum(r.rub for r in records)
    by_cat: Dict[str, Dict[str, float]] = {}
    for r in records:
        cat = r.category or "Без категории"
        sub = r.sub if r.sub and r.sub != "—" else ""
        by_cat.setdefault(cat, {}).setdefault(sub, 0.0)
        by_cat[cat][sub] += r.rub
    lines = [f"📊 <b>{esc(title)}</b>: {fmt_rub(total / 1000)} ({len(records)} трат)", ""]
    for cat, subs in sorted(by_cat.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(f"<b>{esc(cat)}</b> — {fmt_rub(sum(subs.values()) / 1000)}")
        for sub, rub in sorted(subs.items(), key=lambda kv: -kv[1]):
            if sub and len(subs) > 1:
                lines.append(f"   · {esc(sub)} — {fmt_rub(rub / 1000)}")
    return "\n".join(lines)


@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot) -> None:
    arg = (message.text or "").split(maxsplit=1)
    month = stats_month(arg[1] if len(arg) > 1 else "", now_local())
    if month is None:
        await bot.send_message(message.chat.id, "Не понял месяц. Примеры: /stats, /stats 10, /stats 10.2025")
        return
    try:
        records = await asyncio.to_thread(get_storage().month_records, month)
    except Exception as e:  # noqa: BLE001
        log.exception("Итоги месяца")
        await bot.send_message(message.chat.id, f"❌ Не смог прочитать таблицу: {esc(str(e)[:200])}")
        return
    await send_chunks(bot, message.chat.id, stats_text(month, records).split("\n"))


@router.message(Command("check"))
async def cmd_check(message: Message, bot: Bot) -> None:
    lines = []
    t_start = time.monotonic()
    parsed = await read_expense(text="Покупка 150 руб. Магнит. 16.10.2026 12:00", today=now_local())
    took = time.monotonic() - t_start
    if parsed.error:
        lines.append(f"❌ DeepSeek: {esc(parsed.error)}")
    elif abs(parsed.amount - 0.15) < 0.0005:
        lines.append(f"✅ DeepSeek отвечает за {took:.1f} с (модель {esc(config.MODEL)}), сумма прочитана верно.")
    else:
        lines.append(f"⚠️ DeepSeek отвечает, но сумма прочитана как {fmt_rub(parsed.amount)}, ждали 150 ₽.")
    try:
        lines.append(f"✅ Хранилище: {esc(get_storage().label)}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ Хранилище ({esc(config.STORAGE)}): {esc(str(e)[:400])}")
    await bot.send_message(message.chat.id, "\n".join(lines))


# ---------------------------------------------------------------- входящие сообщения

@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    st = get_state(message.from_user.id, message.chat.id)
    async with st.lock:
        await submit(bot, st, Item("photo", file_id=message.photo[-1].file_id))


@router.message(F.document.mime_type.startswith("image/"))
async def on_image_file(message: Message, bot: Bot) -> None:
    st = get_state(message.from_user.id, message.chat.id)
    async with st.lock:
        await submit(bot, st, Item("photo", file_id=message.document.file_id))


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    doc = message.document
    name = doc.file_name or ""
    kind = recon.file_kind(name, doc.mime_type)
    st = get_state(message.from_user.id, message.chat.id)
    async with st.lock:
        rs = st.recon
        if rs is not None and rs.stage == "collect":
            if kind == "other":
                await bot.send_message(st.chat_id, "Для сверки принимаю PDF, Excel (.xlsx) и CSV.")
                return
            rs.files.append(ReconFile(doc.file_id, name or "файл", kind))
            await bot.send_message(
                st.chat_id, f"📎 Принял: {esc(name)} (файлов: {len(rs.files)}). Ещё файлы или «Готово».",
                reply_markup=recon_kb())
            return
        if rs is not None:
            await bot.send_message(st.chat_id, "⏳ Сверка уже идёт, дождись результата.")
            return
        if kind == "pdf":
            await submit(bot, st, Item("pdf", file_id=doc.file_id, name=name))
            return
    if kind in ("xlsx", "csv"):
        await bot.send_message(message.chat.id, "Таблицы я принимаю только для сверки с выписками. Запусти /reconcile.")
    else:
        await bot.send_message(message.chat.id, UNSUPPORTED)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    st = get_state(message.from_user.id, message.chat.id)
    text = message.text.strip()
    if not text:
        return
    async with st.lock:
        if st.current is not None and st.current.stage in ANSWER_STAGES:
            await handle_answer(bot, st, text)
        elif st.recon is not None and st.recon.stage == "pick":
            await handle_pick(bot, st, text)
        else:
            await submit(bot, st, Item("text", text=text))


UNSUPPORTED = "🤷 Этот формат я не читаю. Пришли скриншот (фото или файлом), PDF или текст СМС."


@router.message(~F.text)
async def on_unsupported(message: Message, bot: Bot) -> None:
    """Всё остальное (голосовые, видео, стикеры, другие файлы) — не молчим, а подсказываем."""
    await bot.send_message(message.chat.id, UNSUPPORTED)


# ---------------------------------------------------------------- сверка с выписками

def recon_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ Готово", "r:done"), _btn("❌ Отмена", "r:cancel")]])


RECON_INTRO = (
    "📑 <b>Сверка с выписками.</b>\n"
    "Пришли выписки по всем банкам за прошлую неделю (Альфа, Т, СПб, Точка): PDF, Excel или CSV, "
    "можно несколько файлов. Когда отправишь всё, нажми «Готово»."
)


async def start_recon(bot: Bot, st: UserState) -> None:
    """Включает режим сверки. Вызывается под st.lock."""
    if st.recon is not None and st.recon.stage != "collect":
        await bot.send_message(st.chat_id, "⏳ Сверка уже идёт. Дождись результата или нажми /cancel.")
        return
    if st.recon is None:
        st.recon = ReconState()
    note = f"\nУже принято файлов: {len(st.recon.files)}." if st.recon.files else ""
    await bot.send_message(st.chat_id, RECON_INTRO + note, reply_markup=recon_kb())


async def send_chunks(bot: Bot, chat_id: int, lines: List[str], limit: int = 3500) -> None:
    buf = ""
    for line in lines:
        if buf and len(buf) + len(line) + 1 > limit:
            await bot.send_message(chat_id, buf)
            buf = ""
        buf += line + "\n"
    if buf:
        await bot.send_message(chat_id, buf)


async def read_one_statement(bot: Bot, f: ReconFile, now: datetime):
    data = await fetch_image(bot, f.file_id)
    if f.kind == "pdf":
        return await read_statement(pdf_bytes=data, today=now)
    if f.kind == "csv":
        text = recon.csv_to_text(data)
    else:
        text = await asyncio.to_thread(recon.xlsx_to_text, data)
    return await read_statement(text=text, today=now)


async def read_recon_files(bot: Bot, files: List[ReconFile], now: datetime):
    """Читает все файлы выписок одновременно. Возвращает (операции, замечания по файлам)."""
    results = await asyncio.gather(*(read_one_statement(bot, f, now) for f in files), return_exceptions=True)
    ops, problems, seen = [], [], set()
    for f, res in zip(files, results):
        if isinstance(res, BaseException):
            log.error("Выписка %s: %s", f.name, res)
            problems.append(f"{f.name}: {friendly_error(res) if isinstance(res, Exception) else 'прервано'}")
            continue
        if res.error:
            problems.append(f"{f.name}: {res.error}")
            continue
        if not res.ops:
            problems.append(f"{f.name}: расходных операций не нашёл")
        # одна и та же операция в двух файлах (выписки пересекаются) считается один раз
        ops.extend(o for o in res.ops if recon.op_key(o) not in seen)
        seen.update(recon.op_key(o) for o in res.ops)
    return ops, problems


async def run_reconcile(bot: Bot, st: UserState, rs: ReconState) -> None:
    """Читает выписки и сравнивает с таблицей. Работает без блокировки (это долго),
    поэтому состояние меняет только под st.lock и только если сверку за это время не отменили."""
    chat_id = st.chat_id
    await bot.send_message(chat_id, f"⏳ Читаю выписки (файлов: {len(rs.files)}). Это может занять пару минут.")
    typing = asyncio.create_task(keep_typing(bot, chat_id))
    try:
        now = now_local()
        ops, problems = await read_recon_files(bot, list(rs.files), now)
        warn = ("\n⚠️ " + "\n⚠️ ".join(esc(p) for p in problems)) if problems else ""
        registry, registry_error = [], None
        if ops:
            start = min(o.date for o in ops) - timedelta(days=recon.DATE_TOLERANCE_DAYS)
            end = max(o.date for o in ops) + timedelta(days=recon.DATE_TOLERANCE_DAYS)
            try:
                registry = await asyncio.to_thread(get_storage().rows_between, start, end, True)
            except Exception as e:  # noqa: BLE001
                log.exception("Чтение реестра")
                registry_error = e
        async with st.lock:
            if st.recon is not rs:
                log.info("Сверку отменили, пока читались выписки: результат не показываю")
                return
            if not ops:
                rs.stage, rs.files = "collect", []
                await bot.send_message(chat_id, "Не нашёл ни одной расходной операции." + warn +
                                       "\nМожно прислать другие файлы.", reply_markup=recon_kb())
                return
            if registry_error is not None:
                st.recon = None
                await bot.send_message(chat_id, f"❌ Не смог прочитать таблицу: {esc(str(registry_error)[:200])}")
                return
            missing, matched = recon.find_missing(ops, registry)
            ignored = recon.load_ignored()
            fresh = [o for o in missing if recon.op_key(o) not in ignored]
            header = (f"Нашёл в выписках расходов: {len(ops)}. Уже в таблице: {matched}. "
                      f"Не нашёл в таблице: {len(fresh)}"
                      + (f" (ещё {len(missing) - len(fresh)} ты раньше отклонил)" if len(missing) != len(fresh) else "")
                      + "." + warn)
            if not fresh:
                st.recon = None
                await bot.send_message(chat_id, "✅ " + header + "\nВсё сходится.")
                return
            rs.missing, rs.stage = fresh, "pick"
            lines = [header, ""]
            for i, o in enumerate(fresh, 1):
                bank = f" ({esc(o.bank)})" if o.bank else ""
                lines.append(f"{i}. {o.date:%d.%m %H:%M} · <b>{fmt_rub(o.rub / 1000)}</b> · {esc(o.merchant)}{bank}")
            await send_chunks(bot, chat_id, lines)
            await bot.send_message(
                chat_id,
                "Какие из них внести? Напиши номера (например <code>1,3,5</code> или <code>2-4</code>), "
                "<code>все</code> или <code>нет</code>. Остальные запомню как «не нужно» и спрашивать больше не буду.")
    except Exception as e:  # noqa: BLE001
        log.exception("Сверка")
        async with st.lock:
            if st.recon is rs:
                st.recon = None
        await bot.send_message(chat_id, f"❌ Сверка не удалась: {esc(str(e)[:200])}")
    finally:
        typing.cancel()


async def handle_pick(bot: Bot, st: UserState, text: str) -> None:
    rs = st.recon
    picks = recon.parse_picks(text, len(rs.missing))
    if picks is None:
        await bot.send_message(st.chat_id, "Не понял. Напиши номера через запятую (<code>1,3,5</code> или "
                                           "<code>2-4</code>), <code>все</code> или <code>нет</code>.")
        return
    chosen = set(picks)
    recon.add_ignored(recon.op_key(o) for i, o in enumerate(rs.missing) if i not in chosen)
    items = [
        Item("op", key=recon.op_key(o),
             parsed=ParsedExpense(amount=rub_to_thousands(o.rub), merchant=o.merchant, bank=o.bank, date=o.date))
        for i, o in enumerate(rs.missing) if i in chosen
    ]
    skipped = len(rs.missing) - len(items)
    st.recon = None
    if not items:
        await bot.send_message(st.chat_id, "Хорошо, ничего не вношу. Эти операции больше спрашивать не буду.")
        return
    tail = f" Остальные ({skipped}) больше спрашивать не буду." if skipped else ""
    await bot.send_message(st.chat_id, f"Вношу операций: {len(items)}. Категории спрошу по очереди.{tail}")
    st.queue.extend(items)
    kick(bot, st)


@router.message(Command("reconcile"))
async def cmd_reconcile(message: Message, bot: Bot) -> None:
    st = get_state(message.from_user.id, message.chat.id)
    async with st.lock:
        await start_recon(bot, st)


@router.callback_query(F.data == "r:done")
async def cb_recon_done(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        rs = st.recon
        if rs is None or rs.stage != "collect":
            await bot.answer_callback_query(call.id, text="Сверка не запущена")
            return
        if not rs.files:
            await bot.answer_callback_query(call.id, text="Файлов пока нет")
            return
        rs.stage = "working"
        await bot.answer_callback_query(call.id)
    await run_reconcile(bot, st, rs)   # без блокировки: чтение выписок долгое, бот остаётся отзывчивым


@router.callback_query(F.data == "r:cancel")
async def cb_recon_cancel(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        st.recon = None
        await bot.answer_callback_query(call.id)
    await edit(bot, call.message, "🚫 Сверка отменена. Запустить снова: /reconcile")


def next_recon_time(now: datetime) -> datetime:
    target = now.replace(hour=config.RECON_HOUR, minute=0, second=0, microsecond=0)
    target += timedelta(days=(config.RECON_WEEKDAY - now.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7)
    return target


RECON_CHECK_EVERY = 600          # секунд: как часто сверяем часы
RECON_LATE_LIMIT = timedelta(hours=12)   # если компьютер спал дольше, напоминание уже не к месту


async def weekly_loop(bot: Bot) -> None:
    """Раз в неделю сам просит выписки.

    Не засыпает на неделю целиком, а проверяет время каждые 10 минут: если компьютер
    уходил в сон или переводили часы, напоминание всё равно придёт вовремя (или чуть позже)."""
    target = next_recon_time(now_local())
    log.info("Следующее напоминание о сверке: %s", target.strftime("%d.%m.%Y %H:%M"))
    while True:
        now = now_local()
        if now < target:
            await asyncio.sleep(min(RECON_CHECK_EVERY, max(1.0, (target - now).total_seconds())))
            continue
        if now - target <= RECON_LATE_LIMIT:
            for uid in config.ALLOWED_USER_IDS:
                try:
                    st = get_state(uid, uid)
                    async with st.lock:
                        await start_recon(bot, st)
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось отправить напоминание о сверке")
        else:
            log.info("Напоминание о сверке пропущено: бот не работал в %s", target.strftime("%d.%m.%Y %H:%M"))
        target = next_recon_time(now + timedelta(minutes=1))
        log.info("Следующее напоминание о сверке: %s", target.strftime("%d.%m.%Y %H:%M"))


# ---------------------------------------------------------------- кнопки

async def _current(bot: Bot, call: CallbackQuery, st: UserState) -> Optional[Expense]:
    """Возвращает текущую трату, если кнопка относится к ней, иначе сообщает об устаревшей."""
    eid = call.data.split(":")[1]
    if st.current is not None and str(st.current.eid) == eid:
        return st.current
    await edit(bot, call.message, "⚠️ Эта трата уже обработана или устарела.")
    await bot.answer_callback_query(call.id)
    return None


@router.callback_query(F.data.startswith("c:"))
async def cb_category(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        exp = await _current(bot, call, st)
        if exp is None:
            return
        exp.category = CATS[int(call.data.split(":")[2])]
        exp.sub, exp.ref = "—", "—"
        if exp.category == WORK:
            exp.stage = "object"
            await edit(bot, call.message, "📂 Категория: <b>Работа</b>\n✍️ Введи название объекта:", skip_back_kb(exp))
        else:
            exp.stage = "sub"
            await edit(bot, call.message, f"📍 Категория: <b>{esc(exp.category)}</b>\nВыбери подкатегорию:", sub_kb(exp))
        await bot.answer_callback_query(call.id)


@router.callback_query(F.data.startswith("s:"))
async def cb_sub(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        exp = await _current(bot, call, st)
        if exp is None:
            return
        exp.sub = CATEGORIES[exp.category][int(call.data.split(":")[2])]
        if exp.sub == OTHER:
            exp.stage = "refine"
            await edit(bot, call.message, "📝 Подкатегория: <b>Прочее</b>\nУточни детали:", skip_back_kb(exp))
        else:
            exp.stage = "comment"
            await edit(bot, call.message, "💬 Напиши комментарий к трате (или «-», чтобы пропустить):",
                       skip_back_kb(exp))
        await bot.answer_callback_query(call.id)


@router.callback_query(F.data.startswith("k:"))
async def cb_skip(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        exp = await _current(bot, call, st)
        if exp is None:
            return
        if exp.stage in ("object", "refine"):
            exp.ref = "—"
            exp.stage = "comment"
            await edit(bot, call.message, "💬 Напиши комментарий к трате (или «-», чтобы пропустить):",
                       skip_back_kb(exp))
        elif exp.stage == "comment":
            exp.comment = ""
            await edit(bot, call.message, "⏩ Без комментария")
            await bot.answer_callback_query(call.id)
            await finalize(bot, st)
            return
        await bot.answer_callback_query(call.id)


@router.callback_query(F.data.startswith("b:"))
async def cb_back(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        exp = await _current(bot, call, st)
        if exp is None:
            return
        exp.stage, exp.sub, exp.ref = "category", "—", "—"
        await edit(bot, call.message, question_text(exp), main_kb(exp))
        await bot.answer_callback_query(call.id)


@router.callback_query(F.data.startswith("e:"))
async def cb_edit_amount(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        exp = await _current(bot, call, st)
        if exp is None:
            return
        exp.stage = "amount"
        await edit(bot, call.message, f"✏️ {AMOUNT_HINT}", amount_kb(exp))
        await bot.answer_callback_query(call.id)


@router.callback_query(F.data.startswith("d:"))
async def cb_dup_add(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        exp = st.dups.pop(int(call.data.split(":")[1]), None)
        await bot.answer_callback_query(call.id)
        if exp is None:
            await edit(bot, call.message, "⚠️ Эта кнопка устарела. Пришли чек ещё раз.")
            return
        await edit(bot, call.message, f"➕ Вношу, хотя похоже на повтор: {fmt_rub(exp.amount)} · {esc(exp.merchant)}")
        item = Item("op", file_id=exp.photo_id, raw=exp.raw, force=True,
                    parsed=ParsedExpense(amount=exp.amount, merchant=exp.merchant, bank=exp.bank, date=exp.date))
        await submit(bot, st, item)


@router.callback_query(F.data.startswith("h:"))
async def cb_hint(call: CallbackQuery, bot: Bot) -> None:
    """«⭐ Как в прошлый раз»: категория, подкатегория и уточнение одной кнопкой, дальше — комментарий."""
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        exp = await _current(bot, call, st)
        if exp is None:
            return
        if not exp.hint:
            await bot.answer_callback_query(call.id)
            return
        exp.category, exp.sub, exp.ref = exp.hint
        exp.stage = "comment"
        await edit(bot, call.message,
                   f"⭐ <b>{esc(hint_label(exp.hint))}</b> · {fmt_rub(exp.amount)}\n"
                   "💬 Напиши комментарий к трате (или «-», чтобы пропустить):", skip_back_kb(exp))
        await bot.answer_callback_query(call.id)


@router.callback_query(F.data.startswith("u:"))
async def cb_undo(call: CallbackQuery, bot: Bot) -> None:
    """«↩️ Отменить запись»: удаляет строку из таблицы, если на её месте всё ещё она."""
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        try:
            saved = st.saved.pop(int(call.data.split(":")[1]), None)
        except ValueError:
            saved = None
        await bot.answer_callback_query(call.id)
        if saved is None:
            await edit(bot, call.message, "⚠️ Эту запись уже нельзя отменить кнопкой. Удалите строку в таблице вручную.")
            return
        ref, row, where, amount = saved
        try:
            ok = await asyncio.to_thread(get_storage().delete, ref, row)
        except Exception as e:  # noqa: BLE001
            log.exception("Отмена записи")
            st.saved[int(call.data.split(":")[1])] = saved     # можно попробовать ещё раз
            await bot.send_message(st.chat_id, f"❌ Не получилось отменить: {esc(str(e)[:150])}. Попробуйте ещё раз.")
            return
        if ok:
            log.info("Запись отменена: %s %s", row[0], row[2])
            await edit(bot, call.message, f"↩️ Запись отменена: {esc(where)} · {fmt_rub(amount)}")
        else:
            await edit(bot, call.message,
                       f"⚠️ Не нашёл эту строку на прежнем месте (таблицу меняли). "
                       f"Удалите вручную: {esc(row[0])} · {fmt_rub(amount)} · {esc(row[2])}")


@router.callback_query(F.data.startswith("x:"))
async def cb_cancel(call: CallbackQuery, bot: Bot) -> None:
    st = get_state(call.from_user.id, call.message.chat.id)
    async with st.lock:
        exp = await _current(bot, call, st)
        if exp is None:
            return
        st.current = None
        if exp.skip_key:
            recon.add_ignored([exp.skip_key])   # операция из выписки: больше не спрашиваем
        await edit(bot, call.message, "🚫 Трата отменена.")
        await bot.answer_callback_query(call.id)
        kick(bot, st)


# ---------------------------------------------------------------- сохранение состояния
#
# Текущая трата, очередь, сверка и последние записи раз в пару секунд (если что-то изменилось)
# пишутся в state.json. После перезапуска бот продолжает с того же места, а кнопки
# в старых сообщениях продолжают работать (номера трат сохраняются).

STATE_SAVE_EVERY = 2.0    # секунд


def _dt(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


def _from_dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def parsed_to_dict(p: ParsedExpense) -> dict:
    d = asdict(p)
    d["date"] = _dt(p.date)
    return d


def parsed_from_dict(d: dict) -> ParsedExpense:
    fields = {k: v for k, v in d.items() if k in ParsedExpense.__dataclass_fields__}
    fields["date"] = _from_dt(fields.get("date"))
    return ParsedExpense(**fields)


def item_to_dict(item: Item) -> dict:
    d = dict(kind=item.kind, file_id=item.file_id, text=item.text, name=item.name, key=item.key,
             force=item.force, raw=item.raw, queued=item.queued,
             parsed=parsed_to_dict(item.parsed) if item.parsed else None)
    t = item.task
    if item.kind != "op" and t is not None and t.done() and not t.cancelled() and t.exception() is None:
        rec = t.result()
        if not rec.parsed.error:
            # DeepSeek уже прочитал чек: сохраняем результат, чтобы не платить за чтение второй раз
            d.update(kind="op", parsed=parsed_to_dict(rec.parsed), raw=rec.raw, file_id=rec.photo_id)
    return d


def item_from_dict(d: dict) -> Item:
    parsed = parsed_from_dict(d["parsed"]) if d.get("parsed") else None
    return Item(kind=d["kind"], file_id=d.get("file_id"), text=d.get("text"), name=d.get("name") or "",
                parsed=parsed, key=d.get("key") or "", force=bool(d.get("force")), raw=d.get("raw") or "",
                queued=bool(d.get("queued")))


def expense_to_dict(e: Expense) -> dict:
    d = asdict(e)
    d["date"] = _dt(e.date)
    return d


def expense_from_dict(d: dict) -> Expense:
    fields = {k: v for k, v in d.items() if k in Expense.__dataclass_fields__}
    fields["date"] = _from_dt(fields["date"])
    if fields.get("hint"):
        fields["hint"] = tuple(fields["hint"])
    return Expense(**fields)


def snapshot() -> dict:
    users = {}
    for uid, st in STATES.items():
        u = {"chat_id": st.chat_id}
        if st.current is not None:
            u["current"] = expense_to_dict(st.current)
        if st.queue:
            u["queue"] = [item_to_dict(i) for i in st.queue]
        if st.recon is not None:
            u["recon"] = {
                "files": [asdict(f) for f in st.recon.files],
                "stage": st.recon.stage,
                "missing": [dict(date=_dt(o.date), rub=o.rub, merchant=o.merchant, bank=o.bank)
                            for o in st.recon.missing],
            }
        if st.dups:
            u["dups"] = {str(k): expense_to_dict(v) for k, v in st.dups.items()}
        if st.saved:
            u["saved"] = {str(k): list(v) for k, v in st.saved.items()}
        if len(u) > 1:
            users[str(uid)] = u
    return {"version": 1, "last_eid": _last_eid, "users": users}


def restore(data: dict) -> None:
    global _last_eid
    _last_eid = max(_last_eid, int(data.get("last_eid") or 0))
    for uid, u in (data.get("users") or {}).items():
        st = UserState(chat_id=int(u.get("chat_id") or uid))
        if u.get("current"):
            st.current = expense_from_dict(u["current"])
        st.queue.extend(item_from_dict(i) for i in u.get("queue") or [])
        if u.get("recon"):
            r = u["recon"]
            stage = r.get("stage") or "collect"
            st.recon = ReconState(
                files=[ReconFile(**f) for f in r.get("files") or []],
                stage="collect" if stage == "working" else stage,      # чтение прервалось: файлы просим подтвердить
                missing=[StatementOp(date=_from_dt(o["date"]), rub=o["rub"], merchant=o["merchant"],
                                     bank=o.get("bank") or "") for o in r.get("missing") or []],
            )
            if st.recon.stage == "pick" and not st.recon.missing:
                st.recon = None
        st.dups = {int(k): expense_from_dict(v) for k, v in (u.get("dups") or {}).items()}
        for k, v in (u.get("saved") or {}).items():
            ref, row, where, amount = v
            st.saved[int(k)] = (tuple(ref) if isinstance(ref, list) else ref, row, where, amount)
        STATES[int(uid)] = st


def load_state() -> None:
    try:
        with open(config.STATE_FILE, encoding="utf-8") as f:
            restore(json.load(f))
        log.info("Состояние восстановлено из %s", config.STATE_FILE.name)
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("Не удалось прочитать %s, начинаю с чистого листа", config.STATE_FILE.name)


def write_state(last: Optional[str] = None) -> Optional[str]:
    """Пишет состояние, если оно изменилось. Возвращает записанный текст (для сравнения в следующий раз)."""
    try:
        text = json.dumps(snapshot(), ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось подготовить состояние к сохранению")
        return last
    if text == last:
        return last
    try:
        tmp = config.STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, config.STATE_FILE)
    except OSError as e:
        log.warning("Не удалось сохранить состояние: %s", e)
        return last
    return text


async def state_saver() -> None:
    last = None
    while True:
        await asyncio.sleep(STATE_SAVE_EVERY)
        last = write_state(last)


async def resume_after_restart(bot: Bot) -> None:
    """После перезапуска напоминает, на чём остановились, и продолжает очередь."""
    for uid, st in list(STATES.items()):
        try:
            async with st.lock:
                if st.current is not None or st.queue:
                    waiting = len(st.queue)
                    tail = f" В очереди ещё: {waiting}." if waiting else ""
                    await bot.send_message(st.chat_id, f"♻️ Бот перезапускался, ничего не потерялось.{tail}")
                    if st.current is not None:
                        if st.current.stage == "amount" or st.current.amount <= 0:
                            st.current.stage = "amount"
                            await bot.send_message(st.chat_id, f"Продолжаем: {AMOUNT_HINT}",
                                                   reply_markup=amount_kb(st.current))
                        else:
                            await show_question(bot, st)
                rs = st.recon
                if rs is not None and rs.stage == "collect" and rs.files:
                    await bot.send_message(
                        st.chat_id, f"♻️ Сверка: принято файлов {len(rs.files)}. Пришли ещё или нажми «Готово».",
                        reply_markup=recon_kb())
                elif rs is not None and rs.stage == "pick":
                    await bot.send_message(
                        st.chat_id, f"♻️ Сверка: жду номера операций для внесения (из {len(rs.missing)}), "
                                    "<code>все</code> или <code>нет</code>.")
                kick(bot, st)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось продолжить после перезапуска")


# ---------------------------------------------------------------- запуск

async def connect(bot: Bot, attempts: int = 10, delay: float = 10.0):
    """Подключение к Telegram с повторами: нестабильный интернет иногда отвечает не с первого раза."""
    for attempt in range(1, attempts + 1):
        try:
            return await bot.get_me()
        except TelegramNetworkError as e:
            log.warning("Нет связи с Telegram (попытка %d из %d): %s", attempt, attempts, e)
            if attempt < attempts:
                print(f"Нет связи с Telegram (попытка {attempt} из {attempts}). "
                      f"Пробую снова через {int(delay)} с...")
                await asyncio.sleep(delay)
    return None


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            # журнал не растёт бесконечно: 3 файла по 5 МБ (bot.log, bot.log.1, bot.log.2)
            RotatingFileHandler(config.BASE_DIR / "bot.log", maxBytes=5_000_000, backupCount=2, encoding="utf-8"),
        ],
    )
    try:
        storage = get_storage()  # если таблица недоступна, узнаем сразу, а не при первой трате
    except Exception as e:  # noqa: BLE001
        log.error("Хранилище недоступно: %s", e)
        print(f"\nНе удалось подключить хранилище трат.\n{e}\n")
        return
    print(f"Хранилище трат: {storage.label}")
    session = AiohttpSession()
    if config.FORCE_IPV4:
        try:
            session._connector_init["family"] = socket.AF_INET
        except Exception:  # noqa: BLE001
            log.warning("Не удалось включить режим IPv4, продолжаю без него")
    bot = Bot(token=config.TELEGRAM_TOKEN, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    me = await connect(bot)
    if me is None:
        log.error("Не удалось подключиться к Telegram")
        print("\nНе удаётся подключиться к Telegram. Проверьте интернет и антивирус/брандмауэр "
              "(они могут блокировать Python). Подробности в файле bot.log.\n")
        await bot.session.close()
        return
    log.info("Бот запущен: @%s | хранилище: %s | модель: %s", me.username, config.STORAGE, config.MODEL)
    print(f"\nБот @{me.username} запущен. Напишите ему в Telegram. Остановка: Ctrl+C\n")
    try:
        # Сообщения, присланные, пока бот был выключен или перезапускался, не выбрасываем:
        # иначе чек, отправленный в эту минуту, молча пропадёт.
        await bot.delete_webhook(drop_pending_updates=False)
    except TelegramNetworkError as e:
        log.warning("Не удалось сбросить webhook (%s), продолжаю", e)
    load_state()
    # держим ссылки на фоновые задачи, чтобы их не удалил сборщик мусора
    background = [asyncio.create_task(state_saver())]
    if config.RECON_ENABLED:
        background.append(asyncio.create_task(weekly_loop(bot)))
    await resume_after_restart(bot)
    schedule_flush(bot)      # строки, не записанные из-за сбоя таблицы до перезапуска
    try:
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        write_state()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
