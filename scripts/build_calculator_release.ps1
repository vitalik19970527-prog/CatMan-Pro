param(
    [string]$Version = "1.0.1",
    [switch]$CopyToDesktop
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv/Scripts/python.exe")) {
    throw "Python venv not found at .venv/Scripts/python.exe"
}

$python = ".venv/Scripts/python.exe"
$envFile = Join-Path (Get-Location) ".env"

$releaseRoot = ".program\CatManCalculator-$Version"
$setupDir = Join-Path $releaseRoot ".setup"
$buildDir = Join-Path $setupDir "build"
$workDir = Join-Path $setupDir "build_tmp"
$distDir = Join-Path $buildDir "setup_dist"

New-Item -ItemType Directory -Path $setupDir -Force | Out-Null
if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }
if (Test-Path $workDir) { Remove-Item $workDir -Recurse -Force }
New-Item -ItemType Directory -Path $buildDir | Out-Null
New-Item -ItemType Directory -Path $workDir | Out-Null

Write-Host "=== Building CatManCalculator installer (version $Version) ===" -ForegroundColor Cyan

# --onedir (not --onefile like the old portable build): Inno Setup packages
# the whole dist folder itself, so the installed app should just be plain
# unpacked files, not a self-extracting single exe.
$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--windowed", "--onedir",
    "--name", "CatManCalculator",
    "--collect-all", "flet",
    "--collect-all", "flet_desktop",
    "--collect-all", "unittest",
    "--hidden-import", "database",
    "--hidden-import", "psycopg",
    "--hidden-import", "bcrypt",
    "--hidden-import", "httpx",
    "--hidden-import", "i18n",
    "--hidden-import", "logger",
    "--hidden-import", "views.calculators_view",
    "--distpath", $distDir,
    "--workpath", $workDir,
    "--specpath", $workDir
)
if (Test-Path $envFile) {
    $pyiArgs += @("--add-data", "$envFile;.")
}
$pyiArgs += "calculator_app.py"

& $python @pyiArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed for CatManCalculator"
}

$exePath = "$distDir/CatManCalculator/CatManCalculator.exe"
if (-not (Test-Path $exePath)) {
    throw "Build output not found: $exePath"
}

$isccCandidates = @(
    "$env:LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe",
    "C:/Program Files (x86)/Inno Setup 6/ISCC.exe",
    "C:/Program Files/Inno Setup 6/ISCC.exe"
)
$isccExe = $null
foreach ($candidate in $isccCandidates) {
    if (Test-Path $candidate) {
        $isccExe = $candidate
        break
    }
}
if (-not $isccExe) {
    throw "Inno Setup compiler ISCC.exe was not found. Install Inno Setup 6 first."
}

$installerScript = "scripts/CatManCalculator.installer.iss"
if (-not (Test-Path $installerScript)) {
    throw "Installer script not found: $installerScript"
}

$issOutputDir = (Resolve-Path $setupDir).Path
$issBuildDir = (Resolve-Path "$distDir/CatManCalculator").Path

$issArgs = @(
    "/DAppVersion=$Version",
    "/DOutputBaseFilename=CatManCalculator-Setup-$Version",
    "/DOutputDir=$issOutputDir",
    "/DBuildDirPath=$issBuildDir",
    $installerScript
)

& $isccExe @issArgs
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup compiler failed for CatManCalculator"
}

$setupExe = "$setupDir/CatManCalculator-Setup-$Version.exe"
if (-not (Test-Path $setupExe)) {
    throw "Installer build failed: $setupExe not found"
}

Write-Host "Built: $setupExe" -ForegroundColor Green

if ($CopyToDesktop) {
    $desktopDir = [Environment]::GetFolderPath("Desktop")
    Copy-Item $setupExe (Join-Path $desktopDir (Split-Path $setupExe -Leaf)) -Force
    Write-Host "Copied to Desktop: $setupExe" -ForegroundColor Green
}
