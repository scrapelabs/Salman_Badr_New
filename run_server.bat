@echo off
REM ===========================================================================
REM  MatchMiner - run the development server on port 8000 (Windows)
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

echo Starting MatchMiner at http://localhost:8000/   (press Ctrl+C to stop)
echo.
python manage.py runserver 0.0.0.0:8000

endlocal
