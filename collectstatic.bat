@echo off
REM ===========================================================================
REM  MatchMiner - collect static files (Windows)
REM ===========================================================================
setlocal
if not exist "%~dp0.venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run install.bat first.
    echo.
    pause
    exit /b 1
)
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0artifacts\permitlify"

python manage.py collectstatic --noinput

echo.
pause
endlocal
