@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Optional: copy .env.example to .env and fill in your own paths.
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

if not defined NOVEL_WRITER_ARTICLE_ROOT set "NOVEL_WRITER_ARTICLE_ROOT=%USERPROFILE%\Article"

rem Illustration model swap (optional). Example placeholders, not real machine paths:
rem set NOVEL_WRITER_LLAMA_EXE=C:\llama.cpp\llama-server.exe
rem set NOVEL_WRITER_LLAMA_MODEL=C:\models\Qwen3.8-27B-UD-Q5_K_M.gguf
rem set NOVEL_WRITER_COMFY_ROOT=C:\ComfyUI

start "SHANYDOC" cmd /k python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
ping -n 4 127.0.0.1 >nul
start http://127.0.0.1:8765
