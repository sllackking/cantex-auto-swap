@echo off
setlocal

cd /d "%~dp0"

echo Starting Cantex Auto Swap UI...
powershell -ExecutionPolicy Bypass -File .\run-ui.ps1

echo.
echo UI process exited. Press any key to close.
pause >nul
