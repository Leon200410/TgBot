@echo off
chcp 65001 >nul
cd /d "%~dp0"
python tts_server.py
echo.
pause
