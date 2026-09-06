@echo off
setlocal
title SentientSands - Dependency Installer
cd /d "%~dp0"

set "PYTHON="
set "PYTHON=%~dp0server\python\python.exe"

echo ============================================================
echo   SentientSands Unified Dependency Installer
echo ============================================================
echo.
if exist "%PYTHON%" (
    echo Using embedded Python: %PYTHON%
    echo.
    "%PYTHON%" "%~dp0INSTALL_DEPENDENCIES.py"
) else (
    echo Embedded Python not found. Falling back to py -3.
    echo.
    py -3 "%~dp0INSTALL_DEPENDENCIES.py"
)
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo Installer finished with errors. Exit code: %EXITCODE%
    pause
)
exit /b %EXITCODE%
