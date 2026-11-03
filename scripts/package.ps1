# 本地发行打包入口，可显式跳过已经完成的前端构建。
param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
# 默认先验证前端可以构建，再交给 Python 生成并核验发行包。
if (-not $SkipBuild) {
    Push-Location -LiteralPath 'frontend'
    try { npm run build; if ($LASTEXITCODE -ne 0) { throw '构建失败' } } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath 'frontend\dist\index.html')) { throw '缺少前端静态构建' }
$releaseDir = Join-Path $projectRoot 'dist'
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
$outputPath = Join-Path $releaseDir 'StockAI-2.0.0-Windows.zip'
# 由统一打包逻辑筛选源码、排除本地数据，并生成完整性校验文件。
& '.\.venv\Scripts\python.exe' -X utf8 scripts\package.py
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
Write-Host $outputPath
