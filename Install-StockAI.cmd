@echo off
rem 从发行目录调用统一安装脚本，失败时暂停窗口供查看错误。
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
if errorlevel 1 pause
