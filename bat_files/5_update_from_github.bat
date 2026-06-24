@echo off
REM ===========================================================================
REM  MatchMiner - 5. update from GitHub (Windows)
REM  Pulls the latest code from origin/main (scrapelabs/Salman_Badr_New).
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

git pull origin main
if errorlevel 1 (
    echo.
    echo [ERROR] git pull failed. See the messages above.
    echo         If you have local edits that conflict, stash or commit them first.
    echo.
    pause
    exit /b 1
)

echo.
echo ===========================================================
echo  Update complete. Next step:
echo    Re-run 3_run_server.bat to restart with the latest code.
echo ===========================================================
echo.
pause
endlocal
