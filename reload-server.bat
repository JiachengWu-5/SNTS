@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Wait for the current request to finish, then restart only this writing service.

if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)
if not defined NOVEL_WRITER_ARTICLE_ROOT set "NOVEL_WRITER_ARTICLE_ROOT=%USERPROFILE%\Article"

ping -n 3 127.0.0.1 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match 'uvicorn app.main:app') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
ping -n 2 127.0.0.1 >nul
start "SHANYDOC" cmd /k python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
