@echo off
setlocal EnableDelayedExpansion
set "PDF2MD_SKILL_SCRIPTS=%~dp0"
set "PDF2MD_SKILL_HOME=%PDF2MD_SKILL_SCRIPTS%.."
set "PYTHONDONTWRITEBYTECODE=1"
if defined PDF2MD_ROOT (
  for %%I in ("%PDF2MD_ROOT%") do set "PDF2MD_AGENT_ROOT=%%~fI"
) else (
  for %%I in ("%PDF2MD_SKILL_SCRIPTS%..\..\..") do set "PDF2MD_AGENT_ROOT=%%~fI"
)
set "PDF2MD_AGENT_PYTHON=%PDF2MD_AGENT_ROOT%\runtime\env\python.exe"
if not exist "%PDF2MD_AGENT_PYTHON%" (
  for %%I in ("%PDF2MD_SKILL_SCRIPTS%..\..\..\..") do set "PDF2MD_AGENT_ROOT=%%~fI"
  set "PDF2MD_AGENT_PYTHON=!PDF2MD_AGENT_ROOT!\runtime\env\python.exe"
)
if not exist "%PDF2MD_AGENT_PYTHON%" (
  set "PDF2MD_AGENT_ROOT=%USERPROFILE%\Desktop\PDF2MD"
  set "PDF2MD_AGENT_PYTHON=!PDF2MD_AGENT_ROOT!\runtime\env\python.exe"
)
if not exist "%PDF2MD_AGENT_PYTHON%" (
  for /d %%I in ("%USERPROFILE%\Desktop\*") do (
    if exist "%%~fI\src\pdf2md_cli.py" if exist "%%~fI\runtime\env\python.exe" (
      set "PDF2MD_AGENT_ROOT=%%~fI"
      set "PDF2MD_AGENT_PYTHON=%%~fI\runtime\env\python.exe"
    )
  )
)
if not exist "%PDF2MD_AGENT_PYTHON%" (
  for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -NonInteractive -Command "$item=Get-Item -LiteralPath $env:PDF2MD_SKILL_HOME -Force; if($item.Target){(Resolve-Path (Join-Path ([string]$item.Target) '..\..')).Path}"`) do set "PDF2MD_AGENT_ROOT=%%I"
  set "PDF2MD_AGENT_PYTHON=!PDF2MD_AGENT_ROOT!\runtime\env\python.exe"
)
if not exist "%PDF2MD_AGENT_PYTHON%" (
  echo {"ok":false,"error_code":"RUNTIME_MISSING","message":"Local PDF2MD Python environment was not found."}
  exit /b 5
)
rem Publish the discovered project root to the Python skill process.  A copied
rem global skill lives under .agents, so its own file location is not the root.
set "PDF2MD_ROOT=%PDF2MD_AGENT_ROOT%"
"%PDF2MD_AGENT_PYTHON%" "%PDF2MD_SKILL_SCRIPTS%pdf2md_pdf.py" %*
exit /b %errorlevel%
