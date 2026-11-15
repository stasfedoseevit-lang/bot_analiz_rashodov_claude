"""Настройки бота. Все значения берутся из файла settings.txt."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.txt"
# Значения можно задать и переменными окружения (так запускаются тесты); иначе нужен settings.txt
if not SETTINGS_FILE.exists() and not os.getenv("TELEGRAM_TOKEN"):
    raise SystemExit(
        "Нет файла settings.txt. Скопируйте settings.example.txt в settings.txt "
        "и впишите в него ключи."
    )
load_dotenv(SETTINGS_FILE)


def _need(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("СЮДА"):
        raise SystemExit(f"В файле settings.txt не заполнено поле {name}. Откройте файл в Блокноте и впишите значение.")
    return value


TELEGRAM_TOKEN = _need("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = _need("ANTHROPIC_API_KEY")

try:
    ALLOWED_USER_IDS = {
        int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
    }
except ValueError:
    raise SystemExit("В settings.txt поле ALLOWED_USER_IDS должно содержать числа через запятую.")
if not ALLOWED_USER_IDS:
    raise SystemExit("В settings.txt не заполнено поле ALLOWED_USER_IDS (ваш Telegram ID).")

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5").strip()
STORAGE = os.getenv("STORAGE", "csv").strip().lower()          # csv или sheets
SHEET_NAME = os.getenv("SHEET_NAME", "Analiz_raskhodov").strip()
GOOGLE_CREDENTIALS_FILE = BASE_DIR / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json").strip()
CSV_FILE = BASE_DIR / os.getenv("CSV_FILE", "expenses_test.csv").strip()
FALLBACK_FILE = BASE_DIR / "unsaved_expenses.csv"
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow").strip()
# Ходить к Telegram только по IPv4 (помогает, если на Windows подключение виснет)
FORCE_IPV4 = os.getenv("FORCE_IPV4", "yes").strip().lower() in ("1", "yes", "true", "да")
# Быстрый режим Claude: без «обдумывания» и с минимальным усилием (для чтения чека хватает)
FAST_MODE = os.getenv("FAST_MODE", "yes").strip().lower() in ("1", "yes", "true", "да")

# Еженедельная сверка с выписками: напоминание в выбранный день недели и час (0 = понедельник)
RECON_ENABLED = os.getenv("RECON_ENABLED", "yes").strip().lower() in ("1", "yes", "true", "да")
try:
    RECON_WEEKDAY = int(os.getenv("RECON_WEEKDAY", "0"))
    RECON_HOUR = int(os.getenv("RECON_HOUR", "15"))
except ValueError:
    raise SystemExit("В settings.txt поля RECON_WEEKDAY и RECON_HOUR должны быть числами.")
IGNORED_FILE = BASE_DIR / "ignored_ops.json"
STATE_FILE = BASE_DIR / "state.json"      # незаконченная трата, очередь и сверка (между перезапусками)

if STORAGE not in ("csv", "sheets"):
    raise SystemExit("В settings.txt поле STORAGE должно быть csv или sheets.")
