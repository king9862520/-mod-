@echo off
setlocal
cd /d "%~dp0\.."

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest
python -m PyInstaller --noconfirm --clean ONIHub.spec

if errorlevel 1 exit /b 1

echo.
echo Build complete: dist\ONIHub.exe
endlocal
