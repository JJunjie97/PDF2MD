@echo off
setlocal EnableDelayedExpansion
set "MINERU_SKILL_SCRIPTS=%~dp0"
set "MINERU_SKILL_HOME=%MINERU_SKILL_SCRIPTS%.."
if defined MINERU_LOCAL_ROOT (
  for %%I in ("%MINERU_LOCAL_ROOT%") do set "MINERU_AGENT_ROOT=%%~fI"
) else (
  for %%I in ("%MINERU_SKILL_SCRIPTS%..\..\..") do set "MINERU_AGENT_ROOT=%%~fI"
)
set "MINERU_AGENT_PYTHON=%MINERU_AGENT_ROOT%\.conda-env\python.exe"
if not exist "%MINERU_AGENT_PYTHON%" (
  for %%I in ("%MINERU_SKILL_SCRIPTS%..\..\..\..") do set "MINERU_AGENT_ROOT=%%~fI"
  set "MINERU_AGENT_PYTHON=!MINERU_AGENT_ROOT!\.conda-env\python.exe"
)
if not exist "%MINERU_AGENT_PYTHON%" (
  for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -NonInteractive -Command "$item=Get-Item -LiteralPath $env:MINERU_SKILL_HOME -Force; if($item.Target){(Resolve-Path (Join-Path ([string]$item.Target) '..\..')).Path}"`) do set "MINERU_AGENT_ROOT=%%I"
  set "MINERU_AGENT_PYTHON=!MINERU_AGENT_ROOT!\.conda-env\python.exe"
)
if not exist "%MINERU_AGENT_PYTHON%" (
  echo {"ok":false,"error_code":"RUNTIME_MISSING","message":"Local MinerU Python environment was not found."}
  exit /b 5
)
"%MINERU_AGENT_PYTHON%" "%MINERU_SKILL_SCRIPTS%mineru_pdf.py" %*
exit /b %errorlevel%
