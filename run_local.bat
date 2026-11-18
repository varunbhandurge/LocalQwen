@echo off
rem run_local.bat  -- one-command start for Windows

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

if not exist "venv" (
    echo [start] Creating Python virtualenv ...
    python -m venv venv
)
call venv\Scripts\activate

echo [start] Installing deps ...
pip install -q -r requirements.txt

if not exist ".env" (
    if exist ".env.example" copy .env.example .env
    echo [start] Created .env from .env.example -- edit it for your credentials.
)

python -c "from app.db import init_db; init_db()"

echo [start] Launching at http://localhost:8000
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
