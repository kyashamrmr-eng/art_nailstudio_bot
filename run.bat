@echo off
cd /d "%~dp0"
py -3.10 -m pip install -r requirements.txt
py -3.10 bot.py
pause
