@echo off
cd /d "%~dp0"
if exist "SAMSEL_ULTIMATE\run_public.py" (
  cd /d "%~dp0SAMSEL_ULTIMATE"
)
python run_public.py
pause
