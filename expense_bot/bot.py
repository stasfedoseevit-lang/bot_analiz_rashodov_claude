"""Telegram-бот учёта трат: скриншот или текст -> Claude читает сумму -> вы выбираете категорию -> запись."""
import asyncio
import html
import itertools
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional
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
from claude_parse import ParsedExpense, friendly_error, read_expense, read_statement
from storage import get_storage, save_fallback

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


STATES: Dict[int, UserState] = {}
_eids = itertools.count(1)


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
    return round(value if thousands else value / 1000.0, 3)


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


def main_kb(exp: Expense) -> InlineKeyboardMarkup:
    rows = _grid([_btn(cat, f"c:{exp.eid}:{i}") for i, cat in enumerate(CATS)])
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

async def keep_typing(bot: Bot, chat_id: int) -> None:
    """Показывает «печатает…», пока идёт обработка (индикатор Telegram живёт около 5 секунд)."""
    try:
        for _ in range(8):
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except Exception as e:  # noqa: BLE001
        log.debug("typing: %s", e)


async def process_item(bot: Bot, st: UserState, item: Item, from_queue: bool = False) -> None:
    chat_id = st.chat_id
    t0 = time.monotonic()
    t_download = 0.0
    now = now_local()
    photo_id = None
    typing = asyncio.create_task(keep_typing(bot, chat_id))  # не задерживает основную работу
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
            raw = f"PDF: {item.name}" if item.name else "PDF"
        elif item.kind == "op":
            parsed, raw = item.parsed, item.raw or "Из выписки"
            photo_id = item.file_id
        else:
            parsed = await read_expense(text=item.text, today=now)
            raw = item.text or ""
    except Exception as e:  # noqa: BLE001
        log.exception("Не удалось обработать чек")
        parsed = ParsedExpense(error=friendly_error(e))
        raw = {"photo": "Скриншот", "pdf": "PDF"}.get(item.kind, item.text or "")
        photo_id = item.file_id if item.kind == "photo" else None

    typing.cancel()
    t_ai = time.monotonic() - t0
    date = parsed.date if plausible(parsed.date, now) else now
    exp = Expense(
        eid=next(_eids), date=date, amount=parsed.amount, merchant=parsed.merchant,
        bank=parsed.bank, raw=raw, photo_id=photo_id, skip_key=item.key,
    )

    def log_times() -> None:
        log.info("Время: скачивание %.1f с, Claude %.1f с, всего %.1f с",
                 t_download, t_ai - t_download, time.monotonic() - t0)

    # Повтор: такая же трата уже есть в таблице -> не спрашиваем, сообщаем и идём дальше
    if exp.amount > 0 and plausible(parsed.date, now) and not item.force:
        dup = await find_duplicate(exp)
        if dup is not None:
            st.current = None
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
            log_times()
            await start_next(bot, st)
            return

    st.current = exp
    if photo_id and from_queue:
        # фото повторяем только для трат из очереди, чтобы было понятно, к какому чеку вопрос
        await bot.send_photo(chat_id, photo_id, caption="📷 Чек")

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
        log_times()
        return
    await show_question(bot, st)
    log_times()


DUP_MINUTES = 10   # чек с временем: повтор, если время совпадает с точностью до 10 минут


async def find_duplicate(exp: Expense):
    """Ищет в таблице такую же трату: сумма до рубля и то же время (или тот же день, если времени нет)."""
    try:
        rows = await asyncio.to_thread(
            get_storage().rows_between, exp.date - timedelta(days=1), exp.date + timedelta(days=1))
    except Exception as e:  # noqa: BLE001
        log.warning("Проверка на повтор не удалась: %s", e)
        return None
    rub = exp.amount * 1000
    for d, amount, merchant in rows:
        if abs(amount - rub) > 1.0:
            continue
        if (d.hour, d.minute) == (0, 0) or (exp.date.hour, exp.date.minute) == (0, 0):
            same = d.date() == exp.date.date()
        else:
            same = abs(d - exp.date) <= timedelta(minutes=DUP_MINUTES)
        if same:
            return d, amount, merchant
    return None


async def show_question(bot: Bot, st: UserState) -> None:
    exp = st.current
    exp.stage = "category"
    await bot.send_message(st.chat_id, question_text(exp), reply_markup=main_kb(exp))


async def submit(bot: Bot, st: UserState, item: Item) -> None:
    if st.current is not None:
        st.queue.append(item)
        await bot.send_message(st.chat_id, f"📥 Добавлено в очередь. В очереди: {len(st.queue)}")
        return
    await process_item(bot, st, item)


async def start_next(bot: Bot, st: UserState) -> None:
    if st.queue:
        await process_item(bot, st, st.queue.popleft(), from_queue=True)


async def ask_comment_new(bot: Bot, st: UserState) -> None:
    exp = st.current
    exp.stage = "comment"
    await bot.send_message(st.chat_id, "💬 Напиши комментарий к трате (или «-», чтобы пропустить):",
                           reply_markup=skip_back_kb(exp))


async def finalize(bot: Bot, st: UserState) -> None:
    exp = st.current
    row = [
        exp.date.strftime("%d.%m.%Y %H:%M"), exp.amount, exp.merchant, exp.category,
        exp.sub, exp.ref, exp.comment, exp.raw, exp.bank,
    ]
    note = ""
    try:
        await asyncio.to_thread(get_storage().save, exp.date, row)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка записи")
        save_fallback(exp.date, row)
        note = (f"\n⚠️ Таблица недоступна ({esc(str(e)[:100])}). "
                f"Строка сохранена в файл unsaved_expenses.csv")
    st.current = None
    where = exp.category + (f" / {exp.sub}" if exp.sub not in ("—", "") else "")
    if exp.category == WORK and exp.ref != "—":
        where += f" / {exp.ref}"
    await bot.send_message(st.chat_id, f"✅ <b>Записано:</b> {esc(where)} · {fmt_rub(exp.amount)}{note}")
    await start_next(bot, st)


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

WELCOME = (
    "Привет! Пришли скриншот траты (фото или файлом), PDF-чек или текст СМС, и я прочитаю сумму и магазин. "
    "Потом выберешь категорию.\n\n"
    "/reconcile — сверка с банковскими выписками (сама напоминает по понедельникам в 15:00)\n"
    "/cancel — сбросить текущую трату, очередь и сверку\n"
    "/check — проверить связь с Claude и таблицей"
)


@router.message(Command("start", "help"))
async def cmd_start(message: Message, bot: Bot) -> None:
    await bot.send_message(message.chat.id, WELCOME)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, bot: Bot) -> None:
    st = get_state(message.from_user.id, message.chat.id)
    async with st.lock:
        st.current = None
        st.queue.clear()
        st.recon = None
    await bot.send_message(message.chat.id, "🔄 Очередь, текущая трата и сверка сброшены.")


@router.message(Command("check"))
async def cmd_check(message: Message, bot: Bot) -> None:
    lines = []
    t_start = time.monotonic()
    parsed = await read_expense(text="Покупка 150 руб. Магнит. 16.10.2026 12:00", today=now_local())
    took = time.monotonic() - t_start
    if parsed.error:
        lines.append(f"❌ Claude: {esc(parsed.error)}")
    elif abs(parsed.amount - 0.15) < 0.0005:
        lines.append(f"✅ Claude отвечает за {took:.1f} с (модель {esc(config.MODEL)}), сумма прочитана верно.")
    else:
        lines.append(f"⚠️ Claude отвечает, но сумма прочитана как {fmt_rub(parsed.amount)}, ждали 150 ₽.")
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


async def read_recon_files(bot: Bot, rs: ReconState, now: datetime):
    """Читает все файлы выписок. Возвращает (операции, замечания по файлам)."""
    ops, problems, seen = [], [], set()
    for f in rs.files:
        try:
            data = await fetch_image(bot, f.file_id)
            if f.kind == "pdf":
                res = await read_statement(pdf_bytes=data, today=now)
            else:
                if f.kind == "csv":
                    text = recon.csv_to_text(data)
                else:
                    text = await asyncio.to_thread(recon.xlsx_to_text, data)
                res = await read_statement(text=text, today=now)
        except Exception as e:  # noqa: BLE001
            log.exception("Выписка %s", f.name)
            problems.append(f"{f.name}: {str(e)[:120]}")
            continue
        if res.error:
            problems.append(f"{f.name}: {res.error}")
            continue
        if res.truncated:
            problems.append(f"{f.name}: выписка слишком длинная, прочитана не до конца. Пришли её по частям (по неделям)")
        if not res.ops:
            problems.append(f"{f.name}: расходных операций не нашёл")
        # одна и та же операция в двух файлах (выписки пересекаются) считается один раз
        ops.extend(o for o in res.ops if recon.op_key(o) not in seen)
        seen.update(recon.op_key(o) for o in res.ops)
    return ops, problems


async def run_reconcile(bot: Bot, st: UserState, rs: ReconState) -> None:
    chat_id = st.chat_id
    await bot.send_message(chat_id, f"⏳ Читаю выписки (файлов: {len(rs.files)}). Это может занять пару минут.")
    typing = asyncio.create_task(keep_typing(bot, chat_id))
    try:
        now = now_local()
        ops, problems = await read_recon_files(bot, rs, now)
        warn = ("\n⚠️ " + "\n⚠️ ".join(esc(p) for p in problems)) if problems else ""
        if not ops:
            rs.stage, rs.files = "collect", []
            await bot.send_message(chat_id, "Не нашёл ни одной расходной операции." + warn +
                                   "\nМожно прислать другие файлы.", reply_markup=recon_kb())
            return
        start = min(o.date for o in ops) - timedelta(days=recon.DATE_TOLERANCE_DAYS)
        end = max(o.date for o in ops) + timedelta(days=recon.DATE_TOLERANCE_DAYS)
        try:
            registry = await asyncio.to_thread(get_storage().rows_between, start, end)
        except Exception as e:  # noqa: BLE001
            log.exception("Чтение реестра")
            st.recon = None
            await bot.send_message(chat_id, f"❌ Не смог прочитать таблицу: {esc(str(e)[:200])}")
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
             parsed=ParsedExpense(amount=round(o.rub / 1000.0, 3), merchant=o.merchant, bank=o.bank, date=o.date))
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
    if st.current is None:
        await start_next(bot, st)


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


async def weekly_loop(bot: Bot) -> None:
    """Раз в неделю сам просит выписки."""
    while True:
        now = now_local()
        target = next_recon_time(now)
        log.info("Следующее напоминание о сверке: %s", target.strftime("%d.%m.%Y %H:%M"))
        await asyncio.sleep((target - now).total_seconds())
        for uid in config.ALLOWED_USER_IDS:
            try:
                st = get_state(uid, uid)
                async with st.lock:
                    await start_recon(bot, st)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось отправить напоминание о сверке")
        await asyncio.sleep(60)


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
        await start_next(bot, st)


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
        handlers=[logging.StreamHandler(), logging.FileHandler(config.BASE_DIR / "bot.log", encoding="utf-8")],
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
    recon_task = None
    if config.RECON_ENABLED:
        recon_task = asyncio.create_task(weekly_loop(bot))  # держим ссылку, чтобы задачу не удалил сборщик мусора
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
