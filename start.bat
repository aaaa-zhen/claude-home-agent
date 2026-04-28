@echo off
chcp 65001 >nul
cd /d "%USERPROFILE%\weixin-agent"

:: 初始模型：从参数或 model.txt 或默认 sonnet
if "%~1" neq "" (
    echo %~1> model.txt
)
if not exist model.txt echo sonnet> model.txt

echo ========================================
echo   weixin-acp 守护脚本 (Windows)
echo   关闭此窗口即停止服务
echo ========================================

:: 内网地址不走代理
set no_proxy=192.168.3.6,localhost,127.0.0.1,::1,.local
set NO_PROXY=%no_proxy%

set COUNT=0

:loop
set /a COUNT+=1
set /p CLAUDE_MODEL=<model.txt
echo.
echo [%date% %time%] 第 %COUNT% 次启动 (模型: %CLAUDE_MODEL%)...
echo %date% %time%> session-start.txt
call npx weixin-acp claude-code
echo [%date% %time%] 进程退出，5秒后重启...
timeout /t 5 /nobreak >nul
goto loop
