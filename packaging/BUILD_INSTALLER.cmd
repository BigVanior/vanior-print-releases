@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_latest_installer.ps1"
if errorlevel 1 (
  echo.
  echo Installer build failed. See the error above.
) else (
  echo.
  echo Installer build completed successfully.
)
pause
