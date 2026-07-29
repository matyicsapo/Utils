@echo off
setlocal
set SCRIPT_DIR=%~dp0

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%SCRIPT_DIR%p4m.py" %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python "%SCRIPT_DIR%p4m.py" %*
    exit /b %ERRORLEVEL%
)

echo p4m: Could not find Python launcher or python in PATH. 1>&2
exit /b 1
