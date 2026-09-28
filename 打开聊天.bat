@echo off
chcp 936 >nul
title Code Review Agent - 交互聊天
cd /d "%~dp0"

rem 解释器选择：优先项目虚拟环境，回退到系统 Python
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python。
    echo 请先安装 Python 3.10+，然后在本目录执行: pip install -r requirements.txt
    pause
    exit /b 1
)

echo 进入交互式审查（输入 /exit 退出，/reset 重置会话）...
"%PY%" main.py chat
echo.
pause
