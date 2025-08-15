#!/usr/bin/env powershell

Write-Host "Building Image Viewer executable..." -ForegroundColor Green
Write-Host ""

# Clean previous build
if (Test-Path "dist") {
    Write-Host "Cleaning previous build..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force "dist"
}

if (Test-Path "build") {
    Write-Host "Cleaning build cache..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force "build"
}

Write-Host ""
Write-Host "Starting PyInstaller build..." -ForegroundColor Cyan
pyinstaller --clean image_viewer.spec

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "BUILD SUCCESSFUL!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Your executable is located at:" -ForegroundColor Green
    Write-Host "$PWD\dist\ImageViewer.exe" -ForegroundColor White
    Write-Host ""
    Write-Host "You can now distribute this single .exe file!" -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "BUILD FAILED!" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "Please check the error messages above." -ForegroundColor Red
}

Read-Host "Press Enter to continue"
