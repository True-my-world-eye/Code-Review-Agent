@echo off
chcp 936 >nul
title Code Review Agent - Web
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

echo 正在启动 Code Review Agent Web 服务...
start "CodeReviewAgent-Web" "%PY%" main.py web
timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8000
echo.
echo 浏览器已打开: http://127.0.0.1:8000
echo 关闭 "CodeReviewAgent-Web" 窗口即可停止服务。
timeout /t 3 >nul
exit /b 0
