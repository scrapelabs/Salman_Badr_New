@echo off
REM ===========================================================================
REM  MatchMiner - 11. run the Belgium (Tennis & Padel Vlaanderen) scraper (Windows)
REM  Runs a real scrape against your .env DATABASE_URL and populates the DB.
REM  Input: EITHER a single tournament URL, OR a date range.
REM  NOTE: the site is behind a Zenedge captcha. A live run needs TensorFlow
REM        installed AND the captcha model present at
REM        artifacts\permitlify\accounts\live_scrapers\belgium_assets\captcha_model.keras
REM        otherwise the run fails honestly (no fabricated rows).
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
echo  Belgium Results (Tennis ^& Padel Vlaanderen)
echo   - Paste one tournament URL, OR leave it blank to use a date range.
echo ===========================================================
echo.
set "URL="
set /p URL="Tournament URL (press Enter to use a date range instead): "
if not "%URL%"=="" goto runurl

set "DF="
set "DT="
set /p DF="Start date YYYY-MM-DD (press Enter for the default window): "
set /p DT="End date YYYY-MM-DD (press Enter for the default window): "
"%VENV_PY%" manage.py scrape_now belgium_results --date-from "%DF%" --date-to "%DT%" --out "%~dp0..\scrape_output"
goto done

:runurl
"%VENV_PY%" manage.py scrape_now belgium_results --url "%URL%" --out "%~dp0..\scrape_output"

:done
echo.
echo CSVs (if any) were written under  bat_files\..\scrape_output\belgium_results\
echo.
pause
endlocal
