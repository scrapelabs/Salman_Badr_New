@echo off
REM ===========================================================================
REM  MatchMiner - 3. run the server with Waitress on port 80 (Windows)
REM  Uses the venv interpreter by full path (no reliance on "activate").
REM
REM  NOTES:
REM   * Port 80 is privileged: run this **as Administrator** and make sure no
REM     other service (IIS, Skype, another web server) already holds port 80.
REM   * Waitress serves the WSGI app directly, so static files come from
REM     WhiteNoise. Run 0_setup.bat (or 1_migrate.bat) first so collectstatic
REM     has generated them, or CSS/JS will 404.
REM ===========================================================================
setlocal
set "VENV_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment not found. Run 0_setup.bat first.
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0..\artifacts\permitlify"

echo Starting MatchMiner (Waitress) at http://localhost/   (press Ctrl+C to stop)
echo.
REM  --channel-timeout raises the per-connection inactivity limit (default 120s)
REM  so very large CSV/log downloads (south_africa runs can be 100 MB+) have time
REM  to start streaming over a slow/remote link without the channel being closed.
"%VENV_PY%" -m waitress --listen=0.0.0.0:80 --channel-timeout=1200 matchminer.wsgi:application

endlocal
