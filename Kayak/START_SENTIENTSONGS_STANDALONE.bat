@echo off
title SentientSongs - Standalone Console

set PYTHON=
if exist "%~dp0..\server\python\python.exe" (
    set PYTHON=%~dp0..\server\python\python.exe
) else if exist "%~dp0..\server\python\Scripts\python.exe" (
    set PYTHON=%~dp0..\server\python\Scripts\python.exe
) else (
    set PYTHON=python
)

"%PYTHON%" "%~dp0addons\SentientSongs\StandalonePlay.py"
pause
