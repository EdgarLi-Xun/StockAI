# 停止本地共享服务，先检查 PID 对应的命令行身份。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot 'data\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host 'StockAI 服务未运行。'; exit }
# 从服务记录读取 PID，并查询该进程实际启动命令。
$serverPid = [int](Get-Content -LiteralPath $pidFile)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid"
# 只有命令行同时匹配本项目启动器与服务参数时才停止进程。
if ($process -and $process.CommandLine.Contains((Join-Path $projectRoot 'run.py')) -and $process.CommandLine.Contains('--serve')) {
    Stop-Process -Id $serverPid
    Write-Host 'StockAI 服务已停止。'
} elseif ($process) { throw '进程信息与 StockAI 不匹配，未停止任何进程。' }
