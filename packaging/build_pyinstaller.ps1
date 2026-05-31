param(
  [switch]$Install
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CondaEnv = Join-Path $ProjectRoot "envs\qris-rsync-manager"
$Python = Join-Path $CondaEnv "python.exe"
$WorkPath = Join-Path $ProjectRoot "build"
$DistPath = Join-Path $ProjectRoot "dist"
$SpecPath = Join-Path $ProjectRoot "packaging\QRISRsyncManager.spec"

if (-not (Test-Path -LiteralPath $Python)) {
  throw "Project conda environment was not found at '$CondaEnv'. Create or restore it before building."
}

Set-Location -LiteralPath $ProjectRoot
New-Item -ItemType Directory -Force -Path $WorkPath | Out-Null
New-Item -ItemType Directory -Force -Path $DistPath | Out-Null

$env:CONDA_PREFIX = $CondaEnv
$env:CONDA_DEFAULT_ENV = "qris-rsync-manager"
$env:PATH = @(
  $CondaEnv
  (Join-Path $CondaEnv "Library\mingw-w64\bin")
  (Join-Path $CondaEnv "Library\usr\bin")
  (Join-Path $CondaEnv "Library\bin")
  (Join-Path $CondaEnv "Scripts")
  $env:PATH
) -join [System.IO.Path]::PathSeparator

function Invoke-ProjectPython {
  & $Python @args
  if ($LASTEXITCODE -ne 0) {
    throw "Command failed: $Python $args"
  }
}

if ($Install) {
  Invoke-ProjectPython -m pip install -e ".[packaging]"
}

Invoke-ProjectPython -c "import PyInstaller, PySide6"
Invoke-ProjectPython -m PyInstaller `
  --clean `
  --noconfirm `
  --workpath $WorkPath `
  --distpath $DistPath `
  $SpecPath

Write-Host "Built dist\QRISRsyncManager.exe"
