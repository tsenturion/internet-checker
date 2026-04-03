param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Write-Host "Installing build dependencies..."
& $Python -m pip install --upgrade pyinstaller
& $Python -m pip install -r requirements.txt

Write-Host "Building InternetChecker (onedir)..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name InternetChecker `
    --collect-all windows_toasts `
    --hidden-import pystray._win32 `
    main.py

Write-Host "Done. App folder: dist\\InternetChecker\\"
Write-Host "Run: dist\\InternetChecker\\InternetChecker.exe"
