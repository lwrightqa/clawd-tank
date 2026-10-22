<#
.SYNOPSIS
  Builds host\tray\dist\ClawdTank\ClawdTank.exe (Clawd Tank for Windows).

.DESCRIPTION
  1. creates host\.venv-tray if it is not there (py -3 -m venv) and installs
     host\tray\requirements.txt into it (never into the global Python);
  2. runs make_icons.py: decodes clawd-tank's sprite header into the PNG and .ico
     assets of the app;
  3. runs PyInstaller on ClawdTank.spec (onedir, windowed).

  Run it from anywhere:   powershell -File host\tray\build-tray.ps1
  Add -Clean to remove the output and work folders of THIS build first.

  -OutDir builds somewhere else, for example next to a copy that is running:
      powershell -File host\tray\build-tray.ps1 -OutDir dist-next
  A relative -OutDir is relative to host\tray. The work folder defaults to
  build for dist, and to build-<name of OutDir> otherwise; -WorkDir overrides it.
  Nothing outside the chosen folders is touched. If the ClawdTank.exe that would
  be overwritten is running, the script stops before building and says so.
#>
param([switch]$Clean, [string]$OutDir = 'dist', [string]$WorkDir = '')

$ErrorActionPreference = 'Stop'
$trayDir = $PSScriptRoot
$hostDir = Split-Path $trayDir -Parent
$venv = Join-Path $hostDir '.venv-tray'
$python = Join-Path $venv 'Scripts\python.exe'

function Resolve-Under($base, $path) {
    if ([IO.Path]::IsPathRooted($path)) { return $path }
    return Join-Path $base $path
}
$dist = Resolve-Under $trayDir $OutDir
if (-not $WorkDir) {
    $WorkDir = if ($OutDir -eq 'dist') { 'build' } else { 'build-' + (Split-Path $dist -Leaf) }
}
$work = Resolve-Under $trayDir $WorkDir
$exe = Join-Path $dist 'ClawdTank\ClawdTank.exe'

$running = @(Get-Process ClawdTank -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ($_.Path -ieq $exe) })
if ($running.Count -gt 0) {
    throw ("$exe is running (pid $($running.Id -join ', ')); quit it first, " +
           "or build elsewhere with -OutDir dist-next")
}

if ($Clean) {
    foreach ($p in $dist, $work) {
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

Write-Host 'Building the Clawd icons from the sprite header'
& $python (Join-Path $trayDir 'make_icons.py')
if ($LASTEXITCODE -ne 0) { throw 'make_icons.py failed' }

Write-Host "Running PyInstaller (output $dist, work $work)"
Push-Location $trayDir
try {
    & $python -m PyInstaller ClawdTank.spec --noconfirm --distpath $dist --workpath $work
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
} finally {
    Pop-Location
}

if (-not (Test-Path $exe)) { throw "the build finished but $exe is missing" }
$folder = Get-ChildItem (Join-Path $dist 'ClawdTank') -Recurse -File |
    Measure-Object -Property Length -Sum
Write-Host ''
Write-Host ('Built {0}  ({1:N1} MB exe, {2:N1} MB folder)' -f $exe,
    ((Get-Item $exe).Length / 1MB), ($folder.Sum / 1MB))
Write-Host 'Check it without Bluetooth:  ClawdTank.exe --selftest'
