# 门禁汇总：deploy（内含 preflight + smoke）→ acceptance → 线上版本
# 硬要求：head、deployed、acceptance 必须都 OK；旧门禁不能签新代码。
# 输出： docs/state/last-gate.json
param(
  [string]$Remote = "rokid-r640",
  [string]$RemoteDir = "/home/baize/jl-service"
)

$Local = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Local
$ErrorActionPreference = "Continue"

$head = (git rev-parse --short HEAD)
$result = [ordered]@{
  head          = $head
  deployed_head = ""
  match         = $false
  preflight     = -1
  deploy        = -1
  smoke         = -1
  acceptance    = -1
}

Write-Host "=== gate 1/2) deploy（含 preflight + smoke）===" -ForegroundColor Cyan
powershell -NoProfile -ExecutionPolicy Bypass -File "$Local\scripts\deploy.ps1"
$result.deploy = $LASTEXITCODE
$result.preflight = if ($result.deploy -eq 0) { 0 } else { 1 }
$result.smoke = $result.deploy

Write-Host "=== gate 3) acceptance ===" -ForegroundColor Cyan
# 单引号 here-string：bash 的 $? 不能被 PowerShell 展开
$cmd = @'
cd __DIR__
python3 scripts/acceptance.py > /tmp/acc.out 2>&1
echo EXIT=$?
tail -16 /tmp/acc.out
'@
$cmd = $cmd -replace '__DIR__', $RemoteDir
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cmd))
$out = ssh -o ConnectTimeout=300 $Remote "echo $b64 | base64 -d | bash -s" 2>&1 | Out-String
Write-Host $out.TrimEnd()
if ($out -match "EXIT=(\d+)") { $result.acceptance = [int]$Matches[1] }

Write-Host "=== gate 4) 线上版本必须等于 HEAD ===" -ForegroundColor Cyan
$dep = (ssh -o ConnectTimeout=60 $Remote "head -1 $RemoteDir/runtime/DEPLOYED" 2>&1 | Out-String).Trim()
$result.deployed_head = ($dep -split "\s+")[0]
$result.match = ($result.deployed_head -eq $head)
Write-Host ("  HEAD=" + $head + "  DEPLOYED=" + $result.deployed_head + "  match=" + $result.match)

$json = $result | ConvertTo-Json -Compress
[IO.File]::WriteAllText("$Local\docs\state\last-gate.json", $json, (New-Object Text.UTF8Encoding($false)))
Write-Host ("=== 汇总: " + $json) -ForegroundColor Green

if ($result.deploy -ne 0 -or $result.acceptance -ne 0 -or -not $result.match) { exit 1 }
exit 0
