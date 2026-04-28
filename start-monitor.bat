@echo off
:loop
echo [%date% %time%] Starting monitor...
python "%~dp0monitor.py"
echo [%date% %time%] Monitor exited, restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
