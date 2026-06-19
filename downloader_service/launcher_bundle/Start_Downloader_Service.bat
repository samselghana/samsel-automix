@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment not found in:
  echo %CD%\.venv
  echo.
  echo Create it first with:
  echo   python -m venv .venv
  echo   .venv\Scripts\activate
  echo   pip install -r requirements.txt
  pause
  exit /b 1
)

if not exist "app\main.py" (
  echo [ERROR] app\main.py not found.
  echo Copy this launcher into your downloader_service folder.
  pause
  exit /b 1
)

echo Starting Downloader Service...
start "Downloader Service API" cmd /k ".venv\Scripts\activate.bat && python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload"

timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8010/health

exit /b 0
