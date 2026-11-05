@echo off
setlocal
cd /d %~dp0
echo [guide-agent] stopping uvicorn on port 8787 ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8787" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F >nul 2>&1
)
echo done.
endlocal
