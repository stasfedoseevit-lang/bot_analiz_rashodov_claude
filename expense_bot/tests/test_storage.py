from datetime import datetime

import pytest

import config
import storage


def row(date, thousands, merchant, cat="Я", sub="Транспорт", ref="—"):
    return [date, thousands, merchant, cat, sub, ref, "", "Скриншот", ""]


def test_parse_registry_row():
    r = storage.parse_registry_row(["15.11.2026 12:40", "2,8985", "Пятёрочка", "Я", "Прочее", "хлеб"])
    assert r.date == datetime(2026, 11, 15, 12, 40) and r.rub == pytest.approx(2898.5)
    assert (r.merchant, r.category, r.sub, r.ref) == ("Пятёрочка", "Я", "Прочее", "хлеб")
    assert storage.parse_registry_row(storage.HEADER) is None


def test_normalize_merchant():
    assert storage.normalize_merchant("ПЯТЁРОЧКА 1234 СПб") == storage.normalize_merchant("Пятерочка 5678 спб")


def test_csv_save_read_delete_hints_and_stats():
    s = storage.CsvStorage(config.CSV_FILE)
    d = datetime(2026, 11, 15, 12, 0)
    s.save(d, row("15.11.2026 12:00", 0.15, "Метро 1", sub="Транспорт"))
    s.save(d, row("15.11.2026 13:00", 2.8985, "Пятёрочка 12", sub="Прочее", ref="продукты"))
    s.save(d, row("15.11.2026 14:00", 0.2, "Метро 2", cat="Работа", sub="—", ref="Объект А"))
    got = s.rows_between(datetime(2026, 11, 1), datetime(2026, 11, 30))
    assert [r.rub for r in got] == pytest.approx([150, 2898.5, 200])
    hints = s.category_hints(datetime(2026, 11, 16))
    assert hints["метро"] == ("Работа", "—", "Объект А")          # берётся последняя трата
    assert hints["пятерочка"] == ("Я", "Прочее", "продукты")
    assert len(s.month_records(datetime(2026, 11, 1))) == 3
    assert s.delete("csv", row("15.11.2026 13:00", 2.8985, "Пятёрочка 12", sub="Прочее", ref="продукты"))
    assert len(s.month_records(datetime(2026, 11, 1))) == 2
    assert not s.delete("csv", row("15.11.2026 13:00", 2.8985, "нет такой"))


def test_flush_fallback_moves_rows_and_skips_existing():
    target = storage.CsvStorage(config.CSV_FILE)
    d = datetime(2026, 11, 15, 12, 0)
    target.save(d, row("15.11.2026 12:00", 0.15, "Уже есть"))
    storage.save_fallback(d, row("15.11.2026 12:00", 0.15, "Уже есть"))
    storage.save_fallback(d, row("15.11.2026 12:30", 1.5, "Новая"))
    assert storage.flush_fallback(target) == (2, 0)
    assert not config.FALLBACK_FILE.exists()
    merchants = [r.merchant for r in target.rows_between(datetime(2026, 11, 1), datetime(2026, 11, 30))]
    assert merchants == ["Уже есть", "Новая"]
    assert isinstance(storage.parse_amount_cell("1,5"), float)


def test_flush_fallback_keeps_rows_when_table_is_down():
    class Down:
        def rows_between(self, *a, **k):
            raise OSError("нет связи")
    storage.save_fallback(datetime(2026, 11, 15), row("15.11.2026 12:30", 1.5, "Новая"))
    assert storage.flush_fallback(Down()) == (0, 1)
    assert config.FALLBACK_FILE.exists()
