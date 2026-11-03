# 先停止服务并移除登录启动；只有指定参数时才清理可重装依赖。
[CmdletBinding(SupportsShouldProcess, ConfirmImpact='High')]
param([switch]$RemoveRuntime)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
& (Join-Path $PSScriptRoot 'stop.ps1')
& (Join-Path $PSScriptRoot 'register-startup.ps1') -Unregister
# 逐项解析依赖目录的绝对路径，并在删除前核对仍位于项目根目录内。
if ($RemoveRuntime) {
    foreach ($relativePath in @('.venv', 'frontend\node_modules')) {
        $targetPath = [IO.Path]::GetFullPath((Join-Path $projectRoot $relativePath))
        if (-not $targetPath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '拒绝移除项目目录以外的内容。' }
        # 保留 ShouldProcess 确认流程，仅删除已验证范围内的运行依赖。
        if ((Test-Path -LiteralPath $targetPath) -and $PSCmdlet.ShouldProcess($targetPath, '移除可重新安装的运行依赖')) {
            Remove-Item -LiteralPath $targetPath -Recurse -Force
        }
    }
}
Write-Host '已停止服务并取消登录启动。数据库、配置、研究档案与备份全部保留在 data 文件夹。'
