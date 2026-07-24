$ErrorActionPreference = "Stop"

Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force

$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stageDir = Join-Path $env:TEMP "CatManPro_android_stage"
$releaseApkDir = Join-Path $workspaceRoot ".program/CatManPro-1.0.0/.apk"

$env:JAVA_HOME = "$env:USERPROFILE/java/17.0.13+11"
$env:ANDROID_HOME = "$env:USERPROFILE/Android/sdk"
$env:FLUTTER_HOME = "$env:USERPROFILE/flutter/3.41.4"
$env:PATH = "$env:FLUTTER_HOME/bin;$env:ANDROID_HOME/cmdline-tools/12.0/bin;$env:ANDROID_HOME/platform-tools;$env:JAVA_HOME/bin;$env:PATH"

Write-Host "=== Android Stage Build ===" -ForegroundColor Green
Write-Host "Workspace: $workspaceRoot"
Write-Host "Stage dir: $stageDir"

if (Test-Path $stageDir) {
    Remove-Item $stageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null
New-Item -ItemType Directory -Path $releaseApkDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stageDir "media") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stageDir "media/user_backgrounds") -Force | Out-Null

$rootFiles = @(
    ".env",
    "appwrite_client.py",
    "auth_screen.py",
    "config.py",
    "dashboard.py",
    "database.py",
    "logger.py",
    "main.py",
    "requirements.txt",
    "superadmin_dashboard.py"
)

foreach ($relativePath in $rootFiles) {
    $source = Join-Path $workspaceRoot $relativePath
    if (Test-Path $source) {
        Copy-Item $source (Join-Path $stageDir $relativePath) -Force
    }
}

$viewsSource = Join-Path $workspaceRoot "views"
if (Test-Path $viewsSource) {
    Copy-Item $viewsSource (Join-Path $stageDir "views") -Recurse -Force
}

$companyBackgrounds = Join-Path $workspaceRoot "media/company_backgrounds.json"
if (Test-Path $companyBackgrounds) {
    Copy-Item $companyBackgrounds (Join-Path $stageDir "media/company_backgrounds.json") -Force
} else {
    Set-Content -Path (Join-Path $stageDir "media/company_backgrounds.json") -Value "{}" -Encoding UTF8
}

Set-Content -Path (Join-Path $stageDir "config.txt") -Value "" -Encoding UTF8
Set-Content -Path (Join-Path $stageDir "quick_accounts.json") -Value '{"accounts": []}' -Encoding UTF8
Set-Content -Path (Join-Path $stageDir "super_admin_account.json") -Value '{"company": "", "manager": "", "email": "", "phone": ""}' -Encoding UTF8

$stalePaths = @(
    (Join-Path $stageDir "build"),
    (Join-Path $releaseApkDir "CatManPro-android.apk")
)
foreach ($path in $stalePaths) {
    if (Test-Path $path) {
        Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Stage prepared. Starting Flet build..." -ForegroundColor Cyan

& "d:/CM_WorkSpace/.venv/Scripts/flet.exe" build apk $stageDir --verbose --clear-cache --module-name main --project CatManPro --product "CatMan Pro" --org "com.catmanpro" --exclude __pycache__ logs media/user_backgrounds

if ($LASTEXITCODE -ne 0) {
    throw "flet build apk failed"
}

$apk = Get-ChildItem -Path (Join-Path $stageDir "build/apk") -Filter *.apk -Recurse | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $apk) {
    throw "APK file not found after build"
}

Copy-Item $apk.FullName (Join-Path $releaseApkDir "CatManPro-android.apk") -Force
Write-Host "APK copied to $(Join-Path $releaseApkDir 'CatManPro-android.apk')" -ForegroundColor Green
Write-Host "APK size: $([math]::Round($apk.Length / 1MB, 2)) MB" -ForegroundColor Cyan