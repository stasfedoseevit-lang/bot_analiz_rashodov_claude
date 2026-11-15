import asyncio
import io

import pytest
from anthropic import BadRequestError

import claude_parse as cp


def test_parse_expense_json_keeps_kopecks():
    p = cp.parse_expense_json({"amount_rub": 2898.5, "merchant": "Пятёрочка", "bank": "Т-Банк",
                               "date": "15.11.2026 12:40", "operations": 1})
    assert p.amount == pytest.approx(2.8985)
    assert round(p.amount * 1000, 2) == 2898.5
    assert p.merchant == "Пятёрочка" and p.bank == "Т-Банк"
    assert p.date.strftime("%d.%m.%Y %H:%M") == "15.11.2026 12:40"


def test_parse_expense_json_tolerates_strings_and_gaps():
    p = cp.parse_expense_json({"amount_rub": "2 898,50 ₽", "merchant": "", "bank": "", "date": "", "operations": 0})
    assert p.amount == pytest.approx(2.8985)
    assert p.merchant == "Банк / СМС" and p.date is None and p.ops == 1
    assert cp.parse_expense_json({"amount_rub": 0}).amount == 0


def test_extract_json_with_surrounding_text():
    assert cp.extract_json('Вот ответ:\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        cp.extract_json("суммы нет")


def test_parse_statement_json_skips_bad_rows():
    r = cp.parse_statement_json({"bank": "Альфа-Банк", "operations": [
        {"date": "01.11.2026 10:00", "amount_rub": 150, "merchant": "Кофе"},
        {"date": "кривая дата", "amount_rub": 100, "merchant": "x"},
        {"date": "02.11.2026", "amount_rub": 0, "merchant": "ноль"},
        {"date": "03.11.2026", "amount_rub": "1 200,50", "merchant": "Аптека"},
    ]})
    assert [(o.merchant, o.rub, o.bank) for o in r.ops] == [("Кофе", 150.0, "Альфа-Банк"),
                                                           ("Аптека", 1200.5, "Альфа-Банк")]


def _png(size, noise=False):
    from PIL import Image
    img = Image.effect_noise(size, 100).convert("RGB") if noise else Image.new("RGB", size, "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_image_passes_small_png_through():
    data = _png((100, 100))
    assert cp.prepare_image(data) == (data, "image/png")


def test_prepare_image_shrinks_big_files():
    data = _png((3000, 3000), noise=True)
    assert len(data) > cp.MAX_IMAGE_BYTES
    out, kind = cp.prepare_image(data)
    assert kind == "image/jpeg" and len(out) <= cp.MAX_IMAGE_BYTES


def test_prepare_image_unknown_format_gives_clear_error():
    with pytest.raises(ValueError, match="картинк"):
        cp.prepare_image(b"\x00\x00\x00 ftypheic" + b"\x00" * 100)


# ---------------------------------------------------------------- откат параметров запроса

class FakeBadRequest(BadRequestError):
    """Ошибка 400 без настоящего HTTP-ответа (str(e) = текст ошибки)."""

    def __init__(self, text):
        Exception.__init__(self, text)


def _bad_request(text):
    return FakeBadRequest(text)


class FakeClient:
    def __init__(self, reject):
        self.reject, self.calls = reject, []
        self.messages = self

    async def create(self, **kwargs):
        body = kwargs["extra_body"]
        self.calls.append(body)
        for word, present in self.reject:
            if present(body):
                raise _bad_request(word)

        class R:
            content = []
        return R()


@pytest.fixture
def fresh_flags(monkeypatch):
    monkeypatch.setattr(cp, "_fast_ok", True)
    monkeypatch.setattr(cp, "_schema_ok", True)
    monkeypatch.setattr(cp.config, "FAST_MODE", True)


def test_fast_mode_rejection_is_remembered(fresh_flags, monkeypatch):
    client = FakeClient([("thinking: disabled is not supported", lambda b: "thinking" in b)])
    monkeypatch.setattr(cp, "_client", client)
    asyncio.run(cp._create([], cp.EXPENSE_SCHEMA))
    asyncio.run(cp._create([], cp.EXPENSE_SCHEMA))
    assert len(client.calls) == 3          # 1 отказ + 1 повтор, второй чек сразу без быстрого режима
    assert "thinking" not in client.calls[-1] and "format" in client.calls[-1]["output_config"]


def test_unrelated_bad_request_does_not_disable_modes(fresh_flags, monkeypatch):
    client = FakeClient([("Could not process image", lambda b: True)])
    monkeypatch.setattr(cp, "_client", client)
    with pytest.raises(BadRequestError):
        asyncio.run(cp._create([], cp.EXPENSE_SCHEMA))
    assert cp._fast_ok and cp._schema_ok and len(client.calls) == 1
