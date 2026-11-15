@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist settings.txt (
    copy settings.example.txt settings.txt >nul
    echo Создан файл settings.txt. Откройте его в Блокноте, впишите ключи и запустите start.bat снова.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установите его с сайта python.org и поставьте галочку "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist venv (
    echo Первый запуск: создаю рабочую папку venv...
    python -m venv venv
)

call venv\Scripts\activate.bat
echo Проверяю библиотеки (при первом запуске это займёт несколько минут)...

pip install -r requirements.txt --default-timeout=20 --retries=2
if not errorlevel 1 goto run

echo.
echo Основной сервер библиотек не отвечает. Пробую запасной адрес 1...
pip install -r requirements.txt --default-timeout=30 --retries=2 -i https://mirrors.aliyun.com/pypi/simple/
if not errorlevel 1 goto run

echo.
echo Пробую запасной адрес 2...
pip install -r requirements.txt --default-timeout=30 --retries=2 -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 goto run

echo.
echo Не удалось установить библиотеки. Проверьте интернет (и VPN, если он включён) и запустите файл ещё раз.
pause
exit /b 1

:run
python bot.py
echo.
echo Бот остановлен.
pause
