@echo off
rem Reed AI backend launcher - always uses the project venv (which has pyannote,
rem faster-whisper, torch etc). Run this instead of typing the python command.
cd /d "%~dp0"
rem Port 8010: 8000 now belongs to the DNOPs API; the frontend proxy targets 8010.
".\.venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8010
