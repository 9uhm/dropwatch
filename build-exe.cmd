@echo off
setlocal
cd /d "%~dp0"

rem Builds dist\dropwatch.exe -- a single file that needs no Python install.

set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" (
  echo   Missing venv at %PY%
  echo   Create it:  py -3.14 -m venv .venv ^&^& .venv\Scripts\python -m pip install -e .
  pause
  exit /b 1
)

"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo   Installing PyInstaller...
  "%PY%" -m pip install pyinstaller
  if errorlevel 1 ( pause & exit /b 1 )
)

echo   Building ^(takes a minute^)...
"%PY%" -m PyInstaller dropwatch.spec --noconfirm --log-level WARN
if errorlevel 1 (
  echo.
  echo   Build FAILED.
  pause
  exit /b 1
)

echo.
echo   Built  dist\dropwatch.exe
echo.
echo   It keeps its state beside itself, so copy the exe wherever you want.
echo.
echo   Just double-click it. The app opens, and signing in happens in the window.
echo   Closing the window keeps it watching in the tray; double-click again to
echo   bring it back. Right-click the tray icon to pause or quit.
echo.
echo   From a terminal it is still a normal CLI:
echo     dropwatch.exe doctor     check the install
echo     dropwatch.exe serve      watch with a console instead of a window
echo.
echo   config.toml is optional -- without one it uses defaults and auto-discovery.
echo.
pause
