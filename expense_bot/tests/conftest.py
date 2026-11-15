"""Тесты запускаются без settings.txt и без сети: ключи берутся из переменных окружения."""
import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("STORAGE", "csv")
os.environ.setdefault("RECON_ENABLED", "no")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Каждый тест пишет свои файлы во временную папку, а не рядом с ботом."""
    monkeypatch.setattr(config, "CSV_FILE", tmp_path / "expenses.csv")
    monkeypatch.setattr(config, "FALLBACK_FILE", tmp_path / "unsaved_expenses.csv")
    monkeypatch.setattr(config, "IGNORED_FILE", tmp_path / "ignored_ops.json")
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    import storage
    monkeypatch.setattr(storage, "_storage", None)
    yield tmp_path
