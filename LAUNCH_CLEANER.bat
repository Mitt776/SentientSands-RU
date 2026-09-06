@echo off
setlocal
set ROOT=%~dp0

if exist "%ROOT%server\python\python.exe" (
    "%ROOT%server\python\python.exe" "%ROOT%LAUNCH_CLEANER.py"
) else (
    py -3 "%ROOT%LAUNCH_CLEANER.py"
)

echo.
pause
