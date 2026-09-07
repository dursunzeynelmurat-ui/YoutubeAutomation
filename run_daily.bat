@echo off
REM ====================================================================
REM  run_daily.bat - produce N Reddit-story shorts (for Task Scheduler).
REM  Usage:  run_daily.bat [count]        (default count = 3)
REM  Add --upload once the 2nd channel's YouTube OAuth is set up.
REM ====================================================================
cd /d "%~dp0"
set COUNT=%1
if "%COUNT%"=="" set COUNT=3

call .venv\Scripts\activate.bat

echo [%date% %time%] producing %COUNT% Reddit stor(y/ies)...
python pipeline\redditstory.py --auto --count %COUNT% --config config.reddit.yaml >> logs\daily.log 2>&1

REM  Optional: uncomment to build a weekly compilation every run (or schedule separately)
REM python pipeline\compile_weekly.py --days 7 --config config.reddit.yaml >> logs\daily.log 2>&1

echo [%date% %time%] done. See logs\daily.log
