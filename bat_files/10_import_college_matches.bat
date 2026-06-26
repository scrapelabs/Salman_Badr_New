@echo off
REM ===========================================================================
REM  MatchMiner - 10. import historical College Dual Match CSVs (Windows)
REM  Bulk-loads CSV exports into the match database, deduped against what's
REM  already stored (re-importing the same file inserts nothing new). Imported
REM  rows are tagged "import" so the Lab "Match database" tab can tell them
REM  apart from scraped rows.
REM
REM  Drop your historical export CSVs into:
REM      artifacts\permitlify\imports\college_dual_match\
REM  then double-click this file. Or type a specific CSV file / folder path
REM  at the prompt to import from somewhere else.
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
echo  Import College Dual Match CSVs
echo ===========================================================
echo.
echo Press Enter to import everything in
echo   artifacts\permitlify\imports\college_dual_match\
echo or type a CSV file / folder path to import from there instead.
echo.
set "CSVPATH="
set /p CSVPATH="Path (Enter for the default drop folder): "

REM Quote the path so folders with spaces work; call with no arg when blank so
REM the command falls back to its default drop folder.
if "%CSVPATH%"=="" (
    "%VENV_PY%" manage.py import_college_matches
) else (
    "%VENV_PY%" manage.py import_college_matches "%CSVPATH%"
)

echo.
pause
endlocal
