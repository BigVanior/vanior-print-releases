@echo off
setlocal
set "REPOSITORY=%~dp0.."
set "PYTHON=%REPOSITORY%\.venv\Scripts\python.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_installer.ps1" -PythonExecutable "%PYTHON%"
if errorlevel 1 (
  echo.
  echo Independent preview build failed. See the error above.
) else (
  echo.
  echo Independent preview installer completed successfully.
)
pause
