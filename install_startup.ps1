param(
    [string]$SourceDir = ".\dist\InternetChecker",
    [string]$InstallDir = "$env:LOCALAPPDATA\InternetChecker\app",
    [string]$ShortcutName = "InternetChecker.lnk",
    [switch]$StartAfterInstall = $true
)

$ErrorActionPreference = "Stop"

$sourcePath = (Resolve-Path $SourceDir).Path
$targetExe = Join-Path $InstallDir "InternetChecker.exe"
$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$shortcutPath = Join-Path $startupDir $ShortcutName
$legacyStartupExe = Join-Path $startupDir "InternetChecker.exe"

if (-not (Test-Path $sourcePath)) {
    throw "Source build folder not found: $SourceDir"
}

Write-Host "Stopping running InternetChecker processes..."
for ($i = 0; $i -lt 8; $i++) {
    $procs = Get-Process | Where-Object { $_.ProcessName -eq "InternetChecker" }
    if (-not $procs) { break }
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 250
}

Write-Host "Installing app to: $InstallDir"
New-Item -Path $InstallDir -ItemType Directory -Force | Out-Null
Copy-Item -Path (Join-Path $sourcePath "*") -Destination $InstallDir -Recurse -Force

if (-not (Test-Path $targetExe)) {
    throw "Installed executable not found: $targetExe"
}

if (Test-Path $legacyStartupExe) {
    Write-Host "Removing legacy startup EXE: $legacyStartupExe"
    Remove-Item -Path $legacyStartupExe -Force
}

Write-Host "Creating startup shortcut: $shortcutPath"
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetExe
$shortcut.WorkingDirectory = $InstallDir
$shortcut.WindowStyle = 1
$shortcut.Description = "Internet Checker"
$shortcut.IconLocation = "$targetExe,0"
$shortcut.Save()

if ($StartAfterInstall) {
    Write-Host "Starting app..."
    Start-Process -FilePath $targetExe -WorkingDirectory $InstallDir | Out-Null
}

Write-Host "Done."
Write-Host "Installed EXE: $targetExe"
Write-Host "Startup shortcut: $shortcutPath"
