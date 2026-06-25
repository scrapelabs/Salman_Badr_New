@echo off
REM ===========================================================================
REM  MatchMiner - 5. update from GitHub (Windows)
REM  Pulls the latest code from origin/main (scrapelabs/Salman_Badr_New) AND
REM  reinstalls Python dependencies, so any newly-added package (e.g. psutil for
REM  the live system monitor) is present before you restart the server.
REM  After it finishes, re-run 3_run_server.bat so Django picks up the changes.
REM ===========================================================================
setlocal
cd /d "%~dp0.."

REM --- Git check -------------------------------------------------------------
git --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git was not found on your PATH.
    echo         Install Git from https://git-scm.com/download/win and re-run.
    echo.
    pause
    exit /b 1
)

echo ===========================================================
echo  MatchMiner - updating from GitHub (origin/main)
echo ===========================================================
echo.

REM  gc.auto=0 stops git from repacking/cleaning old pack files during the pull.
REM  That repack step is what triggers Windows "Unlink of file
REM  '.git/objects/pack/*.idx' failed" errors when another program (an editor,
REM  the running server, antivirus, or OneDrive/Dropbox sync) has the repo open.
git -c gc.auto=0 pull origin main
if errorlevel 1 (
    echo.
    echo [ERROR] git pull failed. See the messages above.
    echo         - If you saw "Unlink of file ... .idx failed (y/n)", answer n to
    echo           each prompt: the new code is already fetched; git just cannot
    echo           delete old pack files because something has the repo open.
    echo         - Close your editor, this server, File Explorer, and pause AV,
    echo           then re-run this script. If the project lives in OneDrive /
    echo           Dropbox / Google Drive, move it to a plain local folder.
    echo         - If you have local edits that conflict, stash or commit them first.
    echo.
    pause
    exit /b 1
)

REM --- Reinstall dependencies ------------------------------------------------
REM  A git pull never installs new packages, so do it here. Without this, a
REM  freshly-added dependency is missing and features silently degrade (e.g. the
REM  CPU/memory/disk gauges read 0 when psutil isn't installed).
echo.
if not exist ".venv\Scripts\activate.bat" (
    echo [WARN] No virtual environment found (.venv).
    echo        Run 0_install.bat once to create it and install dependencies.
    echo.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"
echo Reinstalling Python packages (requirements.txt) ...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. See the messages above.
    echo.
    pause
    exit /b 1
)

echo.
echo ===========================================================
echo  Update complete. Next steps:
echo    1. If the schema changed, double-click 1_migrate.bat
echo    2. Re-run 3_run_server.bat to restart with the latest code.
echo ===========================================================
echo.
pause
endlocal
