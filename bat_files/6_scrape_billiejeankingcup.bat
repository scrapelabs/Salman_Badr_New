@echo off
REM ===========================================================================
REM  MatchMiner - 6. run the Billie Jean King Cup scraper (Windows)
REM  Runs a real scrape against your .env DATABASE_URL and populates the DB.
REM  NOTE: this source (ITF/Stadion) is behind CloudFront, which blocks cloud
REM  data-center IPs. From a normal home/residential connection it usually
REM  works directly; if you get 403s, assign a residential proxy in the app
REM  (Lab -> Settings) and run it from there instead.
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
echo  Billie Jean King Cup  (input: season year)
echo ===========================================================
echo.
set "YEAR="
set /p YEAR="Season year (press Enter for the current year): "

"%VENV_PY%" manage.py scrape_now billiejeankingcup --year "%YEAR%" --out "%~dp0..\scrape_output"

echo.
echo CSVs (if any) were written under  bat_files\..\scrape_output\billiejeankingcup\
echo.
pause
endlocal
