@echo off
REM Double-click to open Scroll & Told Studio (desktop control panel).
cd /d "%~dp0"
".venv\Scripts\pythonw.exe" gui.py
if errorlevel 1 ".venv\Scripts\python.exe" gui.py
