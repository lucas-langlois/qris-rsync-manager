$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$TempRoot = Join-Path $ProjectRoot ".test_tmp"
$BaseTemp = Join-Path $TempRoot "basetemp"
$ProjectPython = Join-Path $ProjectRoot "envs\qris-rsync-manager\python.exe"

New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null

$env:TEMP = $TempRoot
$env:TMP = $TempRoot

if (Test-Path $ProjectPython) {
    & $ProjectPython -m pytest -q -p no:cacheprovider --basetemp=$BaseTemp @args
} else {
    & python -m pytest -q -p no:cacheprovider --basetemp=$BaseTemp @args
}

exit $LASTEXITCODE
