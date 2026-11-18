@echo off
setlocal
cd /d "%~dp0"
title Build RecapUpgrade.exe
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1"
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
  echo.
  echo BUILD FAILED - exit code %EXITCODE%
  pause
)
exit /b %EXITCODE%
