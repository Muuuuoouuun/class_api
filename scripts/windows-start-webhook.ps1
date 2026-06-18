param(
  [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

$LocalExe = Join-Path $ProjectRoot ".venv\Scripts\classin-webhook.exe"
if (Test-Path $LocalExe) {
  & $LocalExe
  exit $LASTEXITCODE
}

& classin-webhook
exit $LASTEXITCODE
