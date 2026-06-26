@echo off
REM ===========================================================================
REM  MatchMiner - 4. create a superadmin user (Windows)
REM  Interactively creates a Django superuser (full admin account).
REM  Requires the database to exist first (run 0_setup.bat once).
REM  Uses the venv interpreter by full path (no reliance on "activate").
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

echo ===========================================================
echo  Create a MatchMiner superadmin
echo  You'll be prompted for a username, email (optional),
echo  and a password.
echo ===========================================================
echo.

"%VENV_PY%" manage.py createsuperuser

echo.
pause
endlocal
