@echo off
setlocal
set "PDF2MD_PROJECT_ROOT=%~dp0"
set "PDF2MD_PYTHON=%PDF2MD_PROJECT_ROOT%runtime\env\python.exe"
set "PDF2MD_CLI=%PDF2MD_PROJECT_ROOT%src\pdf2md_cli.py"
set "PYTHONDONTWRITEBYTECODE=1"

if not exist "%PDF2MD_PYTHON%" (
  echo PDF2MD runtime not found: "%PDF2MD_PYTHON%" 1>&2
  echo Run powershell -ExecutionPolicy Bypass -File ".\scripts\install.ps1" first. 1>&2
  exit /b 5
)
if not exist "%PDF2MD_CLI%" (
  echo PDF2MD CLI source not found: "%PDF2MD_CLI%" 1>&2
  exit /b 5
)

"%PDF2MD_PYTHON%" "%PDF2MD_CLI%" %*
exit /b %errorlevel%
