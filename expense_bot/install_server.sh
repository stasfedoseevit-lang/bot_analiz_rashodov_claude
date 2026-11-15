#!/bin/bash
# Установка или переустановка бота на сервере (Ubuntu). Запуск: bash install_server.sh
# Ничего не удаляет: settings.txt и credentials.json остаются как есть.
set -e
DIR=/root/expense_bot_test
cd "$DIR"

if ! python3 -c "import ensurepip" 2>/dev/null; then
  echo "Ставлю python3-venv..."
  apt update && apt install -y python3-venv
fi

[ -d venv ] || python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
# HEIC-фото с iPhone (необязательно: без этой библиотеки бот попросит прислать PNG или JPEG)
venv/bin/pip install "pillow-heif>=0.16" || echo "pillow-heif не установился, HEIC-фото читаться не будут."

[ -f settings.txt ] || { echo "Нет settings.txt: заполните его и запустите скрипт снова."; exit 1; }
chmod 600 settings.txt
[ -f credentials.json ] && chmod 600 credentials.json || echo "Внимание: нет credentials.json (нужен для Google Таблицы)."

cp expense-bot-claude.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable expense-bot-claude
systemctl restart expense-bot-claude
sleep 3
systemctl --no-pager status expense-bot-claude | head -5
echo "Готово. Проверьте бота командой /check в Telegram."
