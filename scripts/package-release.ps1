# 按 backend/cloud/__init__.py 的版本，一次生成桌面安装包、公网 ZIP 与独立 Hub ZIP。
# 发行包不含数据库、凭据、虚拟环境或 .tmp。前端只构建一次，安装包脚本不再重复构建。
param(
    [ValidateSet('all', 'exe', 'public', 'hub')]
    [string]$Target = 'all',
    [switch]$SkipBuild,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '缺少 .venv。请先创建虚拟环境，并按打包说明安装构建依赖。'
}

$version = (& $python -X utf8 -c "import sys; sys.path.insert(0, '.'); from backend.cloud import VERSION; print(VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($version)) {
    throw '无法读取 backend/cloud/__init__.py 中的 VERSION'
}

$needFrontend = $Target -in @('all', 'exe', 'public')
$needExe = $Target -in @('all', 'exe')
$needPublic = $Target -in @('all', 'public')
$needHub = $Target -in @('all', 'hub')

function Find-InnoCompiler {
    $candidates = @(
        $env:INNO_SETUP_COMPILER,
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    ) | Where-Object { $_ }
    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return $null
}

Write-Host "StockAI $version    目标: $Target"

if ($needFrontend) {
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw '未找到 npm.cmd。请先安装 Node.js。'
    }
    if (-not (Test-Path -LiteralPath 'frontend\node_modules')) {
        throw '缺少 frontend\node_modules。请在 frontend 目录执行 npm ci。'
    }
}

if ($needExe) {
    $compiler = Find-InnoCompiler
    if (-not $compiler) {
        throw '缺少 Inno Setup 6 编译器 ISCC.exe。安装后可把路径写入环境变量 INNO_SETUP_COMPILER。'
    }
    Write-Host "Inno Setup: $compiler"
    foreach ($name in @('age.exe', 'age-keygen.exe')) {
        $tool = Join-Path $projectRoot ".tmp\tools\age\$name"
        if (-not (Test-Path -LiteralPath $tool)) {
            throw "缺少已校验的 age 工具：$tool"
        }
    }
    & $python -X utf8 -c "import PyInstaller, PySide6"
    if ($LASTEXITCODE -ne 0) {
        throw '虚拟环境缺少 PyInstaller 或 PySide6。请执行：.\.venv\Scripts\python.exe -m pip install -r requirements-exe.txt'
    }
}

if ($CheckOnly) {
    Write-Host '环境检查通过。未开始构建。'
    exit 0
}

if ($needFrontend -and -not $SkipBuild) {
    Write-Host '构建公网前端（frontend/dist-public）...'
    & npm.cmd --prefix frontend run build:public
    if ($LASTEXITCODE -ne 0) { throw '公网前端构建失败' }
}

if ($needFrontend -and -not (Test-Path -LiteralPath 'frontend\dist-public\index.html')) {
    throw '缺少 frontend\dist-public\index.html。去掉 -SkipBuild 重新构建，或先执行 npm --prefix frontend run build:public。'
}

function Invoke-Packager([string]$Label, [string[]]$Arguments) {
    Write-Host $Label
    & $python -X utf8 @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label 失败" }
}

if ($needExe) {
    Invoke-Packager '构建桌面安装包' @('scripts\package-exe.py', '--skip-build')
}
if ($needPublic) {
    Invoke-Packager '构建公网 ZIP' @('scripts\package-cloud.py')
}
if ($needHub) {
    Invoke-Packager '构建 Hub ZIP' @('scripts\package-hub.py')
}

Write-Host ''
Write-Host '产物：'
$names = @()
if ($needExe) { $names += "StockAI-$version-Setup.exe" }
if ($needPublic) { $names += "StockAI-$version-Public.zip" }
if ($needHub) { $names += "StockAI-$version-Hub.zip" }
foreach ($name in $names) {
    $path = Join-Path $projectRoot "dist\$name"
    $sidecar = "$path.sha256"
    if (-not (Test-Path -LiteralPath $path)) { throw "未找到产物 $path" }
    if (-not (Test-Path -LiteralPath $sidecar)) { throw "未找到校验文件 $sidecar" }
    $recorded = ((Get-Content -LiteralPath $sidecar -Raw).Trim() -split '\s+', 2)[0].ToLower()
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
    if ($recorded -ne $actual) { throw "校验文件与产物不一致：$name" }
    $size = (Get-Item -LiteralPath $path).Length
    Write-Host ("{0}  {1} 字节" -f $name, $size)
    Write-Host "  SHA256=$actual"
}
if ($needExe) {
    $meta = Join-Path $projectRoot "dist\StockAI-$version-Setup.exe.build.json"
    if (-not (Test-Path -LiteralPath $meta)) { throw "未找到安装包构建元数据 $meta" }
    Write-Host "  构建元数据: $meta"
}
