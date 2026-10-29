@echo off
title NetMon Process Check
setlocal

rem Locate Python 3: try the py launcher first, then python on PATH.
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
    python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3 not found.
    echo         Install it from https://www.python.org/downloads/ and retry.
    pause
    exit /b 1
)

%PY% "%~dp0proc_check.py"

echo.
pause
