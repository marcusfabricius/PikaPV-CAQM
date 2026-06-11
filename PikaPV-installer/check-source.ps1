[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

& $Python -m py_compile `
    (Join-Path $Root "src\web_app.py") `
    (Join-Path $Root "src\pikapv_backend.py")

if ($LASTEXITCODE -ne 0) {
    throw "Python source validation failed."
}

Write-Host "PikaPV source validation passed."

