# 注册或取消当前用户登录时启动共享后端的计划任务。
param([switch]$Unregister)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$taskName = 'StockAI Local Research Service'
# 取消注册只影响启动任务，不删除研究数据或配置。
if ($Unregister) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'StockAI 登录启动任务已取消。'
    exit
}
$pythonPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先安装 StockAI。' }
# 计划任务只负责启动常驻服务，研究时间表由后端唯一内建调度器管理。
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + (Join-Path $projectRoot 'run.py') + '" --serve') -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description '仅在登录后启动本地服务；研究计划由唯一内建调度器执行。' -Force | Out-Null
Write-Host '已注册登录启动。08:00、16:00与周日20:00仅由内建调度器触发。需保持登录且主机唤醒。'
