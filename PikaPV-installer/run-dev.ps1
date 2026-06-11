[CmdletBinding()]
param(
    [switch]$InstallDependencies
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

if ($InstallDependencies -or -not (Test-Path -LiteralPath $Python)) {
    if (-not (Test-Path -LiteralPath $Python)) {
        python -m venv $Venv
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the development virtual environment."
        }
    }
    & $Python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Could not update pip in the development virtual environment."
    }
    & $Python -m pip install -r (Join-Path $Root "src\requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install PikaPV development dependencies."
    }
}

Push-Location (Join-Path $Root "src")
try {
    & $Python web_app.py
}
finally {
    Pop-Location
}
