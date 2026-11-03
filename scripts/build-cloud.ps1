# 构建公网前端静态资源，输出目录由 Vite 的 public 模式决定。
$ErrorActionPreference = 'Stop'
$TaskRoot = Split-Path -Parent $PSScriptRoot
# 暂时进入前端目录，确保成功或失败后都恢复原工作目录。
Push-Location (Join-Path $TaskRoot 'frontend')
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
    npm run build:public
    if ($LASTEXITCODE -ne 0) { throw '公网前端构建失败' }
} finally { Pop-Location }
Write-Host '公网前端已构建。Linux 部署请参阅 deploy/cloud/README.md。'
