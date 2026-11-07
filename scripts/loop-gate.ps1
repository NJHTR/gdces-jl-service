# 门禁汇总：deploy（内含 preflight + smoke）→ acceptance → 线上版本
# 用法： powershell -File scripts\loop-gate.ps1
# 输出： docs/state/last-gate.json
param(
  [string]$Remote = "rokid-r640",
  [string]$RemoteDir = "/home/baize/jl-service"
)

$Local = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Local
$ErrorActionPreference = "Continue"

$result = [ordered]@{
  head       = (git rev-parse --short HEAD)
  deploy     = -1
  acceptance = -1
  deployed   = ""
}

Write-Host "=== gate 1/2) deploy（含 preflight + smoke）===" -ForegroundColor Cyan
powershell -NoProfile -ExecutionPolicy Bypass -File "$Local\scripts\deploy.ps1"
$result.deploy = $LASTEXITCODE
$result.preflight = if ($result.deploy -eq 0) { 0 } else { 1 }
$result.smoke = $result.deploy

Write-Host "=== gate 3) acceptance ===" -ForegroundColor Cyan
$cmd = "cd $RemoteDir && python3 scripts/acceptance.py > /tmp/acc.out 2>&1; echo EXIT=`$?; tail -16 /tmp/acc.out"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cmd))
$out = ssh -o ConnectTimeout=300 $Remote "echo $b64 | base64 -d | bash -s" 2>&1 | Out-String
Write-Host $out.TrimEnd()
if ($out -match "EXIT=(\d+)") { $result.acceptance = [int]$Matches[1] }

Write-Host "=== gate 4) 线上版本 ===" -ForegroundColor Cyan
$dep = ssh -o ConnectTimeout=60 $Remote "head -1 $RemoteDir/runtime/DEPLOYED" 2>&1 | Out-String
$result.deployed = $dep.Trim()
Write-Host ("  DEPLOYED: " + $result.deployed)

$json = $result | ConvertTo-Json -Compress
$json | Out-File -Encoding UTF8 "$Local\docs\state\last-gate.json"
Write-Host ("=== 汇总: " + $json) -ForegroundColor Green

if ($result.deploy -ne 0 -or $result.acceptance -ne 0) { exit 1 }
exit 0
