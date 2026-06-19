@echo off
cd /d "%~dp0"

REM Full SAMSEL Web lives under SAMSEL_ULTIMATE (or samsel_web). Running this .bat from
REM the parent SAMSEL-WEB-ENGINE folder would otherwise miss automix_routes / phone_url_print.
if exist "SAMSEL_ULTIMATE\server.py" if exist "SAMSEL_ULTIMATE\automix_routes.py" (
  cd /d "%~dp0SAMSEL_ULTIMATE"
) else if exist "samsel_web\server.py" if exist "samsel_web\automix_routes.py" (
  cd /d "%~dp0samsel_web"
)

REM HTTP port for uvicorn. Override: set SAMSEL_PORT=8766 in this file (before the next line) or in the environment.
if "%SAMSEL_PORT%"=="" set SAMSEL_PORT=8000

REM Phones on home Wi-Fi (private LAN). Remove to lock AutoMix to this PC only.
set SAMSEL_AUTOMIX_LAN=1
REM Cloudflare / internet phones: also set before py -m uvicorn:
set SAMSEL_AUTOMIX_ALLOW_REMOTE=1
set SAMSEL_AUTOMIX_NO_TOKEN=1
REM To require a token instead (recommended): comment the NO_TOKEN line above, uncomment below, and set a strong secret:
REM   set SAMSEL_AUTOMIX_TOKEN=your-long-random-secret
REM Split UI/API domains: set SAMSEL_CORS_ORIGINS=https://your.pages.dev

REM -- Jingle control --
REM Set to 0 to DISABLE user jingle uploads (lock to the default jingle below).
REM Set to 1 (or leave unset) to ALLOW users to pick their own jingle file.
set SAMSEL_JINGLE_UPLOADS=0
REM server.py tries each path in order (;). First hit wins. Uses cwd after cd above.
set "SAMSEL_JINGLE_PATH=%CD%\SAMSEL_AutoMix_Jingle_3.mp3;%USERPROFILE%\base\SAMSEL_WEB\SAMSEL-WEB-ENGINE\SAMSEL_AutoMix_Jingle_3.mp3"
REM Extra engine roots (;) - used if SAMSEL_JINGLE_PATH segments all miss (optional).
if not defined SAMSEL_WEB_ENGINE set "SAMSEL_WEB_ENGINE=%USERPROFILE%\base\SAMSEL_WEB\SAMSEL-WEB-ENGINE"
REM Log which jingle file loaded: set SAMSEL_JINGLE_LOG=1

REM After a deploy, verify: open https://your-domain/api/health - "web_build" must match
REM static/index.html <meta name="samsel-web-build"> and ?v= on CSS/JS. Bump all three next release.

py -3.10 -m pip install -r requirements.txt -q

echo.
echo ========== SAMSEL Web ==========
echo Custom domain (Option A): run tunnel_option_a_run.bat in a second window - see OPTION_A_TUNNEL_CHECKLIST.txt
echo This PC browser:  http://127.0.0.1:%SAMSEL_PORT%/
py -3.10 phone_url_print.py
echo.
echo If the phone shows "cannot connect" or times out:
echo   1) Same Wi-Fi as this PC   2) Use http:// not https://
echo   3) Run open_samsel_port.bat as Administrator (Windows Firewall - use the same SAMSEL_PORT)
echo.

start "" "http://127.0.0.1:%SAMSEL_PORT%/"
py -3.10 -m uvicorn server:app --host 0.0.0.0 --port %SAMSEL_PORT%
pause
