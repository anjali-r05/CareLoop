@echo off
setlocal
cd /d "%~dp0"
title CareLoop AI V21 launcher
set PORT=5002

if not exist .venv\Scripts\python.exe (
  echo [1/4] Creating virtual environment...
  py -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 (
    echo Could not create Python virtual environment. Install Python 3.10+ and retry.
    pause
    exit /b 1
  )
)

if not exist .env copy .env.example .env >nul

echo [2/4] Installing required packages...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Package installation failed. Check your internet connection and try again.
  echo No browser has been opened because the server cannot start without dependencies.
  pause
  exit /b 1
)

echo [3/4] Starting CareLoop AI...
start "CareLoop AI server" /min "%~dp0.venv\Scripts\python.exe" "%~dp0app.py"

echo [4/4] Waiting for the server to respond...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i -lt 30;$i++){ try {$r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5002/api/status -TimeoutSec 1; if($r.StatusCode -eq 200){$ok=$true;break}} catch {}; Start-Sleep -Seconds 1 }; if($ok){Start-Process 'http://127.0.0.1:5002/'} else {Write-Host 'Server did not respond. Check the CareLoop AI server window.'; exit 1}"
if errorlevel 1 (
  echo Check the server window for the error message.
  pause
)
endlocal
