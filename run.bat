@echo off
cd /d "%~dp0"

echo ============================================
echo        Z.ai Multi Account Manager
echo ============================================

:: Check if virtual environment exists
if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found!
    echo Please create it using: python -m venv venv
    pause
    exit /b
)

:: Activate virtual environment
call venv\Scripts\activate.bat

:: Run the main script
python account_router.py --list

echo.
pause