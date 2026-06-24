@echo off
REM ===========================================================================
REM  MatchMiner - 0. install (Windows)
REM  Creates a .venv at the project root, installs deps, and prepares .env.
REM  Run the scripts in this folder in order: 0 -> 1 -> 2 -> 3.
REM ===========================================================================
setlocal
cd /d "%~dp0.."

echo ===========================================================
echo  MatchMiner - install (step 0)
echo ===========================================================
echo.

REM --- 1. Python check -------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on your PATH.
    echo         Install Python 3.11+ from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH" during setup, then re-run.
    echo.
    pause
    exit /b 1
)

REM --- 2. Virtual environment ------------------------------------------------
if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        echo.
        pause
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat"

REM --- 3. Dependencies -------------------------------------------------------
echo Installing Python packages ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. See the messages above.
    echo.
    pause
    exit /b 1
)

REM --- 4. .env ---------------------------------------------------------------
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env from .env.example.
)

echo.
echo ===========================================================
echo  Install complete. Next steps (run in order):
echo    1. Edit .env and set your PostgreSQL credentials
echo    2. Double-click 1_migrate.bat        (create the tables)
echo    3. Double-click 2_collectstatic.bat  (gather static files)
echo    4. Double-click 3_run_server.bat     (http://localhost:8000/)
echo ===========================================================
echo.
pause
endlocal
