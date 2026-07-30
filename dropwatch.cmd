@echo off
setlocal
cd /d "%~dp0"

rem Double-clickable launcher. Uses the project venv, so no build step and no
rem PyInstaller involved -- this works the moment the venv exists.

set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" (
  echo.
  echo   The virtual environment is missing.
  echo   Expected: %PY%
  echo.
  echo   Create it with:
  echo     py -3.14 -m venv .venv
  echo     .venv\Scripts\python -m pip install -e .
  echo.
  pause
  exit /b 1
)

:menu
cls
echo.
echo   dropwatch
echo   ============
echo.
echo     1.  Start watching  (opens the dashboard)
echo     2.  Start watching  (no browser)
echo     3.  Log in to Twitch
echo     4.  Check setup     (doctor)
echo     5.  Show history    (sessions and transitions)
echo     6.  Check Twitch API (gql-check)
echo     7.  Who am I
echo.
echo     Q.  Quit
echo.
set /p choice=  Choose:

if /i "%choice%"=="1" goto serve_open
if /i "%choice%"=="2" goto serve
if /i "%choice%"=="3" goto login
if /i "%choice%"=="4" goto doctor
if /i "%choice%"=="5" goto status
if /i "%choice%"=="6" goto gqlcheck
if /i "%choice%"=="7" goto whoami
if /i "%choice%"=="q" exit /b 0
goto menu

:serve_open
cls
echo   Starting. Ctrl-C to stop, then close this window.
echo.
"%PY%" -m dropwatch serve --open
goto done

:serve
cls
echo   Starting. Dashboard at http://127.0.0.1:8787/
echo   Ctrl-C to stop.
echo.
"%PY%" -m dropwatch serve
goto done

:login
cls
"%PY%" -m dropwatch login
goto done

:doctor
cls
"%PY%" -m dropwatch doctor
goto done

:status
cls
"%PY%" -m dropwatch status
goto done

:gqlcheck
cls
"%PY%" -m dropwatch gql-check
goto done

:whoami
cls
"%PY%" -m dropwatch whoami
goto done

:done
echo.
pause
goto menu
