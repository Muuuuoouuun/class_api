param(
  [Parameter(Mandatory = $true)]
  [string]$TunnelName,
  [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
  Write-Error "cloudflared 를 찾을 수 없습니다. 먼저 cloudflared 를 설치하고 named tunnel 을 생성하세요."
  exit 1
}

& cloudflared tunnel run $TunnelName
exit $LASTEXITCODE
