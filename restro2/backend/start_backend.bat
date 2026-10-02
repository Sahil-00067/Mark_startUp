@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  MenuQR Payment Backend Launcher
echo ============================================

if not exist ".env" (
  echo [!] backend\.env not found.
  echo     Copy .env.example to .env and add your Razorpay keys, then run again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [*] First run: creating virtual environment...
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -m venv .venv
  ) else (
    python -m venv .venv
  )
  if not exist ".venv\Scripts\python.exe" (
    echo [X] Could not create .venv. Install Python 3.10+ from python.org and retry.
    pause
    exit /b 1
  )
)

rem Make sure dependencies are installed inside the venv
".venv\Scripts\python.exe" -c "import flask, flask_cors, razorpay, dotenv" >nul 2>nul
if not %errorlevel%==0 (
  echo [*] Installing dependencies into .venv ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo [*] Starting payment backend on http://127.0.0.1:5000
echo     Keep this window OPEN while taking online payments.
echo     Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" app.py

echo.
echo [!] Backend stopped.
pause
