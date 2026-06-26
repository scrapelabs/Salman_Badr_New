@echo off
REM ===========================================================================
REM  MatchMiner - 0. setup / update (Windows)   [merges old 0, 1, 2, 5]
REM  One double-click does everything to get going or update:
REM    * pulls the latest code from GitHub (origin/main)
REM    * creates the .venv (first run) and installs / updates dependencies
REM    * creates .env from .env.example on the very first run
REM    * applies database migrations
REM    * collects static files
REM  Run it whenever you want to install OR update, then run 3_run_server.bat.
REM
REM  Every Python command uses the venv interpreter BY FULL PATH
REM  (.venv\Scripts\python.exe) instead of relying on "activate" + "python".
REM  In a plain terminal "python" often resolves to the SYSTEM Python
REM  (e.g. C:\Python310), which lacks this project's packages and fails with
REM  "No module named 'dj_database_url'" - so we never depend on it here.
REM ===========================================================================
setlocal
cd /d "%~dp0.."
set "VENV_PY=%~dp0..\.venv\Scripts\python.exe"

echo ===========================================================
echo  MatchMiner - setup / update (step 0)
echo ===========================================================
echo.

REM --- 1. Python check (only needed to CREATE the venv) ----------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on your PATH.
    echo         Install Python 3.11+ from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH" during setup, then re-run.
    echo.
    pause
    exit /b 1
)

REM --- 2. Update from GitHub (origin/main) -----------------------------------
REM  gc.auto=0 stops git repacking old pack files mid-pull, which is what
REM  triggers Windows "Unlink of file '.git/objects/pack/*.idx' failed" errors
REM  when an editor / the server / antivirus / OneDrive has the repo open.
REM  The "< nul" feeds git an empty stdin so its "Unlink ... Should I try
REM  again? (y/n)" retry prompt reads EOF and auto-answers "no" instead of
REM  hanging forever; the skipped file is a redundant *.idx (harmless, git
REM  cleans it on the next gc). Close the editor / server / File Explorer and
REM  pause antivirus / OneDrive, then re-run for a fully clean pull.
git --version >nul 2>&1
if errorlevel 1 (
    echo [WARN] Git was not found on your PATH - skipping the GitHub update.
    echo        Install Git from https://git-scm.com/download/win to enable it.
    echo.
) else (
    echo Pulling latest code from origin/main ...
    git -c gc.auto=0 pull origin main < nul
    if errorlevel 1 (
        echo.
        echo [WARN] git pull did not complete cleanly - continuing with the code
        echo        you already have. If you saw "Unlink of file ... .idx failed",
        echo        close your editor / server / File Explorer, pause antivirus,
        echo        and re-run. If the project lives in OneDrive / Dropbox / Google
        echo        Drive, move it to a plain local folder. If you have local edits
        echo        that conflict, stash or commit them first.
        echo.
    )
)

REM --- 3. Virtual environment ------------------------------------------------
if not exist "%VENV_PY%" (
    echo Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        echo.
        pause
        exit /b 1
    )
)
if not exist "%VENV_PY%" (
    echo [ERROR] .venv exists but has no python.exe inside. Delete the .venv
    echo         folder and re-run this script to recreate it.
    echo.
    pause
    exit /b 1
)

REM --- 4. Dependencies -------------------------------------------------------
echo Installing / updating Python packages ...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. See the messages above.
    echo.
    pause
    exit /b 1
)

REM --- 5. .env (first run only) ----------------------------------------------
REM  On the very first run there are no DB credentials yet, so migrate would
REM  fail. Create .env, then stop and ask the user to fill it in and re-run.
set FRESH_ENV=0
if not exist ".env" (
    copy ".env.example" ".env" >nul
    set FRESH_ENV=1
    echo Created .env from .env.example.
)

if "%FRESH_ENV%"=="1" (
    echo.
    echo ===========================================================
    echo  First-time setup almost done!
    echo    1. Open .env and set your PostgreSQL connection in
    echo       DATABASE_URL ^(postgres://USER:PASSWORD@HOST:PORT/DBNAME^).
    echo    2. Re-run this script to finish ^(migrate + collectstatic^).
    echo    3. Then run 3_run_server.bat   ^(http://localhost:8000/^)
    echo ===========================================================
    echo.
    pause
    endlocal
    exit /b 0
)

REM --- 6. Migrations --------------------------------------------------------
cd /d "%~dp0..\artifacts\permitlify"

echo.
echo Applying database migrations ...
"%VENV_PY%" manage.py migrate
if errorlevel 1 (
    echo.
    echo [ERROR] Migrations failed. Check DATABASE_URL in .env - is PostgreSQL
    echo         running and are the username / password / host / db name right?
    echo.
    pause
    exit /b 1
)

REM --- 7. Static files ------------------------------------------------------
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
echo  Setup / update complete. Next steps:
echo    * Run 3_run_server.bat          (http://localhost:8000/)
echo    * Need an admin login? Run 4_create_superadmin.bat
echo ===========================================================
echo.
pause
endlocal
