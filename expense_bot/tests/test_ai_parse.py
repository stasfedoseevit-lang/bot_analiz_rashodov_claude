import asyncio
import io
import json
from types import SimpleNamespace as NS

import pytest
from openai import BadRequestError

import ai_parse as ap


def test_parse_expense_json_keeps_kopecks():
    p = ap.parse_expense_json({"amount_rub": 2898.5, "merchant": "Пятёрочка", "bank": "Т-Банк",
                               "date": "15.11.2026 12:40", "operations": 1})
    assert p.amount == pytest.approx(2.8985)
    assert p.merchant == "Пятёрочка" and p.bank == "Т-Банк"
    assert p.date.strftime("%d.%m.%Y %H:%M") == "15.11.2026 12:40"


def test_parse_expense_json_tolerates_strings_and_gaps():
    p = ap.parse_expense_json({"amount_rub": "2 898,50 ₽", "merchant": "", "date": "", "operations": "x"})
    assert p.amount == pytest.approx(2.8985)
    assert p.merchant == "Банк / СМС" and p.date is None and p.ops == 1
    assert ap.parse_expense_json({}).amount == 0


def test_extract_json_with_surrounding_text():
    assert ap.extract_json('Вот ответ:\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        ap.extract_json("суммы нет")


def test_parse_statement_json_skips_bad_rows():
    r = ap.parse_statement_json({"bank": "Альфа-Банк", "operations": [
        {"date": "01.11.2026 10:00", "amount_rub": 150, "merchant": "Кофе"},
        {"date": "кривая дата", "amount_rub": 100, "merchant": "x"},
        {"date": "02.11.2026", "amount_rub": 0, "merchant": "ноль"},
        {"date": "03.11.2026", "amount_rub": "1 200,50", "merchant": "Аптека"},
    ]})
    assert [(o.merchant, o.rub, o.bank) for o in r.ops] == [("Кофе", 150.0, "Альфа-Банк"),
                                                           ("Аптека", 1200.5, "Альфа-Банк")]


def test_split_text_keeps_lines_whole_and_repeats_header():
    lines = ["Дата;Сумма;Описание"] + [f"0{i % 9 + 1}.11.2026;{i}00;Магазин {i}" for i in range(400)]
    chunks = ap.split_text("\n".join(lines), limit=2000)
    assert len(chunks) > 1
    assert all(c.startswith("Дата;Сумма;Описание") for c in chunks)
    body = [line for c in chunks for line in c.split("\n")[1:] if line.startswith("0")]
    assert len(set(body)) == 400                          # ни одна операция не потерялась


# ---------------------------------------------------------------- картинки и PDF

def _png(size, noise=False):
    from PIL import Image
    img = Image.effect_noise(size, 100).convert("RGB") if noise else Image.new("RGB", size, "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_image_passes_small_png_through():
    data = _png((100, 100))
    assert ap.prepare_image(data) == (data, "image/png")


def test_prepare_image_shrinks_big_files():
    data = _png((3000, 3000), noise=True)
    out, kind = ap.prepare_image(data)
    assert kind == "image/jpeg" and len(out) <= ap.MAX_IMAGE_BYTES


def test_prepare_image_unknown_format_gives_clear_error():
    with pytest.raises(ValueError, match="картинк"):
        ap.prepare_image(b"\x00\x00\x00 ftypheic" + b"\x00" * 100)


def text_pdf(lines):
    """Минимальный PDF с текстом (латиница, шрифт Helvetica)."""
    stream = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(f"({t}) '" for t in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


def scan_pdf():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(buf, format="PDF")
    return buf.getvalue()


def test_pdf_text_and_scan_detection():
    pages = ap.pdf_pages_text(text_pdf(["Payment 1500.00 RUB", "Shop: Coffee House", "15.11.2026 12:40"]))
    assert "Coffee House" in pages[0] and not ap.pdf_is_scan(pages)
    scan = scan_pdf()
    assert ap.pdf_is_scan(ap.pdf_pages_text(scan))
    images = ap.pdf_page_images(scan, 2)
    assert len(images) == 1 and images[0][:3] == b"\xff\xd8\xff"
    with pytest.raises(ValueError, match="PDF"):
        ap.pdf_pages_text(b"not a pdf")


# ---------------------------------------------------------------- запросы (поддельный клиент)

def _response(content, finish="stop"):
    return NS(choices=[NS(message=NS(content=content), finish_reason=finish)])


class FakeBadRequest(BadRequestError):
    def __init__(self, text):
        Exception.__init__(self, text)


class FakeClient:
    """Отвечает answers по очереди; reject — список (текст ошибки, условие на запрос)."""

    def __init__(self, answers=(), reject=()):
        self.answers, self.reject, self.calls = list(answers), list(reject), []
        self.chat = NS(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        for text, cond in self.reject:
            if cond(kwargs):
                raise FakeBadRequest(text)
        return self.answers.pop(0) if self.answers else _response("{}")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ap, "_fast_ok", True)
    monkeypatch.setattr(ap, "_json_ok", True)
    monkeypatch.setattr(ap.config, "FAST_MODE", True)

    def install(**kw):
        c = FakeClient(**kw)
        monkeypatch.setattr(ap, "_client", c)
        return c
    return install


def test_read_expense_from_image(client):
    c = client(answers=[_response(json.dumps({"amount_rub": 150, "merchant": "Магнит", "bank": "",
                                              "date": "16.10.2026 12:00", "operations": 1}))])
    p = asyncio.run(ap.read_expense(image_bytes=_png((50, 50))))
    assert p.amount == pytest.approx(0.15) and p.merchant == "Магнит"
    call = c.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    parts = call["messages"][0]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "json" in parts[1]["text"].lower()          # JSON-режим требует слово json в запросе


def test_read_expense_from_text_pdf_sends_text(client):
    c = client(answers=[_response('{"amount_rub": 1500, "merchant": "Coffee House", "bank": "", '
                                  '"date": "15.11.2026 12:40", "operations": 1}')])
    p = asyncio.run(ap.read_expense(pdf_bytes=text_pdf(["Payment 1500.00 RUB", "Shop: Coffee House", "Date: 15.11.2026 12:40"])))
    assert p.amount == pytest.approx(1.5)
    content = c.calls[0]["messages"][0]["content"]
    assert isinstance(content, str) and "Coffee House" in content


def test_read_expense_from_scan_pdf_sends_image(client):
    c = client(answers=[_response('{"amount_rub": 10, "merchant": "x", "bank": "", "date": "", "operations": 1}')])
    asyncio.run(ap.read_expense(pdf_bytes=scan_pdf()))
    parts = c.calls[0]["messages"][0]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_fast_mode_rejection_is_remembered(client):
    c = client(reject=[("unknown parameter: thinking", lambda k: "extra_body" in k)])
    asyncio.run(ap._create("json"))
    asyncio.run(ap._create("json"))
    assert len(c.calls) == 3          # 1 отказ + 1 повтор, второй запрос сразу без быстрого режима
    assert "extra_body" not in c.calls[-1] and "response_format" in c.calls[-1]


def test_unrelated_bad_request_does_not_disable_modes(client):
    c = client(reject=[("Invalid image", lambda k: True)])
    with pytest.raises(BadRequestError):
        asyncio.run(ap._create("json"))
    assert ap._fast_ok and ap._json_ok and len(c.calls) == 1


def test_read_statement_in_chunks(client, monkeypatch):
    monkeypatch.setattr(ap, "STATEMENT_CHUNK_CHARS", 300)
    answers = [
        _response('{"bank": "Альфа-Банк", "operations": [{"date": "01.11.2026 10:00", "amount_rub": 100, "merchant": "A"}]}'),
        _response('{"bank": "", "operations": [{"date": "02.11.2026 11:00", "amount_rub": 200, "merchant": "B"}]}'),
    ]
    c = client(answers=answers + [_response('{"bank": "", "operations": []}')] * 10)
    text = "\n".join(f"0{i % 9 + 1}.11.2026;{i}00;Магазин {i}" for i in range(30))
    r = asyncio.run(ap.read_statement(text=text))
    assert len(c.calls) > 1 and not r.error
    assert [(o.merchant, o.bank) for o in r.ops] == [("A", "Альфа-Банк"), ("B", "Альфа-Банк")]


def test_read_statement_truncated_answer_is_reported(client):
    client(answers=[_response('{"bank": "x", "operations": [', finish="length")])
    r = asyncio.run(ap.read_statement(text="01.11.2026;100;A"))
    assert r.truncated and "по частям" in r.error


def test_friendly_errors():
    class Err(Exception):
        def __init__(self, status, text=""):
            super().__init__(text)
            self.status_code = status
    assert "баланс" in ap.friendly_error(Err(402, "Insufficient Balance"))
    assert "ключ" in ap.friendly_error(Err(401))
    assert "DEEPSEEK_MODEL" in ap.friendly_error(Err(404))
