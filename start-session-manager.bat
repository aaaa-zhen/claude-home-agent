@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   Session Manager 守护脚本
echo   关闭此窗口即停止
echo ========================================

:loop
echo [%date% %time%] Starting session manager...
python "%~dp0session-manager.py"
echo [%date% %time%] Session manager exited, restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
