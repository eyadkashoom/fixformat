@echo off
rem fixformat v7 web GUI launcher - opens browser at http://127.0.0.1:8787/
cd /d "%~dp0"
start "" "http://127.0.0.1:8787/"
if exist "%~dp0..\python\python.exe" (
    "%~dp0..\python\python.exe" "%~dp0gui_web.py"
) else if exist "%~dp0python\python.exe" (
    "%~dp0python\python.exe" "%~dp0gui_web.py"
) else (
    python "%~dp0gui_web.py"
)
