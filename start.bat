@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo   YSL OFFLINE PHOTOBOOTH
echo ==========================================
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ was not found. Install Python and enable Add Python to PATH.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :error
)
call ".venv\Scripts\activate.bat"
echo Installing/checking dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo Starting local photobooth server...
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:5000"
python app.py
goto :eof
:error
echo Setup failed. Check Python installation and network access for first-time package installation.
pause
exit /b 1
