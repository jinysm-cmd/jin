@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Paper trading (fake money, real prices). Close this window to stop.
python run_bot.py --mode paper
pause
