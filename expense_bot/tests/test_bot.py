import asyncio
import re
import time
from datetime import datetime

import pytest

import bot as B
import config
import storage
from ai_parse import ParsedExpense, StatementOp


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))

    async def send_photo(self, *a, **k):
        pass

    async def send_chat_action(self, *a, **k):
        pass

    async def edit_message_text(self, **k):
        self.sent.append((k["text"], k.get("reply_markup")))

    async def answer_callback_query(self, *a, **k):
        pass

    def texts(self):
        return [t for t, _ in self.sent]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    B.STATES.clear()
    monkeypatch.setattr(B, "_recognize_sem", None)
    monkeypatch.setattr(B, "now_local", lambda: datetime(2026, 11, 15, 18, 0))

    async def fake_read(*, text=None, image_bytes=None, pdf_bytes=None, today=None):
        await asyncio.sleep(0.3)
        m = re.search(r"(\d+)", text or "")
        if not m:
            return ParsedExpense()
        n = int(m.group(1))
        return ParsedExpense(amount=n / 1000, merchant=f"Магазин {n}", date=datetime(2026, 11, 15, 12, n % 60))
    monkeypatch.setattr(B, "read_expense", fake_read)
    yield


# ---------------------------------------------------------------- простые функции

def test_parse_amount_input_keeps_kopecks():
    assert B.parse_amount_input("1 500,50") == pytest.approx(1.5005)
    assert B.parse_amount_input("3.5т") == pytest.approx(3.5)
    assert B.parse_amount_input("abc") is None and B.parse_amount_input("-5") is None


def test_fmt_rub():
    assert B.fmt_rub(2.8985) == "2 898.50 ₽"
    assert B.fmt_rub(1.5) == "1 500 ₽"


def test_valid_hint():
    assert B.valid_hint(("Я", "Транспорт", "")) == ("Я", "Транспорт", "—")
    assert B.valid_hint(("Я", "Прочее", "хлеб")) == ("Я", "Прочее", "хлеб")
    assert B.valid_hint(("Работа", "", "Объект")) == ("Работа", "—", "Объект")
    assert B.valid_hint(("Нет такой", "x", "")) is None


def test_stats_month():
    now = datetime(2026, 3, 10)
    assert B.stats_month("", now) == datetime(2026, 3, 1)
    assert B.stats_month("10", now) == datetime(2025, 10, 1)
    assert B.stats_month("2", now) == datetime(2026, 2, 1)
    assert B.stats_month("13", now) is None


def test_stats_text():
    rec = storage.Record
    text = B.stats_text(datetime(2026, 11, 1), [
        rec(datetime(2026, 11, 1), 1000, "a", "Я", "Транспорт"),
        rec(datetime(2026, 11, 2), 500, "b", "Я", "Прочее"),
        rec(datetime(2026, 11, 3), 2000, "c", "Работа", "—"),
    ])
    assert "3 500 ₽" in text and "Работа</b> — 2 000 ₽" in text and "Транспорт — 1 000 ₽" in text


def test_next_recon_time(monkeypatch):
    monkeypatch.setattr(config, "RECON_WEEKDAY", 0)
    monkeypatch.setattr(config, "RECON_HOUR", 15)
    assert B.next_recon_time(datetime(2026, 11, 15, 18, 0)) == datetime(2026, 11, 16, 15, 0)   # вс -> пн
    assert B.next_recon_time(datetime(2026, 11, 16, 15, 0)) == datetime(2026, 11, 23, 15, 0)


# ---------------------------------------------------------------- поток трат

async def wait_for(cond, timeout=3.0):
    t = time.monotonic()
    while not cond():
        assert time.monotonic() - t < timeout, "не дождались"
        await asyncio.sleep(0.01)


async def choose(bot, st, cat="Я", sub="Транспорт"):
    async with st.lock:
        st.current.category, st.current.sub = cat, sub
        await B.finalize(bot, st)


def test_queue_is_recognized_in_background():
    async def run():
        bot, st = FakeBot(), B.get_state(1, 1)
        for n in (100, 200, 300):
            async with st.lock:
                await B.submit(bot, st, B.Item("text", text=f"Покупка {n}"))
        await wait_for(lambda: st.current is not None)
        await asyncio.sleep(0.4)          # пока думаем, остальные чеки уже прочитаны
        t = time.monotonic()
        await choose(bot, st)
        await wait_for(lambda: st.current is not None and st.current.merchant == "Магазин 200")
        assert time.monotonic() - t < 0.2
        # кнопка «Отменить запись» у сохранённой траты
        assert any(kb and kb.inline_keyboard[0][0].callback_data.startswith("u:") for _, kb in bot.sent)
    asyncio.run(run())


def test_duplicate_is_skipped():
    async def run():
        bot, st = FakeBot(), B.get_state(1, 1)
        async with st.lock:
            await B.submit(bot, st, B.Item("text", text="Покупка 100"))
        await wait_for(lambda: st.current is not None)
        await choose(bot, st)
        # тот же чек ещё раз -> «Уже есть», без вопроса, и кнопка «Всё равно внести»
        async with st.lock:
            await B.submit(bot, st, B.Item("text", text="Покупка 100"))
        await wait_for(lambda: any("Уже есть" in t for t in bot.texts()))
        assert st.current is None and len(st.dups) == 1
    asyncio.run(run())


def test_hint_button_shown(monkeypatch):
    async def run():
        s = storage.CsvStorage(config.CSV_FILE)
        s.save(datetime(2026, 11, 1), ["01.11.2026 10:00", 0.1, "Магазин 250", "Я", "Транспорт", "—", "", "", ""])
        bot, st = FakeBot(), B.get_state(1, 1)
        async with st.lock:
            await B.submit(bot, st, B.Item("text", text="Покупка 250"))
        await wait_for(lambda: st.current is not None)
        assert st.current.hint == ("Я", "Транспорт", "—")
        kb = bot.sent[-1][1]
        assert kb.inline_keyboard[0][0].text == "⭐ Я / Транспорт"
    asyncio.run(run())


def test_undo_removes_row():
    async def run():
        bot, st = FakeBot(), B.get_state(1, 1)
        async with st.lock:
            await B.submit(bot, st, B.Item("text", text="Покупка 100"))
        await wait_for(lambda: st.current is not None)
        eid = st.current.eid
        await choose(bot, st)
        s = storage.get_storage()
        assert len(s.month_records(datetime(2026, 11, 1))) == 1
        ref, row, where, amount = st.saved[eid]
        assert s.delete(ref, row)
        assert len(s.month_records(datetime(2026, 11, 1))) == 0
    asyncio.run(run())


def test_cancel_stops_background_reading():
    async def run():
        bot, st = FakeBot(), B.get_state(1, 1)
        async with st.lock:
            await B.submit(bot, st, B.Item("text", text="Покупка 400"))
            await B.submit(bot, st, B.Item("text", text="Покупка 500"))
        await asyncio.sleep(0.05)
        async with st.lock:
            for it in st.queue:
                it.task.cancel()
            st.queue.clear()
        await asyncio.sleep(0.5)
        assert st.current is None and st.pump.done()
    asyncio.run(run())


def test_state_survives_restart():
    async def run():
        bot, st = FakeBot(), B.get_state(1, 1)
        for n in (100, 200, 300):
            async with st.lock:
                await B.submit(bot, st, B.Item("text", text=f"Покупка {n}"))
        await wait_for(lambda: st.current is not None)
        await asyncio.sleep(0.4)
        st.recon = B.ReconState(stage="pick", missing=[StatementOp(datetime(2026, 11, 1, 10), 150.0, "Кофе", "Т-Банк")])
        B.write_state()
        current_eid = st.current.eid
        B.STATES.clear()
        B.load_state()
        st2 = B.STATES[1]
        assert st2.current.eid == current_eid and st2.current.merchant == "Магазин 100"
        # уже прочитанные чеки сохранены вместе с результатом: Claude второй раз не нужен
        assert [(i.kind, i.parsed.merchant) for i in st2.queue] == [("op", "Магазин 200"), ("op", "Магазин 300")]
        assert st2.recon.stage == "pick" and st2.recon.missing[0].merchant == "Кофе"
        assert B.new_eid() > current_eid
    asyncio.run(run())


def test_recon_result_ignored_after_cancel(monkeypatch):
    async def run():
        bot, st = FakeBot(), B.get_state(1, 1)

        async def slow_read(bot_, files, now):
            await asyncio.sleep(0.2)
            return [StatementOp(datetime(2026, 11, 1, 10), 150.0, "Кофе")], []
        monkeypatch.setattr(B, "read_recon_files", slow_read)
        rs = B.ReconState(files=[B.ReconFile("f", "a.pdf", "pdf")], stage="working")
        st.recon = rs
        task = asyncio.create_task(B.run_reconcile(bot, st, rs))
        await asyncio.sleep(0.05)
        async with st.lock:                 # /cancel и сразу новая сверка
            st.recon = B.ReconState()
        await task
        assert st.recon is not None and st.recon.stage == "collect"   # новую сверку не сбросило
        assert not any("Какие из них внести" in t for t in bot.texts())
    asyncio.run(run())


def _call(data):
    from types import SimpleNamespace as NS
    return NS(id="1", data=data, from_user=NS(id=1), message=NS(chat=NS(id=1), message_id=10))


def test_hint_and_undo_buttons_and_stats():
    from types import SimpleNamespace as NS

    async def run():
        s = storage.get_storage()
        s.save(datetime(2026, 11, 1), ["01.11.2026 10:00", 0.1, "Магазин 250", "Я", "Прочее", "хлеб", "", "", ""])
        bot, st = FakeBot(), B.get_state(1, 1)
        async with st.lock:
            await B.submit(bot, st, B.Item("text", text="Покупка 250"))
        await wait_for(lambda: st.current is not None)
        eid = st.current.eid
        await B.cb_hint(_call(f"h:{eid}"), bot)
        assert (st.current.category, st.current.sub, st.current.ref, st.current.stage) == ("Я", "Прочее", "хлеб", "comment")
        await B.cb_skip(_call(f"k:{eid}"), bot)          # без комментария -> запись
        assert st.current is None and len(s.month_records(datetime(2026, 11, 1))) == 2
        await B.cmd_stats(NS(text="/stats", chat=NS(id=1)), bot)
        assert "350 ₽" in bot.texts()[-1]
        await B.cb_undo(_call(f"u:{eid}"), bot)
        assert "Запись отменена" in bot.texts()[-1]
        assert len(s.month_records(datetime(2026, 11, 1))) == 1
        await B.cb_undo(_call(f"u:{eid}"), bot)          # второй раз — уже нечего отменять
        assert "нельзя отменить" in bot.texts()[-1]
    asyncio.run(run())
