@echo off
REM Automated setup script for Toast Net Sales App
REM Run this once to set up the environment

echo ========================================
echo Toast Net Sales App - Setup
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.10+ from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo [1/3] Python found. Creating virtual environment...
cd backend
if exist venv (
    echo Virtual environment already exists. Skipping creation.
) else (
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
    echo Virtual environment created.
)

echo.
echo [2/3] Installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)
echo Dependencies installed successfully.

echo.
echo [3/3] Setting up environment file...
if exist .env (
    echo .env file already exists. Skipping.
) else (
    if exist .env.example (
        copy .env.example .env
        echo Created .env file from .env.example
        echo.
        echo ========================================
        echo IMPORTANT: Configuration Required
        echo ========================================
        echo.
        echo Please edit backend\.env and add your Toast API credentials:
        echo   - TOAST_API_BASE_URL
        echo   - TOAST_API_CLIENT_ID
        echo   - TOAST_API_CLIENT_SECRET
        echo.
    ) else (
        echo WARNING: .env.example not found. You'll need to create .env manually.
    )
)

cd ..
echo.
echo ========================================
echo Setup complete!
echo ========================================
echo.
echo Next steps:
echo 1. Edit backend\.env with your Toast API credentials
echo 2. Double-click START_APP.bat to run the app
echo.
pause

