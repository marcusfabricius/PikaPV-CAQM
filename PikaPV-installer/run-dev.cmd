@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-dev.ps1" %*
exit /b %ERRORLEVEL%

