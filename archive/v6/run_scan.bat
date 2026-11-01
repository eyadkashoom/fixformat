@echo off
rem raw_scan launcher - works with the bundled embedded Python (no install).
rem The bundled python\ folder lives in the repository root (next to v1\ and v2\).
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo WARNING: this window is NOT running as Administrator.
    echo Raw disk access may fail. Close it and run as Administrator.
    echo.
)
if exist "%~dp0..\python\python.exe" (
    echo Using bundled embedded Python - no installation needed.
    "%~dp0..\python\python.exe" "%~dp0raw_scan.py"
) else if exist "%~dp0python\python.exe" (
    echo Using bundled embedded Python - no installation needed.
    "%~dp0python\python.exe" "%~dp0raw_scan.py"
) else (
    echo Bundled Python not found - trying the system python.
    echo See README.md for how to add the embedded Python folder.
    python "%~dp0raw_scan.py"
)
echo.
pause
