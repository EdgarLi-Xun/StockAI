$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot 'data\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host 'StockAI 服务未运行。'; exit }
$serverPid = [int](Get-Content -LiteralPath $pidFile)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid"
if ($process -and $process.CommandLine.Contains((Join-Path $projectRoot 'run.py')) -and $process.CommandLine.Contains('--serve')) {
    Stop-Process -Id $serverPid
    Write-Host 'StockAI 服务已停止。'
} elseif ($process) { throw '进程信息与 StockAI 不匹配，未停止任何进程。' }
