@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found: .venv\Scripts\python.exe
    echo Create it with: py -3 -m venv .venv
    echo Then install dependencies with: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

REM Load optional .env values without replacing variables already set in the shell.
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if not "%%~A"=="" if not defined %%A set "%%A=%%B"
    )
)

if not defined APP_HOST set "APP_HOST=127.0.0.1"
if not defined APP_PORT set "APP_PORT=8000"

if /I "%APP_HOST%"=="127.0.0.1" goto host_ok
if /I "%APP_HOST%"=="localhost" goto host_ok
if /I "%APP_HOST%"=="::1" goto host_ok
echo [ERROR] APP_HOST must be a loopback address: 127.0.0.1, localhost, or ::1.
echo This local MVP has no login system and must not be exposed to non-loopback interfaces.
pause
exit /b 1
:host_ok

netstat -ano | findstr ":%APP_PORT% " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [ERROR] Port %APP_PORT% is already listening. Stop the existing process or set APP_PORT before retrying.
    pause
    exit /b 1
)

echo Personal AI OS MVP starting at http://%APP_HOST%:%APP_PORT% ...
echo Keep this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host "%APP_HOST%" --port "%APP_PORT%"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [STOPPED] server exited with code %EXIT_CODE%
pause
exit /b %EXIT_CODE%
