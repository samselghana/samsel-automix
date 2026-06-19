@echo off
cd /d "%~dp0"
REM Use set "SAMSEL_PORT=8000" (quotes, no spaces after the number) for DJ desktop_app.py -p 8000.
if "%SAMSEL_PORT%"=="" set "SAMSEL_PORT=8765"

REM =============================================================================
REM Option A — One hostname (e.g. samsel-automix.com) -> this PC only (uvicorn)
REM =============================================================================
REM Before first run (Cloudflare dashboard — do once):
REM   1) Workers & Pages: remove custom domain from samsel-automix.com if you see
REM      "DNS managed by Workers" (apex must be free for the tunnel CNAME).
REM   2) Zero Trust -> Networks -> Tunnels -> Create tunnel (or open existing).
REM   2b) On THIS Windows user, once:  cloudflared tunnel login
REM        (Browser opens — pick the account/zone for samsel-automix.com.)
REM        Creates cert.pem under %USERPROFILE%\.cloudflared\  — required for "tunnel run".
REM   3) Public hostname:
REM        Subdomain: @     Domain: samsel-automix.com
REM        Type: HTTP      URL: http://127.0.0.1:%SAMSEL_PORT%
REM      (Optional) Repeat for www -> same URL, or add redirect in Cloudflare.
REM      One hostname -> one origin. For DJ Engine Pro on a *second* service, add e.g.
REM        Subdomain: dj   URL: http://127.0.0.1:8000  (match py -3.10 desktop_app.py -p 8000)
REM   4) Note the tunnel NAME you chose (below).
REM Every session (this PC):
REM   SAMSEL Web:  Window 1 run_web.bat  |  Window 2 this file (same SAMSEL_PORT as hostname URL)
REM   DJ desktop:  Window 1 py -3.10 desktop_app.py -p %SAMSEL_PORT%  |  Window 2 this file
REM =============================================================================

REM Set this to the exact tunnel name from: cloudflared tunnel list
if "%SAMSEL_CF_TUNNEL_NAME%"=="" set SAMSEL_CF_TUNNEL_NAME=samsel-automix

echo.
echo  Option A: cloudflared tunnel run "%SAMSEL_CF_TUNNEL_NAME%"
echo  Service on this PC must be: http://127.0.0.1:%SAMSEL_PORT%  ^(run_web.bat or desktop_app.py -p^)
echo.
echo  502 Bad Gateway / Unable to reach origin:
echo    - Zero Trust -^> Tunnels -^> your tunnel -^> Public Hostname: Service URL must be
echo      Type HTTP, URL http://127.0.0.1:%SAMSEL_PORT%  ^(not https, not localhost^).
echo    - Origin must be running; test http://127.0.0.1:%SAMSEL_PORT%/api/health locally.
echo.

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo [ERROR] cloudflared not in PATH.
  echo   winget install Cloudflare.cloudflared
  pause
  exit /b 1
)

REM Origin certificate (fixes: Cannot determine default origin certificate path / origincert)
if not defined TUNNEL_ORIGIN_CERT (
  if exist "%USERPROFILE%\.cloudflared\cert.pem" set "TUNNEL_ORIGIN_CERT=%USERPROFILE%\.cloudflared\cert.pem"
)
if not defined TUNNEL_ORIGIN_CERT (
  if exist "%USERPROFILE%\.cloudflare-warp\cert.pem" set "TUNNEL_ORIGIN_CERT=%USERPROFILE%\.cloudflare-warp\cert.pem"
)
if not defined TUNNEL_ORIGIN_CERT (
  echo [ERROR] Missing Cloudflare origin certificate ^(cert.pem^).
  echo   Named tunnels need a one-time login on this PC. Run in CMD:
  echo     cloudflared tunnel login
  echo   Sign in and select the zone that contains samsel-automix.com.
  echo   That creates:  %USERPROFILE%\.cloudflared\cert.pem
  echo   Or set TUNNEL_ORIGIN_CERT^=C:\full\path\to\cert.pem  then run this bat again.
  echo.
  pause
  exit /b 1
)
echo Using origin cert: %TUNNEL_ORIGIN_CERT%

powershell -NoProfile -Command "try { $u = 'http://127.0.0.1:%SAMSEL_PORT%/api/health'; $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 4; if ($r.StatusCode -eq 200) { exit 0 } } catch { }; exit 1" >nul 2>&1
if errorlevel 1 (
  echo [WARN] No HTTP 200 from http://127.0.0.1:%SAMSEL_PORT%/api/health
  echo        Start run_web.bat or desktop_app.py -p %SAMSEL_PORT%, or fix the Public Hostname port in Cloudflare.
  echo.
  pause
)

cloudflared tunnel --origincert "%TUNNEL_ORIGIN_CERT%" run "%SAMSEL_CF_TUNNEL_NAME%"
echo.
echo Tunnel exited.
pause
