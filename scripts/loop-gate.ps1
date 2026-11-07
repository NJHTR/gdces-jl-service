# 门禁汇总：本地跑 preflight + deploy（含 smoke），服务器跑 acceptance，输出 JSON
#
# 用法： powershell -File scripts\loop-gate.ps1
# 输出： docs/state/last-gate.json  {preflight, deploy, smoke, acceptance, head, deployed}
param(
  [string]$Remote = "rokid-r640",
  [string]$RemoteDir = "/home/baize/jl-service"
)

$Local = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Local
$result = @{ head = (git rev-parse --short HEAD); preflight = -1; deploy = -1; acceptance = -1 }
$ErrorActionPreference = "Continue"

Write-Host "=== gate 1) preflight ===" -ForegroundColor Cyan
python "$Local\scripts\preflight.py" 2>&1 | Select-Object -Last 2
$result.preflight = $LASTEXITCODE

Write-Host "=== gate 2) deploy（含 smoke）===" -ForegroundColor Cyan
powershell -NoProfile -ExecutionPolicy Bypass -File "$Local\scripts\deploy.ps1" 2>&1 | Select-Object -Last 6
$result.deploy = $LASTEXITCODE

Write-Host "=== gate 3) acceptance ===" -ForegroundColor Cyan
$cmd = "cd $RemoteDir && python3 scripts/acceptance.py 2>&1 | tail -14"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cmd))
$out = ssh -o ConnectTimeout=300 $Remote "echo $b64 | base64 -d | bash -s" 2>&1 | Out-String
$result.acceptance = if ($out -match "✗") { 1 } else { 0 }
$out.Trim().Split("`n") | ForEach-Object { "  $_" }

Write-Host "=== gate 4) 线上版本 ===" -ForegroundColor Cyan
$dep = ssh -o ConnectTimeout=60 $Remote "head -1 $RemoteDir/runtime/DEPLOYED" 2>&1 | Out-String
$result.deployed = $dep.Trim()
"  DEPLOYED: $($result.deployed)"

$result.smoke = $result.deploy   # deploy 内含 smoke；分开只为回执字段完整
$result | ConvertTo-Json -Compress | Out-File -Encoding UTF8 "$Local\docs\state\last-gate.json"
Write-Host ("=== 汇总: " + ($result | ConvertTo-Json -Compress)) -ForegroundColor Green
if ($result.preflight -ne 0 -or $result.deploy -ne 0 -or $result.acceptance -ne 0) { exit 1 }
exit 0
