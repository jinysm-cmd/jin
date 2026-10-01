@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Running backtest (top 15 coins, last 120 days). First run downloads data and takes 10-30 minutes...
python backtest.py --days 120 > backtest_result.txt 2>&1
type backtest_result.txt
echo.
echo *** Result saved to backtest_result.txt ***
pause
