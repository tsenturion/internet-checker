param(
    [string]$SourceExe = ".\dist\InternetChecker.exe",
    [string]$StartupExeName = "InternetChecker.exe",
    [string]$SourceEnvFile = ".\.env",
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

Write-Host "Остановка установленного приложения из Startup..."
for ($i = 0; $i -lt 12; $i++) {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'InternetChecker.exe'" |
        Where-Object { $_.ExecutablePath -eq $targetExe }
    if (-not $procs) { break }
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 250
}

New-Item -Path $startupDir -ItemType Directory -Force | Out-Null

if (Test-Path $legacyShortcut) {
    Write-Host "Removing legacy startup shortcut: $legacyShortcut"
    Remove-Item -LiteralPath $legacyShortcut -Force
}

Write-Host "Installing startup EXE: $targetExe"
Copy-Item -LiteralPath $sourcePath -Destination $targetExe -Force

if (-not (Test-Path $targetExe)) {
    throw "Installed executable not found: $targetExe"
}

if (Test-Path -LiteralPath $SourceEnvFile) {
    $dataDir = Join-Path $env:LOCALAPPDATA "InternetChecker"
    New-Item -Path $dataDir -ItemType Directory -Force | Out-Null
    Copy-Item -LiteralPath $SourceEnvFile -Destination (Join-Path $dataDir ".env") -Force
    Write-Host "Настройки API скопированы в пользовательский каталог приложения."
}

if ($StartAfterInstall) {
    Write-Host "Starting app..."
    Start-Process -FilePath $targetExe -WorkingDirectory $startupDir -WindowStyle Hidden | Out-Null
}

Write-Host "Done."
Write-Host "Startup EXE: $targetExe"
