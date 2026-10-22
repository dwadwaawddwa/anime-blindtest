@echo off
rem Lance le blind test en local : http://127.0.0.1:8090 (session ouverte d'office via LOCAL_USER_ID du .env)
cd /d "%~dp0"
start "" http://127.0.0.1:8090
venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
