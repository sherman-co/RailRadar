@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Installing Python packages...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Python or pip not found. Install Python 3.10+ from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
echo.
echo Downloading bot browser (optional - Microsoft Edge is used if this fails)...
python -m playwright install chromium
echo.
echo Done. Now run run.bat
pause
