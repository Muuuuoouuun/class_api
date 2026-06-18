param(
  [Parameter(Mandatory = $true)]
  [string]$TunnelName,
  [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
  [string]$TaskPrefix = "ClassIn Toolkit"
)

$ErrorActionPreference = "Stop"

$WebhookScript = Join-Path $ProjectRoot "scripts\windows-start-webhook.ps1"
$TunnelScript = Join-Path $ProjectRoot "scripts\windows-start-tunnel.ps1"

if (-not (Test-Path $WebhookScript)) {
  throw "Webhook 시작 스크립트를 찾을 수 없습니다: $WebhookScript"
}
if (-not (Test-Path $TunnelScript)) {
  throw "Tunnel 시작 스크립트를 찾을 수 없습니다: $TunnelScript"
}

$PowerShell = (Get-Command powershell.exe).Source
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1)

$WebhookAction = New-ScheduledTaskAction `
  -Execute $PowerShell `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WebhookScript`" -ProjectRoot `"$ProjectRoot`""

$TunnelAction = New-ScheduledTaskAction `
  -Execute $PowerShell `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$TunnelScript`" -TunnelName `"$TunnelName`" -ProjectRoot `"$ProjectRoot`""

Register-ScheduledTask `
  -TaskName "$TaskPrefix Webhook Receiver" `
  -Action $WebhookAction `
  -Trigger $Trigger `
  -Settings $Settings `
  -Description "ClassIn Toolkit FastAPI webhook receiver" `
  -Force | Out-Null

Register-ScheduledTask `
  -TaskName "$TaskPrefix Cloudflare Tunnel" `
  -Action $TunnelAction `
  -Trigger $Trigger `
  -Settings $Settings `
  -Description "ClassIn Toolkit Cloudflare named tunnel" `
  -Force | Out-Null

Write-Host "등록 완료:"
Write-Host "  - $TaskPrefix Webhook Receiver"
Write-Host "  - $TaskPrefix Cloudflare Tunnel"
Write-Host ""
Write-Host "확인:"
Write-Host "  Get-ScheduledTask -TaskName '$TaskPrefix*'"
