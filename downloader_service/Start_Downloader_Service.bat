@echo off
cd /d %~dp0

REM Activate virtual environment
call .venv\Scripts\activate

REM Start server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload

pause