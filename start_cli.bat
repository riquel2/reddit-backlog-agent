@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Reddit Backlog Agent - CLI

REM Usage:
REM   start_cli.bat --demo
REM   start_cli.bat --subreddits SaaS,Notion --limit 300
REM   start_cli.bat --help
REM Double-click with no argument shows the help screen.

call "%~dp0_env.bat"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    "%VENV_PY%" main.py --help
    echo.
    echo ------------------------------------------------------------
    echo  Try:  start_cli.bat --demo
    echo        start_cli.bat --subreddits SaaS,Notion --limit 300
    echo ------------------------------------------------------------
    pause
    exit /b 0
)

"%VENV_PY%" main.py %*

if errorlevel 1 (
    echo.
    pause
)
