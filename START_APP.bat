@echo off
REM One-click launcher for Toast Net Sales App
REM This script sets up everything and starts the app automatically

echo ========================================
echo Toast Net Sales App - Starting...
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed!
    echo.
    echo Please install Python 3.10+ from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM Navigate to backend directory
cd backend

REM Check if virtual environment exists, create if not
if not exist venv (
    echo [1/3] Creating virtual environment (first time only)...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
)

REM Activate virtual environment
call venv\Scripts\activate.bat

REM Check if dependencies are installed
python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo [2/3] Installing dependencies (first time only, may take a minute)...
    pip install --quiet --upgrade pip
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies
        pause
        exit /b 1
    )
) else (
    echo [2/3] Dependencies already installed.
)

REM Check if .env exists
if not exist .env (
    if exist .env.example (
        echo [3/3] Creating .env file from template...
        copy .env.example .env >nul
        echo.
        echo ========================================
        echo IMPORTANT: Configuration Required
        echo ========================================
        echo.
        echo The .env file has been created. You need to add your Toast API credentials.
        echo.
        echo Please edit backend\.env and add:
        echo   - TOAST_API_BASE_URL
        echo   - TOAST_API_CLIENT_ID
        echo   - TOAST_API_CLIENT_SECRET
        echo.
        echo Then run START_APP.bat again.
        echo.
        pause
        exit /b 0
    ) else (
        echo WARNING: .env file not found and .env.example is missing.
        echo You'll need to create backend\.env manually with your credentials.
        echo.
        pause
    )
)

echo [3/3] Starting server...
echo.
echo ========================================
echo Server starting at http://localhost:8000
echo ========================================
echo.
echo The app will open in your browser automatically.
echo.
echo To stop the server, press Ctrl+C in this window.
echo.

REM Wait a moment, then open browser
timeout /t 3 /nobreak >nul
start http://localhost:8000

REM Start the server
uvicorn main:app --host 127.0.0.1 --port 8000

