@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Local environment not found. Follow LOCAL_SETUP.md first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
if errorlevel 1 pause
