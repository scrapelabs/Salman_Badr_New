@echo off
REM ===========================================================================
REM  MatchMiner - 2. collect static files (Windows)
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

python manage.py collectstatic --noinput

echo.
pause
endlocal
