@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File .\run-ui.ps1
pause
