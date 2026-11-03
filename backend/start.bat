@echo off
rem Reed AI backend launcher - always uses the project venv (which has pyannote,
rem faster-whisper, torch etc). Run this instead of typing the python command.
cd /d "%~dp0"
".\.venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
