@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist "%~dp0server\python\python.exe" (
    set "PYTHON=%~dp0server\python\python.exe"
) else if exist "%~dp0server\python\Scripts\python.exe" (
    set "PYTHON=%~dp0server\python\Scripts\python.exe"
)

echo ============================================================
echo   Applying Sentient Sands master config
echo ============================================================
echo Using Python: %PYTHON%
echo.

"%PYTHON%" "%~dp0apply_config.py" %*

echo.
pause
