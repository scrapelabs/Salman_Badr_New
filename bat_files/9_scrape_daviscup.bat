@echo off
REM ===========================================================================
REM  MatchMiner - 9. run the Davis Cup scraper (Windows)
REM  Runs a real scrape against your .env DATABASE_URL and populates the DB.
REM  NOTE: this source (ITF/Stadion) is behind CloudFront, which blocks cloud
REM  data-center IPs. From a normal home/residential connection it usually
REM  works directly; if you get 403s, assign a residential proxy in the app
REM  (Lab -> Settings) and run it from there instead.
REM ===========================================================================
setlocal
if not exist "%~dp0..\.venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run 0_setup.bat first.
    echo.
    pause
    exit /b 1
)
call "%~dp0..\.venv\Scripts\activate.bat"
cd /d "%~dp0..\artifacts\permitlify"

echo ===========================================================
echo  Davis Cup  (input: season year)
echo ===========================================================
echo.
set "YEAR="
set /p YEAR="Season year (press Enter for the current year): "

python manage.py scrape_now davis_cup --year "%YEAR%" --out "%~dp0..\scrape_output"

echo.
echo CSVs (if any) were written under  bat_files\..\scrape_output\davis_cup\
echo.
pause
endlocal
