@echo off
setlocal
cd /d %~dp0

set PY=..\.venv\Scripts\python.exe
if exist "runtime\python\python.exe" set PY=runtime\python\python.exe
if not exist "%PY%" set PY=python

set PYTHONPATH=%~dp0
set GUIDE_PORT=8787

echo [guide-agent] starting on http://127.0.0.1:%GUIDE_PORT%
echo   kiosk : http://127.0.0.1:%GUIDE_PORT%/kiosk/
echo   admin : http://127.0.0.1:%GUIDE_PORT%/admin/
echo   doctor: http://127.0.0.1:%GUIDE_PORT%/doctor/
echo   health: http://127.0.0.1:%GUIDE_PORT%/healthz
echo.

start "" http://127.0.0.1:%GUIDE_PORT%/kiosk/
"%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port %GUIDE_PORT%

endlocal
