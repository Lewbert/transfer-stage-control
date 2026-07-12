@echo off
REM ============================================================
REM Build script for Transfer Stage Control application
REM ============================================================
REM Prerequisites: conda environment 'transfer_stage' with
REM   python=3.12, pyserial, pyinstaller
REM
REM Usage: build.bat
REM Output: dist\TransferStageControl.exe
REM ============================================================

echo === Transfer Stage Control — Build ===
echo.

REM Activate conda environment
call conda activate transfer_stage
if errorlevel 1 (
    echo ERROR: Could not activate conda env 'transfer_stage'.
    echo Run: conda env create -f environment.yml
    pause
    exit /b 1
)

REM Clean previous build
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

echo.
echo === Building with PyInstaller ===
pyinstaller --clean --noconfirm "%~dp0transfer_stage.spec"
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo === Build complete ===
echo Output: dist\TransferStageControl.exe
echo.
pause
