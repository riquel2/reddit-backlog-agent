@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Reddit Backlog Agent - Web UI

echo ============================================
echo   Reddit Backlog Agent - Web UI
echo   Press Ctrl+C in this window to stop
echo ============================================
echo.

REM ---------- suppress the first-run email prompt ----------
set "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false"
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    > "%USERPROFILE%\.streamlit\credentials.toml" (
        echo [general]
        echo email = ""
    )
)

REM ---------- python + dependencies ----------
call "%~dp0_env.bat"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)

REM ---------- pick a free port from 8501 ----------
set "PORT="
for %%P in (8501 8502 8503 8504 8505) do (
    if not defined PORT (
        netstat -ano | findstr /C:":%%P" | findstr /I "LISTENING" >nul 2>&1
        if errorlevel 1 set "PORT=%%P"
    )
)
if not defined PORT set "PORT=8501"

echo.
echo   Open in browser: http://localhost:%PORT%
echo.

"%VENV_PY%" -m streamlit run app.py --server.port %PORT%

echo.
echo Server stopped.
pause
