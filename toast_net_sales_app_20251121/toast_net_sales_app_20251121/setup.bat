@echo off
echo ========================================
echo Newk's Toast Net Sales App - Setup
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.10+ from https://www.python.org/
    pause
    exit /b 1
)

echo [1/4] Python found. Creating virtual environment...
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
echo [2/4] Activating virtual environment and installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)
echo Dependencies installed successfully.

echo.
echo [3/4] Setting up environment file...
if exist .env (
    echo .env file already exists. Skipping.
) else (
    if exist .env.example (
        copy .env.example .env
        echo Created .env file from .env.example
        echo.
        echo IMPORTANT: You need to edit backend\.env and add your Toast API credentials!
    ) else (
        echo WARNING: .env.example not found. You'll need to create .env manually.
    )
)

echo.
echo [4/4] Setup complete!
echo.
echo ========================================
echo Next steps:
echo ========================================
echo 1. Edit backend\.env and add your Toast API credentials:
echo    - TOAST_API_BASE_URL
echo    - TOAST_API_CLIENT_ID
echo    - TOAST_API_CLIENT_SECRET
echo.
echo 2. Start the server:
echo    cd backend
echo    venv\Scripts\activate
echo    uvicorn main:app --reload
echo.
echo 3. Visit http://localhost:8000/ in your browser
echo.
pause

