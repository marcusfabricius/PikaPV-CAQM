@echo off
pushd "%~dp0"
set PYTHON=
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set PYTHON=python
) else (
    where py >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set PYTHON=py
    )
)
if "%PYTHON%"=="" (
    echo Python executable not found in PATH.
    echo Install Python 3 and try again.
    pause
    exit /b 1
)
"%PYTHON%" "%~dp0start_web_gui.py" %*
if ERRORLEVEL 1 pause
popd
