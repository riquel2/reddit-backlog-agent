@echo off
REM ============================================================
REM  Shared bootstrap. Finds or creates .venv and exports VENV_PY.
REM  Must NOT use setlocal, so variables survive back to caller.
REM  Return code 0 = ready, 1 = failed.
REM ============================================================

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import streamlit, praw, pandas, openpyxl, openai" >nul 2>&1
    if not errorlevel 1 exit /b 0
    echo [1/2] venv found, installing missing packages...
    "%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 exit /b 1
    exit /b 0
)

REM any system python that already has the dependencies?
set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3" "python" "python3") do (
    if not defined PY (
        %%~C -c "import streamlit, praw, pandas, openpyxl, openai" >nul 2>&1
        if not errorlevel 1 set "PY=%%~C"
    )
)
if defined PY (
    echo [info] using system python: %PY%
    set "VENV_PY=%PY%"
    exit /b 0
)

REM build a project-local venv; first launch only
echo [1/2] No python with dependencies found. Creating .venv
echo        First launch only, takes 1-3 minutes. Please wait...
set "BASEPY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3" "python") do (
    if not defined BASEPY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
        if not errorlevel 1 set "BASEPY=%%~C"
    )
)
if not defined BASEPY (
    echo.
    echo [ERROR] Python 3.10+ not found.
    echo         Install it from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH".
    exit /b 1
)

%BASEPY% -m venv "%~dp0.venv"
if errorlevel 1 exit /b 1

echo [2/2] Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. Check your network, or run:
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)
exit /b 0
