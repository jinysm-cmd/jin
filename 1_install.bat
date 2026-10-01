@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/1] Installing required packages...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo *** FAILED. Is Python installed? Did you check "Add python.exe to PATH"? ***
) else (
  echo.
  echo *** DONE. Now double-click 2_backtest.bat ***
)
pause
