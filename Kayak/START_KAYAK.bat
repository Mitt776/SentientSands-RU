@echo off
setlocal
title Kayak Server
echo =====================================
echo   Kayak v0.7 - Starting server...
echo =====================================
echo.
echo   NOTE: You do not normally need to run
echo   this. Kayak starts automatically when
echo   SentientSands starts.
echo.
echo   Only run this manually if something
echo   went wrong with the auto-start.
echo =====================================
echo.

cd /d "%~dp0"
set "PYTHON=%~dp0..\server\python\python.exe"

if exist "%PYTHON%" (
    "%PYTHON%" "%~dp0kayak_server.py"
) else (
    py -3 "%~dp0kayak_server.py"
)

echo.
echo =====================================
echo   Server stopped. Press any key...
echo =====================================
pause
