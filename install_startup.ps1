param(
    [string]$SourceExe = ".\dist\InternetChecker.exe",
    [string]$StartupExeName = "InternetChecker.exe",
    [switch]$StartAfterInstall = $true
)

$ErrorActionPreference = "Stop"

$sourcePath = (Resolve-Path $SourceExe).Path
$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$targetExe = Join-Path $startupDir $StartupExeName
$legacyShortcut = Join-Path $startupDir "InternetChecker.lnk"

if (-not (Test-Path $sourcePath)) {
    throw "Source executable not found: $SourceExe"
}

Write-Host "Stopping running InternetChecker processes..."
for ($i = 0; $i -lt 12; $i++) {
    $procs = Get-Process | Where-Object { $_.ProcessName -eq "InternetChecker" }
    if (-not $procs) { break }
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 250
}

New-Item -Path $startupDir -ItemType Directory -Force | Out-Null

if (Test-Path $legacyShortcut) {
    Write-Host "Removing legacy startup shortcut: $legacyShortcut"
    Remove-Item -Path $legacyShortcut -Force
}

Write-Host "Installing startup EXE: $targetExe"
Copy-Item -Path $sourcePath -Destination $targetExe -Force

if (-not (Test-Path $targetExe)) {
    throw "Installed executable not found: $targetExe"
}

if ($StartAfterInstall) {
    Write-Host "Starting app..."
    Start-Process -FilePath $targetExe -WorkingDirectory $startupDir -WindowStyle Hidden | Out-Null
}

Write-Host "Done."
Write-Host "Startup EXE: $targetExe"
