@echo off
REM ===========================================================================
REM  MatchMiner - 1. apply database migrations (Windows)
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

python manage.py migrate

echo.
pause
endlocal
