@echo off
echo Building Image Viewer executable...
echo.

REM Clean previous build
if exist "dist" (
    echo Cleaning previous build...
    rmdir /s /q "dist"
)

if exist "build" (
    echo Cleaning build cache...
    rmdir /s /q "build"
)

echo.
echo Starting PyInstaller build...
pyinstaller --clean image_viewer.spec

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo BUILD SUCCESSFUL!
    echo ========================================
    echo.
    echo Your executable is located at:
    echo %cd%\dist\ImageViewer.exe
    echo.
    echo You can now distribute this single .exe file!
    echo.
) else (
    echo.
    echo ========================================
    echo BUILD FAILED!
    echo ========================================
    echo Please check the error messages above.
)

pause
