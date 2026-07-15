@echo off
setlocal

set "PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Development environment not found.
    echo Run "uv sync --extra desktop" once, then run ".\dev.cmd" again.
    exit /b 1
)

"%PYTHON%" "%~dp0dev.py" %*
exit /b %ERRORLEVEL%
