Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c cd /d %~dp0 && .venv\Scripts\activate && python -m uvicorn app.main:app --host 127.0.0.1 --port 8010", 0