@echo off
chcp 936 >nul
title 技能名单
cd /d "%~dp0"

set "PY="
python -V >nul 2>nul
if not errorlevel 1 set "PY=python"

if not defined PY (
    py -3 -V >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
    echo.
    echo   [错误] 没有找到可用的 Python 3。
    echo.
    echo   请先安装 Python 3，下载地址 https://www.python.org/downloads/
    echo   安装时请勾选 Add Python to PATH。
    echo.
    echo   或用命令行手动运行：
    echo     python "%~dp0skillui.py"
    echo.
    pause
    exit /b 1
)

echo 正在启动技能名单界面，浏览器会自动打开。
echo 关闭本窗口即可停止服务。
echo.
%PY% skillui.py
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo 服务已停止。按任意键关闭窗口。
) else (
    echo [错误] 启动失败，错误码 %RC%，请查看上方信息。
    echo 按任意键关闭窗口。
)
pause >nul
