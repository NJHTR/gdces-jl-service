# 部署（唯一入口）
#
# 用法（本地 PowerShell）：
#   .\scripts\deploy.ps1              # 全部：检查 → 传代码 → 重启 → preflight → 健康
#   .\scripts\deploy.ps1 -SkipSmoke   # 不跑端到端（省 1 分钟）
#
# 它守的规矩见 docs/DEPLOY.md：
#   代码只从本地流向服务器；服务器不许手改；部署必须能说出「线上是哪个提交」。

param(
  [string]$Remote = "rokid-r640",
  [string]$RemoteDir = "/home/baize/jl-service",
  [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
$Local = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Local

Write-Host "=== 1) 检查本地 ===" -ForegroundColor Cyan
$dirty = (git status --short | Measure-Object).Count
if ($dirty -gt 0) {
  Write-Host "  工作区有 $dirty 处未提交改动 —— 先提交再部署（服务器上跑的必须是 git 里的代码）" -ForegroundColor Red
  git status --short | ForEach-Object { "    $_" }
  exit 1
}
$hash = (git rev-parse --short HEAD).Trim()
$subject = (git log -1 --pretty=%s).Trim()
Write-Host "  提交 $hash  $subject"

Write-Host "=== 2) 传代码 ===" -ForegroundColor Cyan
$dirs = @("bff", "scripts", "data/knowledge")
foreach ($d in $dirs) {
  for ($i = 1; $i -le 6; $i++) {
    scp -r -o ConnectTimeout=30 "$Local\$($d -replace '/','\')" "${Remote}:$RemoteDir/$(Split-Path $d -Parent)/" 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "  OK $d"; break }
    if ($i -eq 6) { Write-Host "  失败 $d" -ForegroundColor Red; exit 1 }
    Start-Sleep -Seconds 3
  }
}

Write-Host "=== 3) 记下部署的提交 ===" -ForegroundColor Cyan
$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
$cmd = "cd $RemoteDir && mkdir -p runtime && printf '%s  %s\n%s\n' '$hash' '$stamp' '$subject' > runtime/DEPLOYED && cat runtime/DEPLOYED"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cmd))
$out = ssh -o ConnectTimeout=60 $Remote "echo $b64 | base64 -d | bash -s" 2>&1 | Out-String
$out.Trim().Split("`n") | ForEach-Object { "  $_" }

Write-Host "=== 4) 重启 BFF ===" -ForegroundColor Cyan
$cmd = @"
cd $RemoteDir
pkill -9 -f 'runtime/entry.py' 2>/dev/null || true
sleep 2
scripts/start-bff.sh | tail -2
sleep 3
curl -s -m 5 http://127.0.0.1:8081/healthz; echo
"@
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($cmd -replace "`r`n", "`n")))
$out = ssh -o ConnectTimeout=200 $Remote "echo $b64 | base64 -d | bash -s" 2>&1 | Out-String
$out.Trim().Split("`n") | ForEach-Object { "  $_" }

Write-Host "=== 5) preflight ===" -ForegroundColor Cyan
$cmd = "cd $RemoteDir && python3 scripts/preflight.py 2>&1 | tail -8"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cmd))
$out = ssh -o ConnectTimeout=200 $Remote "echo $b64 | base64 -d | bash -s" 2>&1 | Out-String
$ok = $out -match "全部通过"
$out.Trim().Split("`n") | ForEach-Object { "  $_" }

if (-not $SkipSmoke) {
  Write-Host "=== 6) 端到端 ===" -ForegroundColor Cyan
  $cmd = "cd $RemoteDir && python3 scripts/smoke-e2e.py 2>&1 | tail -6; echo EXIT=`${PIPESTATUS[0]}"
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cmd))
  $out = ssh -o ConnectTimeout=900 $Remote "echo $b64 | base64 -d | bash -s" 2>&1 | Out-String
  $out.Trim().Split("`n") | ForEach-Object { "  $_" }
}

Write-Host ""
if ($ok) {
  Write-Host "部署完成：线上 = $hash" -ForegroundColor Green
  exit 0
}
Write-Host "部署完成但 preflight 未全绿 —— 看上面输出" -ForegroundColor Yellow
exit 1
