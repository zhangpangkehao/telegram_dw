@echo off
cd /d "%~dp0"
echo ============================================
echo   tdl GUI - Telegram Downloader
echo ============================================
echo.
python server.py
echo.
echo Server stopped. Press any key to exit.
pause >nul
