<#
  Builds the installer:   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-Version 1.0.0]

  1. stages ngrok.exe and rg.exe into packaging\bin
  2. PyInstaller  -> packaging\dist_app\CodyMCPServer\   (GUI exe + server exe, no Python needed to run)
  3. Inno Setup   -> packaging\installer_output\CodyMCPServer-Setup-<version>.exe
#>
param(
    [string]$Version = "1.0.0",
    [string]$NgrokPath,
    [string]$RgPath,
    [switch]$SkipInstaller      # build only the app folder (for testing)
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Find-Tool([string]$Name, [string]$Override) {
    if ($Override) {
        if (-not (Test-Path $Override)) { throw "$Name not found at $Override" }
        return (Resolve-Path $Override).Path
    }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "$Name not found on PATH. Install it or pass its path (-NgrokPath / -RgPath)." }
    return $cmd.Source
}

Write-Host "==> Staging bundled tools" -ForegroundColor Cyan
$bin = Join-Path $PSScriptRoot "bin"
New-Item -ItemType Directory -Force $bin | Out-Null
Copy-Item (Find-Tool "ngrok" $NgrokPath) (Join-Path $bin "ngrok.exe") -Force
Copy-Item (Find-Tool "rg" $RgPath)       (Join-Path $bin "rg.exe") -Force

Write-Host "==> PyInstaller" -ForegroundColor Cyan
$python = Join-Path $root "venv\Scripts\python.exe"
& $python -m PyInstaller (Join-Path $PSScriptRoot "CodyMCPServer.spec") `
    --noconfirm --clean --distpath (Join-Path $PSScriptRoot "dist_app") --workpath (Join-Path $PSScriptRoot "build")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

if ($SkipInstaller) { Write-Host "App folder ready: packaging\dist_app\CodyMCPServer" -ForegroundColor Green; return }

Write-Host "==> Inno Setup" -ForegroundColor Cyan
$candidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}
if (-not $iscc) { throw "Inno Setup 6 not found. Install it:  winget install JRSoftware.InnoSetup" }

& $iscc "/DAppVersion=$Version" (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Write-Host "Installer: packaging\installer_output\CodyMCPServer-Setup-$Version.exe" -ForegroundColor Green
