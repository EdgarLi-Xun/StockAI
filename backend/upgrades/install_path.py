"""Per-user desktop install location. Research data stays in the workspace."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess


def install_dir():
    base = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local'))
    return base / 'Programs' / 'StockAI'


def install_exe():
    return install_dir() / 'StockAI.exe'


def program_path(value):
    if value.get('layout') in ('setup', 'installed'):
        return install_exe()
    return Path(value['path'])


def silent_install(setup, directory):
    """Install a verified setup into an empty directory. Does not start the app."""
    from backend.processes import hidden_process
    from .trust import UpgradeError
    directory.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run([str(setup), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/DIR=' + str(directory)], timeout=900, **hidden_process())
    if completed.returncode or not (directory / 'StockAI.exe').is_file():
        raise UpgradeError('PACKAGE', '安装程序没有完成')


def schedule_swap(parent, staging, arguments):
    """After this process exits, move the staged app into the install directory and start it."""
    from backend.processes import hidden_process
    from .trust import UpgradeError
    staging = Path(staging)
    if not (staging / 'StockAI.exe').is_file():
        raise UpgradeError('PACKAGE', '暂存的桌面程序不存在')
    live = install_dir()
    folder = staging.parent
    backup = folder / 'previous-app'
    argfile = folder / 'restart-args.txt'
    script = folder / 'swap.ps1'
    argfile.write_text('\n'.join(arguments), encoding='utf-8-sig')
    script.write_text(
        "param([int]$Parent,[string]$Staging,[string]$Live,[string]$Backup,[string]$Exe,[string]$ArgFile)\n"
        "Wait-Process -Id $Parent -ErrorAction SilentlyContinue\n"
        "Start-Sleep -Seconds 1\n"
        "if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Recurse -Force }\n"
        "$moved = $false\n"
        "if (Test-Path -LiteralPath $Live) { Move-Item -LiteralPath $Live -Destination $Backup; $moved = $true }\n"
        "try { Move-Item -LiteralPath $Staging -Destination $Live }\n"
        "catch {\n"
        "  if ($moved -and -not (Test-Path -LiteralPath $Live) -and (Test-Path -LiteralPath $Backup)) { Move-Item -LiteralPath $Backup -Destination $Live }\n"
        "  exit 1\n"
        "}\n"
        "$lines = @()\n"
        "if (Test-Path -LiteralPath $ArgFile) { $lines = @(Get-Content -LiteralPath $ArgFile -Encoding UTF8) }\n"
        "if ($lines.Count -gt 0) { Start-Process -FilePath $Exe -ArgumentList $lines } else { Start-Process -FilePath $Exe }\n"
        "if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Recurse -Force }\n",
        encoding='ascii')
    subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script), str(parent), str(staging), str(live), str(backup), str(live / 'StockAI.exe'), str(argfile)], **hidden_process())
