@echo off
REM ===========================================================================
REM  MatchMiner - 7. run the Brazil Results (CBT) scraper (Windows)
REM  Runs a real scrape against your .env DATABASE_URL and populates the DB.
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

echo ===========================================================
echo  Brazil Results (CBT)  (input: season year + month)
echo ===========================================================
echo.
set "YEAR="
set /p YEAR="Season year (press Enter for the current year): "
set "MONTH="
set /p MONTH="Month 1-12, or 0 for the whole year (press Enter for all): "

"%VENV_PY%" manage.py scrape_now brazil_results --year "%YEAR%" --month "%MONTH%" --out "%~dp0..\scrape_output"

echo.
echo CSVs (if any) were written under  bat_files\..\scrape_output\brazil_results\
echo.
pause
endlocal
