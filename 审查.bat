@echo off
chcp 936 >nul
title Code Review Agent - 代码审查
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

rem 支持两种用法：拖拽文件/文件夹到图标上（自动带入路径），或双击后手动输入
set "TARGET=%~1"
if "%TARGET%"=="" (
    set /p TARGET=请输入要审查的文件或目录路径（也可直接把文件拖到本图标上）:
)
if "%TARGET%"=="" (
    echo 未输入路径，已取消。
    pause
    exit /b 1
)

"%PY%" main.py review "%TARGET%"
echo.
pause
