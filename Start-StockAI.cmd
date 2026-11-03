@echo off
rem 切换到脚本所在项目目录，支持从快捷方式或其他工作目录启动。
cd /d "%~dp0"
rem 缺少桌面安装标记时先安装依赖，安装失败则停止启动。
if not exist ".venv\stockai-desktop-installed" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
rem 使用无控制台 Python 打开桌面客户端，并复用本地共享服务。
start "" ".venv\Scripts\pythonw.exe" "%~dp0run.py" --desktop
