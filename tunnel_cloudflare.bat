@echo off
cd /d "%~dp0"
if "%SAMSEL_PORT%"=="" set "SAMSEL_PORT=8765"
echo.
echo  THIS FILE = QUICK tunnel only (random *.trycloudflare.com URL each run).
echo  For your own domain (e.g. samsel-automix.com), use Option A instead:
echo    - Read OPTION_A_TUNNEL_CHECKLIST.txt
echo    - Run tunnel_option_a_run.bat  (after Zero Trust tunnel + Public Hostname)
echo.
echo  Quick tunnel -^> http://127.0.0.1:%SAMSEL_PORT%
echo  Start origin first: run_web.bat / uvicorn, or DJ desktop_app.py -p %SAMSEL_PORT%
echo  Use the same SAMSEL_PORT as run_web.bat if not 8765.
echo.
echo  If you see "502 Bad Gateway / Unable to reach the origin service":
echo    - Origin is whatever URL this script uses below. cloudflared must reach it.
echo    1) run_web.bat must be running ^(uvicorn listening^).
echo    2) Same port: set SAMSEL_PORT before this bat if not 8765.
echo    3) Test in a browser: http://127.0.0.1:%SAMSEL_PORT%/api/health  ^(JSON ok^).
echo    4) Use 127.0.0.1 not "localhost" in tunnel URLs ^(avoids IPv6 ::1 vs IPv4 issues^).
echo    5) Public Hostname ^(Option A^): Type HTTP, URL http://127.0.0.1:PORT  ^(not https://^).
echo.

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo [ERROR] cloudflared not in PATH.
  echo   winget install Cloudflare.cloudflared
  echo   https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/
  pause
  exit /b 1
)

powershell -NoProfile -Command "try { $u = 'http://127.0.0.1:%SAMSEL_PORT%/api/health'; $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 4; if ($r.StatusCode -eq 200) { exit 0 } } catch { }; exit 1" >nul 2>&1
if errorlevel 1 (
  echo [WARN] No HTTP 200 from http://127.0.0.1:%SAMSEL_PORT%/api/health
  echo        Fix that first, or the tunnel will return 502. Start run_web.bat / desktop_app.py -p or fix SAMSEL_PORT.
  echo.
  pause
)

echo Starting quick tunnel...
cloudflared tunnel --url http://127.0.0.1:%SAMSEL_PORT%
pause
