# 安装本地或仅浏览器环境，可按需重新构建前端。
param([switch]$WebOnly, [switch]$RebuildUI)
$ErrorActionPreference = 'Stop'
# 以脚本位置定位项目，临时文件和依赖缓存统一放入项目临时目录。
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
New-Item -ItemType Directory -Path (Join-Path $projectRoot '.tmp') -Force | Out-Null
$env:TEMP = Join-Path $projectRoot '.tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $env:TEMP 'pip-cache'
$env:npm_config_cache = Join-Path $env:TEMP 'npm-cache'
# 首次安装时选择受支持的标准 Python，并创建项目专属虚拟环境。
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    $pythonVersion = $null
    foreach ($candidate in @('3.13', '3.12', '3.11')) {
        & py "-V:$candidate" --version 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonVersion = $candidate; break }
    }
    if (-not $pythonVersion) { throw '请先安装标准版 Python 3.11/3.12/3.13（不使用 free-threaded 版本）。' }
    & py "-V:$pythonVersion" -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 环境创建失败，需要 Python 3.11 或更新版本。' }
}
# 按客户端模式选择后端或桌面依赖清单，安装失败立即退出。
$requirementFile = if ($WebOnly) { 'requirements.txt' } else { 'requirements-desktop.txt' }
& '.\.venv\Scripts\python.exe' -m pip install -r $requirementFile
if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。请检查网络或 pip 源。' }
# 仅在用户要求或缺少静态资源时安装前端依赖并执行构建。
if ($RebuildUI -or -not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
    Push-Location -LiteralPath 'frontend'
    try {
        npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    } finally { Pop-Location }
}
# 依赖及前端准备成功后写入安装标记，供启动脚本判断。
Set-Content -LiteralPath '.venv\stockai-web-installed' -Value '2.0.0' -Encoding ascii
if (-not $WebOnly) { Set-Content -LiteralPath '.venv\stockai-desktop-installed' -Value '2.0.0' -Encoding ascii }
Write-Host 'StockAI 安装完成。双击 Start-StockAI.cmd 打开桌面端，或 Start-Web.cmd 打开浏览器。'
