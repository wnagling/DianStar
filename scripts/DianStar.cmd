@echo off
cd /d "%~dp0"
"%~dp0DianStar.exe"
if errorlevel 1 (
    echo.
    echo DianStar failed to start. See the error above.
    pause
)
