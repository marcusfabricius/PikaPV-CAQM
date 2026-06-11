@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check-source.ps1" %*
exit /b %ERRORLEVEL%

