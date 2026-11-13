# 新电脑第一次运行前执行一次：建虚拟环境、装依赖、建表、探测数据源。
# 用法：powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    $SystemPython = (Get-Command python.exe -ErrorAction SilentlyContinue)
    if (-not $SystemPython) {
        throw "没找到 Python。请先安装 Python 3.10（64 位），安装时勾选 Add python.exe to PATH。"
    }
    Write-Host "创建虚拟环境..." -ForegroundColor Cyan
    & $SystemPython.Source -m venv .venv
}

Write-Host "安装依赖..." -ForegroundColor Cyan
& $Python -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "升级 pip 失败，继续用现有版本" -ForegroundColor Yellow }
$Requirements = if (Test-Path -LiteralPath (Join-Path $ProjectRoot "requirements.lock.txt")) { "requirements.lock.txt" } else { "requirements.txt" }
& $Python -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，检查网络或换国内镜像源（见 docs/商业化方案.md）" }

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env"))) {
    Copy-Item (Join-Path $ProjectRoot ".env.example") (Join-Path $ProjectRoot ".env")
    throw "已生成 .env，请填入 PUSHPLUS_TOKEN 后重新运行本脚本。"
}

Write-Host "建表并同步源注册表..." -ForegroundColor Cyan
& $Python -X utf8 -m app.housing.cli migrate --apply
if ($LASTEXITCODE -ne 0) { throw "住房数据迁移失败" }
& $Python -m app.housing.cli init
if ($LASTEXITCODE -ne 0) { throw "初始化失败" }

Write-Host "探测数据源..." -ForegroundColor Cyan
& $Python -m app.housing.cli sources --verify
if ($LASTEXITCODE -ne 0) {
    Write-Host "有数据源不可用。如果只是个别源失败可以继续；全部失败说明网络访问不了政府站。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "下一步：" -ForegroundColor Green
Write-Host "  1. 编辑 data\groups.json 填入群组编码"
Write-Host "  2. .\.venv\Scripts\python.exe -m app.housing.cli groups"
Write-Host "  3. powershell -ExecutionPolicy Bypass -File .\scripts\install_housing_tasks.ps1"
