@echo off
REM ---------------------------------------------------------------
REM  Возврат англоязычных промптов Kayak.
REM  Копирует эталон из D:\Project VSC\_prompts_EN_backup во все
REM  четыре папки mandatory (Template и Campaigns\Default,
REM  в игровой копии мода и в проекте).
REM  Игру перед запуском закрыть - сервер держит файлы кампании.
REM ---------------------------------------------------------------
setlocal

set "SRC=D:\Project VSC\_prompts_EN_backup"
set "GAME=E:\SteamLibrary\steamapps\common\Kenshi\mods\SentientSands\Kayak\KayakDB"
set "PROJ=D:\Project VSC\3771003618\Kayak\KayakDB"

if not exist "%SRC%" (
    echo [!] Эталон не найден: %SRC%
    echo     Откат невозможен.
    pause
    exit /b 1
)

echo Возвращаю английские промпты...
echo.

for %%R in ("%GAME%" "%PROJ%") do (
    for %%S in ("Template" "Campaigns\Default") do (
        if exist "%%~R\%%~S\mandatory" (
            echo   -^> %%~R\%%~S\mandatory
            robocopy "%SRC%" "%%~R\%%~S\mandatory" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >nul
        )
    )
)

echo.
echo Готово. Русские версии лежат в истории этого проекта,
echo при необходимости их вернёт Claude.
echo Перезапусти игру, чтобы сервер перечитал промпты.
pause
