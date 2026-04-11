@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File .\network-check.ps1
pause
