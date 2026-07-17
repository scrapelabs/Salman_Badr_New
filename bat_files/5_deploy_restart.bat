@echo off
REM ===========================================================================
REM  MatchMiner - apply Django changes and restart Waitress (Windows)
REM
REM  This script intentionally performs NO Git operations and NO package install.
REM  It keeps the current Waitress process running while Django preparation runs,
REM  then restarts only this project's Waitress process after every step succeeds.
REM ===========================================================================
setlocal EnableExtensions

set "NO_PAUSE=0"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "APP_DIR=%PROJECT_ROOT%\artifacts\matchminer"
set "VENV_PY=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "EXIT_CODE=1"

echo ===========================================================
echo  MatchMiner - migrate, collect static, restart Waitress
echo ===========================================================
echo.

if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment not found: %VENV_PY%
    echo         Run bat_files\0_setup.bat first.
    goto :finish
)

if not exist "%APP_DIR%\manage.py" (
    echo [ERROR] Django application not found: %APP_DIR%
    goto :finish
)

pushd "%APP_DIR%"
if errorlevel 1 (
    echo [ERROR] Could not enter the Django application directory.
    goto :finish
)

echo [1/3] Running Django checks while the current server stays online ...
"%VENV_PY%" manage.py check
if errorlevel 1 (
    popd
    echo [ERROR] Django checks failed. Waitress was not restarted.
    goto :finish
)

echo.
echo [2/3] Applying database migrations ...
"%VENV_PY%" manage.py migrate --noinput
if errorlevel 1 (
    popd
    echo [ERROR] Migrations failed. Waitress was not restarted.
    goto :finish
)

echo.
echo [3/3] Collecting static files ...
"%VENV_PY%" manage.py collectstatic --noinput
if errorlevel 1 (
    popd
    echo [ERROR] collectstatic failed. Waitress was not restarted.
    goto :finish
)
popd

echo.
echo Stopping this project's Waitress process tree ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$target = [IO.Path]::GetFullPath('%VENV_PY%'); $processes = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.ExecutablePath -and ([IO.Path]::GetFullPath($_.ExecutablePath) -ieq $target) -and $_.CommandLine -match '(?i)(^|\s)-m\s+waitress(\s|$)' -and $_.CommandLine -match 'matchminer\.wsgi:application' }); foreach ($process in $processes) { Write-Host ('Stopping Waitress PID ' + $process.ProcessId); & taskkill.exe /PID $process.ProcessId /T /F; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }; exit 0"
if errorlevel 1 (
    echo [ERROR] Could not stop the existing Waitress process safely.
    goto :finish
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline = (Get-Date).AddSeconds(15); do { $listener = Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue; if (-not $listener) { exit 0 }; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
    echo [ERROR] Port 80 did not become available after stopping Waitress.
    goto :finish
)

echo Starting Waitress in a new minimized window ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$arguments = @('-m', 'waitress', '--listen=0.0.0.0:80', '--threads=16', '--channel-timeout=1200', 'matchminer.wsgi:application'); $process = Start-Process -FilePath '%VENV_PY%' -ArgumentList $arguments -WorkingDirectory '%APP_DIR%' -WindowStyle Minimized -PassThru; Write-Host ('Started Waitress launcher PID ' + $process.Id)"
if errorlevel 1 (
    echo [ERROR] Windows could not launch Waitress.
    goto :finish
)

echo Waiting for http://127.0.0.1/ to become healthy ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline = (Get-Date).AddSeconds(45); do { try { $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1/' -TimeoutSec 5; if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) { exit 0 } } catch {}; Start-Sleep -Seconds 1 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
    echo [ERROR] Waitress did not pass the HTTP health check within 45 seconds.
    goto :finish
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$target = [IO.Path]::GetFullPath('%VENV_PY%'); $process = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.ExecutablePath -and ([IO.Path]::GetFullPath($_.ExecutablePath) -ieq $target) -and $_.CommandLine -match '(?i)(^|\s)-m\s+waitress(\s|$)' -and $_.CommandLine -match 'matchminer\.wsgi:application' } | Select-Object -First 1; if ($process) { Write-Host ('Waitress is running as PID ' + $process.ProcessId); exit 0 }; exit 1"
if errorlevel 1 (
    echo [ERROR] HTTP responded, but the project Waitress process was not found.
    goto :finish
)

echo.
echo ===========================================================
echo  Deployment steps completed and Waitress is healthy.
echo ===========================================================
set "EXIT_CODE=0"

:finish
echo.
if "%NO_PAUSE%"=="0" pause
endlocal & exit /b %EXIT_CODE%
