# 注册 Health System 的三个计划任务（Windows，需管理员 PowerShell）
#
# 为什么是「两同步 + 一校验」（2026-10-19 用户定）
# ------------------------------------------------
# 校验必须晚于上传，否则「本地有云端无」是必然的、天天误报。所以校验独立成档，
# 由同步任务跑完后**立即触发**（`--chain`，同进程串行，不依赖事件日志触发器）。
#
# | 任务 | 时间 | 参数 | 预计耗时 |
# |---|---|---|---|
# | HealthSystem-Activity | 每日 23:00 | `--profile activity --chain` | ~80 s |
# | HealthSystem-Health   | 每日 08:00 | `--profile health --chain`   | ~40 s |
# | HealthSystem-Verify   | 每日 21:00 | `--profile verify`（兜底 + 手动重跑） | ~40 s |
#
# 为什么 Verify 还留一个定时：两个同步任务若都失败（如断网），当日就没有校验发生；
# 21:00 的兜底任务至少留下一次「此刻状态」的快照。
#
# 用法（管理员）：
#     powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Uninstall
#
# 说明：
# - 用 `--profile` 区分；脚本用绝对路径调用 daily.cmd，工作目录设成仓库根。
# - `-StartWhenAvailable`：机器关机错过时间点时，开机后补跑。
# - `-MultipleInstances IgnoreNew`：两个同步任务同时收尾时，校验不会并发跑。
# - 旧的 `HealthSystem-DailySync`（22:30 单链）会被移除，避免与新任务重复跑。

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$cmd = Join-Path $repo "daily.cmd"

if (-not (Test-Path $cmd)) {
    throw "找不到 daily.cmd：$cmd"
}

$tasks = @(
    @{ Name = "HealthSystem-Activity"; Args = "--profile activity --chain"; Time = "23:00"; Weekly = $false },
    @{ Name = "HealthSystem-Health";   Args = "--profile health --chain";   Time = "08:00"; Weekly = $false },
    @{ Name = "HealthSystem-Verify";   Args = "--profile verify";           Time = "21:00"; Weekly = $false }
)

$legacy = "HealthSystem-DailySync"

function Remove-TaskIfExists($name) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "已移除：$name"
    }
}

if ($Uninstall) {
    foreach ($t in $tasks) { Remove-TaskIfExists $t.Name }
    Remove-TaskIfExists $legacy
    Write-Host "卸载完成。"
    exit 0
}

# 旧单链任务会与新任务重复执行，先移除
Remove-TaskIfExists $legacy

foreach ($t in $tasks) {
    Remove-TaskIfExists $t.Name

    $action = New-ScheduledTaskAction `
        -Execute $cmd `
        -Argument $t.Args `
        -WorkingDirectory $repo

    if ($t.Weekly) {
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $t.Time
    } else {
        $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
    }

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType S4U `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $t.Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Health System sync ($($t.Args))" | Out-Null

    Write-Host "已注册：$($t.Name)  $($t.Time)  $($t.Args)"
}

Write-Host ""
Write-Host "验证："
Write-Host "  Get-ScheduledTask -TaskName 'HealthSystem-*' | Select TaskName,State"
Write-Host "手动跑一次校验："
Write-Host "  Start-ScheduledTask -TaskName 'HealthSystem-Verify'"
