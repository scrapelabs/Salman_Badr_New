@echo off
REM ===========================================================================
REM  MatchMiner - 3. run the development server on port 8000 (Windows)
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

echo Starting MatchMiner at http://localhost:8000/   (press Ctrl+C to stop)
echo.
"%VENV_PY%" manage.py runserver 0.0.0.0:8000

endlocal
