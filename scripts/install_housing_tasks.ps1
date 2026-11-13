$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$Runner = Join-Path $ProjectRoot "scripts\run_housing_scheduled.ps1"
$PowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".venv\Scripts\python.exe"))) {
    throw "请先运行 scripts\setup.ps1"
}

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# 每 30 分钟一次，07:00 起持续 15 小时 30 分钟
$ScanAt = (Get-Date).Date.AddHours(7)
$ScanTrigger = New-ScheduledTaskTrigger -Daily -At $ScanAt
$ScanTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At $ScanAt `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 15 -Minutes 30)).Repetition

$ScanAction = New-ScheduledTaskAction -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`" -Mode scan" `
    -WorkingDirectory $ProjectRoot
Register-ScheduledTask -TaskName "HousingBot-Scan" -Action $ScanAction -Trigger $ScanTrigger -Settings $Settings -Principal $Principal -Force | Out-Null

$DigestAt = (Get-Date).Date.AddHours(8).AddMinutes(30)
$DigestTrigger = New-ScheduledTaskTrigger -Daily -At $DigestAt
$DigestAction = New-ScheduledTaskAction -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`" -Mode digest" `
    -WorkingDirectory $ProjectRoot
Register-ScheduledTask -TaskName "HousingBot-Digest" -Action $DigestAction -Trigger $DigestTrigger -Settings $Settings -Principal $Principal -Force | Out-Null

# 固定分类晚报：每日概览、在途批次和截止事项；独立提醒已发过也不取消晚报。
# 20:30 是截止前一晚最后一个还能准备材料、找家人商量的时间点；09:00 会跟 08:30 早报叠在一起。
# 若以后要加回多个时间点：先用 @(...) 组成数组再传，直接写 -Trigger $A, $B 会被当成两个位置参数。
$RemindTrigger = New-ScheduledTaskTrigger -Daily -At (Get-Date).Date.AddHours(20).AddMinutes(30)
$RemindAction = New-ScheduledTaskAction -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`" -Mode remind" `
    -WorkingDirectory $ProjectRoot
Register-ScheduledTask -TaskName "HousingBot-Remind" -Action $RemindAction -Trigger $RemindTrigger -Settings $Settings -Principal $Principal -Force | Out-Null

Get-ScheduledTask -TaskName "HousingBot-*" | Select-Object TaskName, State
