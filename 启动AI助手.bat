@echo off
rem ===================================================================
rem  炉石传说 AI 助手 - 双击启动（悬浮窗，无浏览器）
rem  正常模式：自动找 Power.log；找不到先用示例回放，日志出现后自动切
rem ===================================================================
chcp 936 >nul
setlocal
cd /d "%~dp0"
title 炉石传说 AI 助手
set "MODE="
if /i "%~1"=="demo" set "MODE=--demo"

echo.
echo   炉石传说 AI 助手
echo   预判卡组 / 留牌建议 / 出牌建议
echo.

rem ---------- 找 Python ----------
set "PY="
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo   [错误] 没找到 Python。
    echo.
    echo   请先安装 Python 3.10 或更高版本：
    echo       https://www.python.org/downloads/
    echo   安装时务必勾选 Add Python to PATH，装完重新双击本文件。
    echo.
    pause
    exit /b 1
)
set "PYW=pythonw"
if /i not "%PY%"=="python" set "PYW=py -3w"

rem ---------- 首次运行：准备卡牌库 ----------
if not exist "data\cards_zhCN.json" (
    echo   首次运行：正在下载卡牌库，约 10 MB ...
    %PY% -m hsai cards --update
    if errorlevel 1 (
        echo.
        echo   [警告] 卡牌库没下载成功，先不启动。联网后重新双击本文件即可。
        echo.
        pause
        exit /b 1
    )
)

rem ---------- 日志开关 ----------
%PY% -m hsai setup-log --auto
rem 写不进去只警告不退出：助手仍能退回示例回放，别让日志开关挡住整个程序
if errorlevel 1 echo   [警告] 日志开关没写成功，助手会退回示例回放；请右键以管理员身份重新运行本文件。

rem ---------- 环境预检（出错会停在这里，而不是启动后什么都没出现）----------
%PY% -m hsai ui --check
if errorlevel 1 (
    echo.
    echo   [错误] 环境检查没通过，请看上面的原因。
    echo.
    pause
    exit /b 1
)

rem ---------- 启动悬浮窗 ----------
echo   正在启动悬浮窗，稍等...
start "" %PYW% -m hsai ui %MODE%
exit /b 0