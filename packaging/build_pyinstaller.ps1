Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

python -m pip install -e ".[packaging]"
python -m PyInstaller `
  --name QRISRsyncManager `
  --windowed `
  --onefile `
  app\main.py

Write-Host "Built dist\QRISRsyncManager.exe"

