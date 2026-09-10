@echo off
setlocal
cd /d "%~dp0\.."
if not exist ".venv\Scripts\python.exe" (
  echo Project Python was not found.
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "packaging\build_msix.ps1" -PythonExecutable ".venv\Scripts\python.exe" %*
exit /b %ERRORLEVEL%
