@echo off
cd /d "%~dp0"
if exist "SAMSEL_ULTIMATE\desktop_app.py" (
  cd /d "%~dp0SAMSEL_ULTIMATE"
)
python desktop_app.py
if errorlevel 1 pause
