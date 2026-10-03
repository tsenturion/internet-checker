param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Write-Host "Installing build dependencies..."
& $Python -m pip install --upgrade pyinstaller
& $Python -m pip install -r requirements.txt

Write-Host "Building InternetChecker (onefile)..."
& $Python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name InternetChecker `
    --collect-all windows_toasts `
    --hidden-import pystray._win32 `
    --exclude-module IPython `
    main.py

Write-Host "Done. EXE: dist\\InternetChecker.exe"
Write-Host "Run: dist\\InternetChecker.exe"
