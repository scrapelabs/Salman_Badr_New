@echo off
REM ===========================================================================
REM  MatchMiner - 4. create a superadmin user (Windows)
REM  Interactively creates a Django superuser (full admin account).
REM  Requires the database to exist first (run 1_migrate.bat once).
REM ===========================================================================
setlocal
if not exist "%~dp0..\.venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run 0_install.bat first.
    echo.
    pause
    exit /b 1
)
call "%~dp0..\.venv\Scripts\activate.bat"
cd /d "%~dp0..\artifacts\permitlify"

echo ===========================================================
echo  Create a MatchMiner superadmin
echo  You'll be prompted for a username, email (optional),
echo  and a password.
echo ===========================================================
echo.

python manage.py createsuperuser

echo.
pause
endlocal
