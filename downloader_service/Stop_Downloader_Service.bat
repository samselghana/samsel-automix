@echo off
echo Stopping Downloader Service...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8010 ^| findstr LISTENING') do (
  taskkill /F /PID %%a >nul 2>&1
)
taskkill /F /FI "WINDOWTITLE eq Downloader Service API" >nul 2>&1
echo Done.
timeout /t 2 >nul
