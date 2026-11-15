import json
from datetime import datetime

import config
import recon
from ai_parse import StatementOp


def op(d, rub, merchant="x"):
    return StatementOp(datetime.strptime(d, "%d.%m.%Y %H:%M"), rub, merchant)


def test_op_key_ignores_merchant_spelling():
    assert recon.op_key(op("01.11.2026 10:00", 150, "OZON")) == recon.op_key(op("01.11.2026 10:00", 150, "Ozon.ru"))


def test_old_ignored_keys_are_migrated():
    config.IGNORED_FILE.write_text(json.dumps(["01.11.2026 10:00|150.00|OZON"]), encoding="utf-8")
    assert recon.op_key(op("01.11.2026 10:00", 150, "Ozon.ru")) in recon.load_ignored()


def test_add_ignored_prunes_old_keys():
    recon.add_ignored(["01.01.2025 10:00|1.00", "01.11.2026 10:00|2.00"], now=datetime(2026, 11, 15))
    assert recon.load_ignored() == {"01.11.2026 10:00|2.00"}


def test_find_missing_matches_each_row_once():
    ops = [op("01.11.2026 10:00", 150), op("01.11.2026 11:00", 150), op("05.11.2026 10:00", 999)]
    registry = [(datetime(2026, 11, 1, 10, 5), 150.4, "Кофе")]
    missing, matched = recon.find_missing(ops, registry)
    assert matched == 1 and [o.rub for o in missing] == [150, 999]


def test_parse_picks():
    assert recon.parse_picks("1,3, 5-6", 6) == [0, 2, 4, 5]
    assert recon.parse_picks("все", 3) == [0, 1, 2]
    assert recon.parse_picks("нет", 3) == []
    assert recon.parse_picks("7", 3) is None
