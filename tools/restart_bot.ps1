<#
Restart the Polymarket sweep-lock bot:
  1) stop the process listening on the dashboard port
  2) calibrate the paper ledger (idempotent)
  3) start a fresh process with the current code
  4) health check

Run it from an ELEVATED PowerShell (the bot usually runs elevated, so a
non-elevated shell cannot stop it):

    powershell -ExecutionPolicy Bypass -File tools\restart_bot.ps1
    powershell -ExecutionPolicy Bypass -File tools\restart_bot.ps1 -Hidden
    powershell -ExecutionPolicy Bypass -File tools\restart_bot.ps1 -DryRun

All output is ASCII on purpose (Windows PowerShell 5.1 decodes BOM-less
UTF-8 scripts with the ANSI codepage, which can corrupt non-ASCII text).
#>
param(
    [switch]$Hidden,
    [switch]$DryRun,
    [int]$Port = 8082,
    [int]$StartupWaitSeconds = 15
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Write-Host "project root : $root"
Write-Host "dashboard    : http://127.0.0.1:$Port"

function Get-BotProcessId {
    param([int]$ListenPort)
    $conn = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return 0
}

# ---------- 1) stop the old process ----------
$oldPid = Get-BotProcessId -ListenPort $Port
if ($oldPid -gt 0) {
    Write-Host "step 1/4 : stopping old process PID=$oldPid"
    if (-not $DryRun) {
        Stop-Process -Id $oldPid -Force
        $freed = $false
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Seconds 1
            if ((Get-BotProcessId -ListenPort $Port) -eq 0) { $freed = $true; break }
        }
        if (-not $freed) { throw "port $Port is still in use after 10s; aborting" }
        Write-Host "           port $Port released"
    }
}
else {
    Write-Host "step 1/4 : nothing listening on $Port (already stopped)"
}

# ---------- 2) calibrate the paper ledger (idempotent) ----------
Write-Host "step 2/4 : calibrating paper ledger (idempotent)"
if (-not $DryRun) {
    try {
        & python tmp\recalc_paper_cash.py --apply
    }
    catch {
        Write-Host "           WARNING: ledger recalibration failed: $_"
    }
}

# ---------- 2b) repair settlement prices (idempotent) ----------
# 只在机器人**已经停掉**的窗口里改账本：它跑着的时候会把内存里的旧账本写回来，
# 我们 2026-09-16 的修正就是这样被覆盖过一次。脚本幂等，没毛病时什么都不做；
# 网络不通会跳过（不会误改），只是修不了。
Write-Host "step 2b/4: checking settlement prices (idempotent)"
foreach ($ledger in @('paper', 'probability')) {
    if ($DryRun) {
        Write-Host "           (dry run: skipped $ledger)"
        continue
    }
    try {
        & python tmp\fix_settlement_prices.py --apply --ledger $ledger
    }
    catch {
        Write-Host "           WARNING: settlement price check failed ($ledger): $_"
    }
}

# ---------- 3) start a fresh process ----------
Write-Host "step 3/4 : starting bot"
if (-not $DryRun) {
    if ($Hidden) {
        Start-Process -FilePath 'python' -ArgumentList '-m', 'src.main' -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        Write-Host "           started hidden; logs go to logs\bot.log"
    }
    else {
        Start-Process -FilePath 'python' -ArgumentList '-m', 'src.main' -WorkingDirectory $root | Out-Null
        Write-Host "           started in a new window (watch the live log there)"
    }
}

# ---------- 4) health check ----------
Write-Host "step 4/4 : health check (waiting ${StartupWaitSeconds}s for startup)"
if (-not $DryRun) {
    Start-Sleep -Seconds $StartupWaitSeconds
    $status = $null
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 10
        Write-Host "           /api/status OK  mode=$($status.mode)  cash=$($status.summary.cash)"
    }
    catch {
        Write-Host "           ERROR: dashboard not responding: $_"
    }
    try {
        $live = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/live-trader" -TimeoutSec 10
        Write-Host "           live: armed=$($live.armed) enabled=$($live.enabled) dry_run=$($live.dry_run) orders=$($live.total_orders) open=$($live.open_positions)"
        Write-Host "           daily spent=$($live.daily_spent_usdc) / $($live.max_daily_spend_usdc)  reconcile=$($live.reconcile | ConvertTo-Json -Compress)"
    }
    catch {
        Write-Host "           NOTE: /api/live-trader not ready yet: $_"
    }
}
else {
    Write-Host "           (dry run: skipped)"
}

Write-Host "done."
