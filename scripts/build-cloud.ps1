$ErrorActionPreference = 'Stop'
$TaskRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $TaskRoot 'frontend')
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
    npm run build:public
    if ($LASTEXITCODE -ne 0) { throw '公网前端构建失败' }
} finally { Pop-Location }
Write-Host '公网前端已构建。Linux 部署请参阅 deploy/cloud/README.md。'
