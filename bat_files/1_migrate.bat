@echo off
REM ===========================================================================
REM  MatchMiner - 1. migrate + refresh static (Windows)
REM  Quick post-pull update: applies new database migrations AND collects
REM  static files, using the project's .venv - WITHOUT pulling code or
REM  reinstalling packages. Use this after a `git pull` that added a migration
REM  (e.g. a new model field) and/or changed static assets. For a full
REM  first-time install / update (pull + deps + migrate + static), run
REM  0_setup.bat instead.
REM
REM  Uses the venv interpreter BY FULL PATH (.venv\Scripts\python.exe) instead
REM  of "activate" + "python", because "python" in a plain terminal often
REM  resolves to the SYSTEM Python (e.g. C:\Python310), which lacks this
REM  project's packages and fails with "No module named 'dj_database_url'".
REM ===========================================================================
setlocal
set "VENV_PY=%~dp0..\.venv\Scripts\python.exe"

echo ===========================================================
echo  MatchMiner - migrate + refresh static (step 1)
echo ===========================================================
echo.

REM --- Virtual environment must already exist (created by 0_setup.bat) -------
if not exist "%VENV_PY%" (
    echo [ERROR] No virtual environment (.venv) was found.
    echo         Run 0_setup.bat first - it creates the .venv and installs the
    echo         dependencies. Then you can use this script for quick updates.
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0..\artifacts\permitlify"

REM --- Migrations -----------------------------------------------------------
echo Applying database migrations ...
"%VENV_PY%" manage.py migrate
if errorlevel 1 (
    echo.
    echo [ERROR] Migrations failed. Common causes:
    echo         * .env DATABASE_URL is wrong, or PostgreSQL is not running.
    echo         * A new dependency is missing - run 0_setup.bat to reinstall.
    echo.
    pause
    exit /b 1
)

REM --- Static files ---------------------------------------------------------
echo.
echo Collecting static files ...
"%VENV_PY%" manage.py collectstatic --noinput
if errorlevel 1 (
    echo.
    echo [ERROR] collectstatic failed. See the messages above.
    echo.
    pause
    exit /b 1
)

echo.
echo ===========================================================
echo  Done (migrate + static). Next: run 3_run_server.bat
echo  (http://localhost:8000/)
echo ===========================================================
echo.
pause
endlocal
