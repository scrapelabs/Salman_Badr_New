@echo off
REM ===========================================================================
REM  MatchMiner - 8. run the Croatia League (HTS) scraper (Windows)
REM  Runs a real scrape against your .env DATABASE_URL and populates the DB.
REM  Input: EITHER a single tournament URL, OR a date range.
REM ===========================================================================
setlocal
if not exist "%~dp0..\.venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run 0_install.bat first.
    echo.
    pause
    exit /b 1
)
call "%~dp0..\.venv\Scripts\activate.bat"
cd /d "%~dp0..\artifacts\permitlify"

echo ===========================================================
echo  Croatia League (HTS)
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
python manage.py scrape_now croatia_league --date-from "%DF%" --date-to "%DT%" --out "%~dp0..\scrape_output"
goto done

:runurl
python manage.py scrape_now croatia_league --url "%URL%" --out "%~dp0..\scrape_output"

:done
echo.
echo CSVs (if any) were written under  bat_files\..\scrape_output\croatia_league\
echo.
pause
endlocal
