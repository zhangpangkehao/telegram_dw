@echo off
cd /d "%~dp0"
echo ============================================
echo   tdl GUI - Telegram Downloader
echo ============================================
echo.
python server.py
echo.
if errorlevel 1 (
  echo Startup was not completed. Press any key to exit.
) else (
  echo Server stopped. Press any key to exit.
)
pause >nul
