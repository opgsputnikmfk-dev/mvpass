@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Open http://127.0.0.1:8765 after preparation completes.
.venv\Scripts\python.exe bot.py demo
pause
exit /b
:fail
echo Install Python 3.11 or newer and retry.
pause
