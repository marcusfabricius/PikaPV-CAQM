[CmdletBinding()]
param(
    [string]$Version,
    [switch]$SkipInstaller,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".build-venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$VersionFile = Join-Path $Root "VERSION"

if (-not $Version) {
    $Version = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
}
if ($Version -notmatch '^\d+\.\d+\.\d+([-.][0-9A-Za-z.-]+)?$') {
    throw "VERSION must look like 1.2.3. Current value: $Version"
}

function Remove-BuildDirectory {
    param([Parameter(Mandatory)][string]$RelativePath)

    $target = Join-Path $Root $RelativePath
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $resolvedTarget = [System.IO.Path]::GetFullPath($target)
    if (-not $resolvedTarget.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside the project: $resolvedTarget"
    }
    if (Test-Path -LiteralPath $resolvedTarget) {
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
}

if ($Clean) {
    foreach ($path in @("build", "dist", "installer-output")) {
        Remove-BuildDirectory -RelativePath $path
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv $Venv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the build virtual environment."
    }
}

& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Could not update pip in the build virtual environment."
}
& $Python -m pip install -r (Join-Path $Root "src\requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install PikaPV runtime dependencies."
}
& $Python -m pip install -r (Join-Path $Root "requirements-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install PikaPV build dependencies."
}

Push-Location $Root
try {
    & $Python -m PyInstaller --noconfirm --clean "packaging\PikaPV.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE."
    }

    if ($SkipInstaller) {
        Write-Host "Portable bundle ready: $Root\dist\PikaPV\PikaPV.exe"
        return
    }

    $IsccCandidates = @(
        $env:ISCC_EXE,
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $Iscc = $IsccCandidates | Select-Object -First 1
    if (-not $Iscc) {
        throw @"
PyInstaller bundle created, but Inno Setup 6 was not found.
Install Inno Setup 6 or set ISCC_EXE, then run build.ps1 again.
Portable bundle: $Root\dist\PikaPV\PikaPV.exe
"@
    }

    & $Iscc "/DMyAppVersion=$Version" "packaging\PikaPV.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup build failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

Write-Host "Installer ready: $Root\installer-output\PikaPV-Setup-$Version.exe"
