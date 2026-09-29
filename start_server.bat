@echo off
title "Cloud Upload and Video Streaming Server (PM2 Watchdog)"
cd /d "%~dp0"

echo =====================================================================
echo  Starting FastAPI Server with PM2-Style Auto-Refresh and Watchdog
echo =====================================================================
echo.

:: Detect Python
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set PY_CMD=python
) else (
    where py >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set PY_CMD=py
    ) else (
        echo [ERROR] Python not found in PATH! Please install Python 3.
        pause
        exit /b 1
    )
)

:: Run Supervisor
%PY_CMD% server_manager.py %*

pause
