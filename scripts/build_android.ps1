# Complete Android APK build script with all permissions and environment setup

# Set execution policy
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force

# Configure environment variables
$env:JAVA_HOME = "$env:USERPROFILE/java/17.0.13+11"
$env:ANDROID_HOME = "$env:USERPROFILE/Android/sdk"
$env:FLUTTER_HOME = "$env:USERPROFILE/flutter/3.41.4"
$env:PATH = "$env:FLUTTER_HOME/bin;$env:ANDROID_HOME/cmdline-tools/12.0/bin;$env:ANDROID_HOME/platform-tools;$env:JAVA_HOME/bin;$env:PATH"

# Verify environment
Write-Host "=== Environment Check ===" -ForegroundColor Green
Write-Host "JAVA_HOME: $env:JAVA_HOME"
Write-Host "ANDROID_HOME: $env:ANDROID_HOME"
Write-Host "FLUTTER_HOME: $env:FLUTTER_HOME"

# Verify tools exist
$javaCheck = & "$env:JAVA_HOME/bin/java.exe" -version 2>&1
Write-Host "Java version: $($javaCheck[0])" -ForegroundColor Cyan

$flutterCheck = & "$env:FLUTTER_HOME/bin/flutter.bat" --version 2>&1
Write-Host "Flutter version: $($flutterCheck[0])" -ForegroundColor Cyan

# Create release directory
$releaseApkDir = ".program/CatManPro-1.0.0/.apk"
New-Item -ItemType Directory -Path $releaseApkDir -Force | Out-Null
Write-Host "Release directory ready: $releaseApkDir" -ForegroundColor Green

# Remove stale Android build artifacts so packaging cannot reuse an old app bundle.
$stalePaths = @(
    "build/flutter/app/app.zip",
    "build/flutter/app/app.zip.hash",
    "build/apk/CM_WorkSpace.apk",
    "build/apk/CM_WorkSpace.apk.sha1",
    "$releaseApkDir/CatManPro-android.apk"
)
foreach ($path in $stalePaths) {
    if (Test-Path $path) {
        Remove-Item $path -Force -ErrorAction SilentlyContinue
    }
}

# Build APK
Write-Host "`n=== Starting APK Build ===" -ForegroundColor Green
Write-Host "Command: flet build apk --verbose --clear-cache --module-name main ..." -ForegroundColor Cyan
Write-Host "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan

& "d:/CM_WorkSpace/.venv/Scripts/flet.exe" build apk --verbose --clear-cache --module-name main --exclude .venv .program build dist __pycache__ logs .git .github .vscode Парсер media/user_backgrounds CatManPro.spec pyarmor.bug.log quick_accounts.json super_admin_account.json

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n=== Build Successful ===" -ForegroundColor Green
    
    # Find and copy APK
    $apk = Get-ChildItem -Path . -Filter *.apk -Recurse | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    
    if ($apk) {
        Write-Host "Found APK: $($apk.FullName)" -ForegroundColor Cyan
        Write-Host "Size: $([math]::Round($apk.Length / 1MB, 2)) MB" -ForegroundColor Cyan
        
        Copy-Item $apk.FullName "$releaseApkDir/CatManPro-android.apk" -Force
        Write-Host "✅ APK copied to: $releaseApkDir/CatManPro-android.apk" -ForegroundColor Green
        Write-Host "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
    } else {
        Write-Host "❌ APK file not found after build" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "`n❌ Build failed with exit code: $LASTEXITCODE" -ForegroundColor Red
    exit 1
}
