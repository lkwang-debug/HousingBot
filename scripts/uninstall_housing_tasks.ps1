$ErrorActionPreference = "Stop"

Get-ScheduledTask -TaskName "HousingBot-*" -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    Write-Host "已移除 $($_.TaskName)"
}

Get-ScheduledTask -TaskName "HousingBot-*" -ErrorAction SilentlyContinue | Select-Object TaskName, State
