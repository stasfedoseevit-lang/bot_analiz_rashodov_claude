БОТ УЧЁТА ТРАТ (Analiz_trat_Claude) — СОСТАВ И РАБОТА НА СЕРВЕРЕ
==================================================================

ЧТО УМЕЕТ
  - Читает трату со скриншота (фото или картинка файлом), PDF-чека/справки или текста СМС.
  - Спрашивает категорию (Сестрорецк, Просвет, Лиговка, Парнас, Я, Работа) и пишет строку
    в Google Таблицу, на лист месяца ("November 2026" и т.д.).
  - После записи перечитывает строку: «Записано» только если она реально есть в таблице.
    Если нет — сохраняет трату в unsaved_expenses.csv и предупреждает.
  - Пропускает повторы: если такая же трата (сумма до рубля, время ±10 минут или тот же
    день, если времени нет) уже есть в таблице, пишет «Уже есть, пропускаю».
    Кнопка «Всё равно внести» — если это действительно вторая такая покупка.
  - По понедельникам в 15:00 просит выписки (PDF, Excel, CSV), сравнивает с таблицей и
    показывает список того, чего в таблице нет. Вы пишете номера — они вносятся.

КОМАНДЫ БОТА
  /check      — проверить связь с Claude и таблицей
  /reconcile  — запустить сверку с выписками вручную
  /cancel     — сбросить текущую трату, очередь и сверку
  /help       — подсказка

ФАЙЛЫ
  bot.py              — основной код бота (Telegram, вопросы, очередь, повторы, сверка)
  claude_parse.py     — чтение скриншотов, PDF, текста и выписок через Claude
  storage.py          — запись в Google Таблицу / CSV, проверка записи, чтение реестра
  recon.py            — сверка с выписками: сопоставление, Excel/CSV, «не нужно»
  categories.py       — список категорий и подкатегорий
  config.py           — чтение настроек из settings.txt
  settings.example.txt — ШАБЛОН настроек; на сервере его копия settings.txt с ключами
  requirements.txt    — нужные библиотеки Python
  expense-bot-claude.service — служба systemd (бот работает 24/7 и сам перезапускается)
  install_server.sh   — установка на сервер одной командой
  start.bat, README.txt — запуск для проверки на компьютере с Windows

ЧЕГО В АРХИВЕ НЕТ (секреты, лежат только на сервере в /root/expense_bot_test/)
  settings.txt с настоящими ключами (токен Telegram, ключ Claude, ваш ID)
  credentials.json — ключ служебного аккаунта Google для таблицы
  Без них бот не запустится. Как сохранить их копию — ниже, «Резервная копия».

СЛУЖЕБНЫЕ ФАЙЛЫ, КОТОРЫЕ БОТ СОЗДАЁТ САМ
  bot.log               — журнал работы
  ignored_ops.json      — операции из выписок, отмеченные «не нужно»
  unsaved_expenses.csv  — траты, которые не удалось записать в таблицу (если были)

УСТАНОВКА НА НОВЫЙ СЕРВЕР (Ubuntu)
  1. Скопируйте папку на сервер в /root/expense_bot_test/
     (на сервере: git clone АДРЕС_РЕПОЗИТОРИЯ /root/repo, затем
      cp -r /root/repo/expense_bot /root/expense_bot_test, если папки ещё нет).
     Если settings.txt на сервере ещё нет: cp settings.example.txt settings.txt и заполните.
  2. Положите туда settings.txt с ключами и credentials.json.
  3. На сервере: cd /root/expense_bot_test && bash install_server.sh

ОБНОВЛЕНИЕ КОДА
  Замените .py-файлы и выполните: systemctl restart expense-bot-claude
  settings.txt и credentials.json при этом не трогайте (в репозитории их нет,
  поэтому копирование новых файлов поверх их не затрёт).

ПОЛЕЗНЫЕ КОМАНДЫ (на сервере)
  systemctl status expense-bot-claude     — работает ли бот
  systemctl restart expense-bot-claude    — перезапуск
  tail -50 /root/expense_bot_test/bot.log — последние записи журнала

КЛЮЧ CLAUDE
  У ключа есть срок действия (виден в Claude Console, «Ключи и доступ»).
  Перед окончанием создайте новый и запишите его так (на сервере):
    read -s -p "Ключ: " K; echo; sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$K|" /root/expense_bot_test/settings.txt; unset K
    systemctl restart expense-bot-claude
  Затем /check в боте.

РЕЗЕРВНАЯ КОПИЯ ВСЕГО С СЕРВЕРА (вместе с ключами), из Termux:
  ssh root@АДРЕС "cd /root && tar czf - --exclude=venv --exclude=__pycache__ expense_bot_test" > ~/storage/downloads/expense_bot_backup.tar.gz
  Этот файл содержит секретные ключи — храните его у себя, никому не пересылайте.
