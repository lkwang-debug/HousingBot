param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("scan", "digest", "remind")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "虚拟环境不存在，请先运行 scripts\setup.ps1"
}
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$LogDirectory = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$LogPath = Join-Path $LogDirectory ("task-{0}-{1}.log" -f $Mode, (Get-Date -Format "yyyy-MM-dd"))
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value ("START {0:o} mode={1}" -f (Get-Date), $Mode)

$RunExitCode = 1
try {
    $ErrorActionPreference = "Continue"
    switch ($Mode) {
    "digest" { & $Python -X utf8 -m app.housing.cli digest --deliver --if-due 2>&1 | Out-File -LiteralPath $LogPath -Append -Encoding UTF8 }
    "remind" { & $Python -X utf8 -m app.housing.cli evening --deliver --if-due 2>&1 | Out-File -LiteralPath $LogPath -Append -Encoding UTF8 }
    default { & $Python -X utf8 -m app.housing.cli scan --deliver 2>&1 | Out-File -LiteralPath $LogPath -Append -Encoding UTF8 }
    }
    $RunExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = "Stop"
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value ("END {0:o} exit={1}" -f (Get-Date), $RunExitCode)
}
exit $RunExitCode
