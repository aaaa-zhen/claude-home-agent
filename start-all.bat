@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 正在后台启动 weixin-agent 三个服务...

powershell -Command "Start-Process 'C:\Users\Administrator\weixin-agent\start.bat' -WindowStyle Hidden"
powershell -Command "Start-Process 'C:\Users\Administrator\weixin-agent\start-monitor.bat' -WindowStyle Hidden"
powershell -Command "Start-Process 'C:\Users\Administrator\weixin-agent\start-session-manager.bat' -WindowStyle Hidden"

echo 已启动！窗口将在 3 秒后关闭。
timeout /t 3 /nobreak >nul
