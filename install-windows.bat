@echo off
rem Установщик sabz-notifier на Windows (локально). Запускать обычным двойным
rem кликом или из cmd. Права администратора не нужны.
chcp 65001 >nul
setlocal EnableDelayedExpansion

set "TASK=SabzNotifierBot"
rem каталог намеренно НЕ в AppData\Local: оттуда Планировщик задач не стартует
set "DIR=%USERPROFILE%\SabzNotifierBot"
set "REPO=sabz04/sabz-notifier"
set "BRANCH=vps"
set "RAW=https://raw.githubusercontent.com/%REPO%/%BRANCH%"
set "VENVPY=%DIR%\venv\Scripts\python.exe"
set "VENVPYW=%DIR%\venv\Scripts\pythonw.exe"
set "PWPATH=%DIR%\ms-playwright"

if /i "%~1"=="uninstall" goto :uninstall
if /i "%~1"=="purge"     goto :purge

echo.
echo   sabz-notifier — уведомления Avito и ВК в Telegram
echo   ------------------------------------------------
echo.

where curl.exe >nul 2>&1
if errorlevel 1 (
  echo   [x] Нет curl. Нужна Windows 10 версии 1803 или новее.
  goto :fail
)

set "PY="
for /f "delims=" %%i in ('where python.exe 2^>nul') do (
  if not defined PY set "PY=%%i"
)
if not defined PY (
  echo   [x] Не нашёл python. Поставь с python.org и отметь "Add to PATH".
  goto :fail
)
echo   [+] python: %PY%

rem ---------------------------------------------------------------- вводные
set "TOKEN="
set "OWNER="
set "RECIPIENT="
if exist "%DIR%\.env" (
  for /f "usebackq tokens=1,* delims==" %%a in ("%DIR%\.env") do (
    if /i "%%a"=="TG_TOKEN"     set "TOKEN=%%b"
    if /i "%%a"=="TG_OWNER"     set "OWNER=%%b"
    if /i "%%a"=="TG_RECIPIENT" set "RECIPIENT=%%b"
  )
  if defined TOKEN echo   [+] настройки взяты из прошлой установки
)

if not defined TOKEN (
  echo.
  set /p "TOKEN=  Токен бота от @BotFather: "
)
if not defined TOKEN goto :notoken

if not defined OWNER (
  echo.
  echo   Владелец — ТВОЙ аккаунт в Telegram, а не имя бота.
  set /p "OWNER=  Твой @username или числовой id: "
)
if not defined OWNER goto :noowner
if "!OWNER:~0,1!"=="@" set "OWNER=!OWNER:~1!"

if not defined RECIPIENT (
  set /p "RECIPIENT=  Кому слать уведомления (Enter — себе): "
)
if not defined RECIPIENT set "RECIPIENT=@!OWNER!"

echo.
echo   владелец:   !OWNER!
echo   получатель: !RECIPIENT!
echo   каталог:    %DIR%
echo.

rem --------------------------------------------------------- прежний запуск
schtasks /query /tn "%TASK%" >nul 2>&1
if not errorlevel 1 (
  echo   [*] Останавливаю прежний экземпляр...
  schtasks /end /tn "%TASK%" >nul 2>&1
  taskkill /f /im pythonw.exe >nul 2>&1
)

rem ------------------------------------------------------------------ файлы
if not exist "%DIR%\data" mkdir "%DIR%\data" >nul 2>&1
echo   [*] Качаю bot.py и run.pyw...
curl -fsSL "%RAW%/bot.py"  -o "%DIR%\bot.py"  || goto :dlfail
curl -fsSL "%RAW%/run.pyw" -o "%DIR%\run.pyw" || goto :dlfail
echo   [+] исходники на месте

rem ---------------------------------------------------------------- браузер
if not exist "%VENVPY%" (
  echo   [*] Создаю окружение...
  "%PY%" -m venv "%DIR%\venv" || goto :venvfail
)
"%VENVPY%" -c "import playwright" >nul 2>&1
if errorlevel 1 (
  echo   [*] Ставлю playwright, это займёт минуту...
  "%VENVPY%" -m pip install --quiet --upgrade pip >nul 2>&1
  "%VENVPY%" -m pip install playwright || goto :pwfail
)
echo   [+] playwright готов

set "HAVECHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "HAVECHROME=1"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "HAVECHROME=1"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "HAVECHROME=1"
if defined HAVECHROME (
  echo   [+] нашёл Google Chrome — использую его
) else (
  echo   [*] Chrome не найден, качаю chromium, это несколько минут...
  set "PLAYWRIGHT_BROWSERS_PATH=%PWPATH%"
  "%VENVPY%" -m playwright install chromium || goto :pwfail
  echo   [+] chromium установлен
)

rem ------------------------------------------------------------------- .env
> "%DIR%\.env" echo TG_TOKEN=!TOKEN!
>>"%DIR%\.env" echo TG_OWNER=!OWNER!
>>"%DIR%\.env" echo TG_RECIPIENT=!RECIPIENT!
>>"%DIR%\.env" echo BOT_DATA=%DIR%\data
if not defined HAVECHROME >>"%DIR%\.env" echo PLAYWRIGHT_BROWSERS_PATH=%PWPATH%
>>"%DIR%\.env" echo PYTHONIOENCODING=utf-8
echo   [+] настройки записаны

rem ----------------------------------------------------------- самопроверка
echo.
echo   Самопроверка:
"%VENVPY%" "%DIR%\bot.py" --selfcheck
echo.

rem ------------------------------------------------------------- автозапуск
schtasks /create /tn "%TASK%" /tr "'%VENVPYW%' '%DIR%\run.pyw'" /sc onlogon /rl limited /f >nul
if errorlevel 1 (
  echo   [!] Не удалось создать задачу автозапуска.
) else (
  echo   [+] автозапуск при входе в систему настроен
)
schtasks /run /tn "%TASK%" >nul 2>&1
echo   [+] бот запущен

echo.
echo   ГОТОВО.
echo.
echo   Осталось два шага:
echo     1. Напиши боту в Telegram /start
echo     2. Через полминуты откроется окно браузера с вкладками
echo        Авито и ВК — залогинься в них. Это нужно один раз.
echo.
echo   Закроешь окно — бот откроет его снова.
echo   Логи: %DIR%\bot.log
echo   Настройки меняются прямо в Telegram: /settings
echo.
echo   Один токен — один бот. Если он крутится ещё где-то,
echo   останови там, иначе Telegram будет отдавать 409 обоим.
echo.
echo   Удалить:  %~nx0 uninstall     (данные останутся)
echo   Снести:   %~nx0 purge         (вместе с данными)
echo.
pause
exit /b 0

:uninstall
echo.
echo   Удаляю %TASK%...
schtasks /end    /tn "%TASK%" >nul 2>&1
schtasks /delete /tn "%TASK%" /f >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
del /q "%DIR%\bot.py" "%DIR%\run.pyw" "%DIR%\.env" >nul 2>&1
echo   [+] бот остановлен и снят с автозапуска
echo   [+] данные сохранены в %DIR%\data
echo.
pause
exit /b 0

:purge
echo.
echo   Сношу %TASK% подчистую...
schtasks /end    /tn "%TASK%" >nul 2>&1
schtasks /delete /tn "%TASK%" /f >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
rmdir /s /q "%DIR%" >nul 2>&1
echo   [+] удалено полностью, вместе с данными и браузером
echo.
pause
exit /b 0

:notoken
echo   [x] Токен не введён.
goto :fail
:noowner
echo   [x] Владелец не указан.
goto :fail
:dlfail
echo   [x] Не скачались исходники. Проверь интернет.
goto :fail
:venvfail
echo   [x] Не удалось создать окружение python.
goto :fail
:pwfail
echo   [x] Не удалось поставить браузер.
goto :fail
:fail
echo.
pause
exit /b 1
