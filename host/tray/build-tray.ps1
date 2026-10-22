<#
.SYNOPSIS
  Builds host\tray\dist\ClawdTank\ClawdTank.exe (Clawd Tank for Windows).

.DESCRIPTION
  1. creates host\.venv-tray if it is not there (py -3 -m venv) and installs
     host\tray\requirements.txt into it (never into the global Python);
  2. runs make_icons.py: decodes clawd-tank's sprite headers into the PNG and .ico
     assets of the app;
  3. runs PyInstaller on ClawdTank.spec (onedir, windowed).

  Run it from anywhere:   powershell -File host\tray\build-tray.ps1
  Add -Clean to remove host\tray\build and host\tray\dist first.
#>
param([switch]$Clean)

$ErrorActionPreference = 'Stop'
$trayDir = $PSScriptRoot
$hostDir = Split-Path $trayDir -Parent
$venv = Join-Path $hostDir '.venv-tray'
$python = Join-Path $venv 'Scripts\python.exe'

if ($Clean) {
    foreach ($d in 'build', 'dist') {
        $p = Join-Path $trayDir $d
        if (Test-Path $p) { Remove-Item $p -Recurse -Force }
    }
}

if (-not (Test-Path $python)) {
    Write-Host "Creating the virtual environment: $venv"
    & py -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "py -3 -m venv failed (is the Python launcher installed?)" }
}

Write-Host 'Installing requirements into the venv'
& $python -m pip install --disable-pip-version-check --quiet -r (Join-Path $trayDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

Write-Host 'Building the Clawd icons from the sprite headers'
& $python (Join-Path $trayDir 'make_icons.py')
if ($LASTEXITCODE -ne 0) { throw 'make_icons.py failed' }

Write-Host 'Running PyInstaller'
Push-Location $trayDir
try {
    & $python -m PyInstaller ClawdTank.spec --noconfirm --distpath dist --workpath build
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
} finally {
    Pop-Location
}

$exe = Join-Path $trayDir 'dist\ClawdTank\ClawdTank.exe'
if (-not (Test-Path $exe)) { throw "the build finished but $exe is missing" }
$folder = Get-ChildItem (Join-Path $trayDir 'dist\ClawdTank') -Recurse -File |
    Measure-Object -Property Length -Sum
Write-Host ''
Write-Host ('Built {0}  ({1:N1} MB exe, {2:N1} MB folder)' -f $exe,
    ((Get-Item $exe).Length / 1MB), ($folder.Sum / 1MB))
Write-Host 'Check it without Bluetooth:  ClawdTank.exe --selftest'
