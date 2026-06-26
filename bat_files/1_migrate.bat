@echo off
REM ===========================================================================
REM  MatchMiner - 1. migrate only (Windows)
REM  Applies any new database migrations using the project's .venv, WITHOUT
REM  pulling code, reinstalling packages, or collecting static files.
REM  Use this after a `git pull` that only added a new migration (e.g. a new
REM  model field). For a full first-time install / update, run 0_setup.bat.
REM
REM  Why this exists: running `python manage.py migrate` in a plain terminal
REM  often picks up your SYSTEM Python (e.g. C:\Python310) instead of the
REM  project's .venv, which fails with "No module named 'dj_database_url'".
REM  This script activates the .venv first so the right packages are used.
REM ===========================================================================
setlocal
cd /d "%~dp0.."

echo ===========================================================
echo  MatchMiner - migrate only (step 1)
echo ===========================================================
echo.

REM --- Virtual environment must already exist (created by 0_setup.bat) -------
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] No virtual environment (.venv) was found.
    echo         Run 0_setup.bat first - it creates the .venv and installs the
    echo         dependencies. Then you can use this script for migrate-only.
    echo.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"

REM --- Apply migrations -----------------------------------------------------
cd /d "%~dp0..\artifacts\permitlify"

echo Applying database migrations ...
python manage.py migrate
if errorlevel 1 (
    echo.
    echo [ERROR] Migrations failed. Common causes:
    echo         * .env DATABASE_URL is wrong, or PostgreSQL is not running.
    echo         * A new dependency is missing - run 0_setup.bat to reinstall.
    echo.
    pause
    exit /b 1
)

echo.
echo ===========================================================
echo  Migrations applied. Next: run 3_run_server.bat
echo  (http://localhost:8000/)
echo ===========================================================
echo.
pause
endlocal
