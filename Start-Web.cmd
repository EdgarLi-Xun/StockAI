@echo off
rem 切换到脚本所在项目目录，保证相对路径始终正确。
cd /d "%~dp0"
rem 缺少浏览器版安装标记时准备后端依赖和静态页面。
if not exist ".venv\stockai-web-installed" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -WebOnly
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
rem 后台启动共享服务入口，并通过默认浏览器打开研究界面。
start "" ".venv\Scripts\pythonw.exe" "%~dp0run.py"
