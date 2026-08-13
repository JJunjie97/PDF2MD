@echo off
setlocal
set "MINERU_PROJECT_ROOT=%~dp0"
set "MINERU_PYTHON=%MINERU_PROJECT_ROOT%runtime\env\python.exe"
set "MINERU_CLI=%MINERU_PROJECT_ROOT%src\mineru_cli.py"
set "PYTHONDONTWRITEBYTECODE=1"

if not exist "%MINERU_PYTHON%" (
  echo MinerU runtime not found: "%MINERU_PYTHON%" 1>&2
  echo Run powershell -ExecutionPolicy Bypass -File ".\scripts\install.ps1" first. 1>&2
  exit /b 5
)
if not exist "%MINERU_CLI%" (
  echo MinerU CLI source not found: "%MINERU_CLI%" 1>&2
  exit /b 5
)

"%MINERU_PYTHON%" "%MINERU_CLI%" %*
exit /b %errorlevel%
