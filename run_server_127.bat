@echo off
cd /d "%~dp0"
echo SAMSEL DJ Engine — binding to 127.0.0.1:8000
echo Open: http://127.0.0.1:8000/
echo If deck uploads showed "Failed to fetch" while using localhost, use this URL.
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --timeout-keep-alive 300
if errorlevel 1 pause
