@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "SHANYDOC" cmd /k python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
ping -n 4 127.0.0.1 >nul
start http://127.0.0.1:8765
