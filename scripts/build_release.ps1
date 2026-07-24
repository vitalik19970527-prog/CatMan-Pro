param(
    [string]$Version = "",
    [switch]$SkipCodeProtection,
    [switch]$NoSetup,
    [switch]$BuildApk,
    [ValidateSet("main", "superadmin", "both")]
    [string]$App = "main",
    [switch]$CopyToDesktop
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv/Scripts/python.exe")) {
    throw "Python venv not found at .venv/Scripts/python.exe"
}

$python = ".venv/Scripts/python.exe"
$programRoot = ".program"
$envFile = Join-Path (Get-Location) ".env"

$buildSetup = -not $NoSetup

# Per-app build profile. "main" is the full CatMan Pro desktop/mobile app;
# "superadmin" is the standalone CatMan SupAdm mini-app (superadmin_app.py,
# see [[project_telegram_site_bot]] Phase 4/5) — same repo, same DB/i18n
# modules, just a different entry point and a distinct app identity so both
# can be installed side by side on the same PC/phone.
$appProfiles = @{
    main       = @{
        EntryScript       = "main.py"
        ExeBaseName       = "CatManPro"
        HiddenImports     = @(
            "flet", "auth_screen", "dashboard", "superadmin_dashboard",
            "database", "config", "logger", "bcrypt",
            "views.categories_brands_view", "views.packaging_view",
            "views.products_view", "views.settings_view", "views.suppliers_view",
            "views.notebook_view", "views.languages_view", "views.help_utils",
            "views.web_site_view", "gsm_scheduler",
            # matplotlib (GSM Parser device-card charts) imports this dynamically —
            # PyInstaller's static analysis never sees the reference, so just NOT
            # excluding "unittest" isn't enough; it must be forced in explicitly
            # or the packaged exe/apk fails with "No module named 'unittest'"
            # the moment a chart with real data renders (found via live testing).
            "unittest",
            # All function-scoped/lazy imports found in a codebase sweep after the
            # unittest incident — none confirmed broken yet, added defensively
            # since they're only reached at a specific runtime moment (DB
            # credential encryption, second-instance warning dialog, Excel
            # import/export) rather than at main.py's own top level.
            "cryptography", "cryptography.fernet", "tkinter", "openpyxl", "PIL"
        )
        InstallerAppName  = "CatMan Pro"
        # Doubled "{{" is Inno Setup's own escape for a literal "{" in the .iss
        # AppId={#MyAppId} substitution (see CatManPro.installer.iss).
        InstallerAppId    = "{{3E7A9DB5-6C9E-4E80-94A8-1B8D6D7F04A2}"
        ApkModuleName     = "main"
        ApkOrg            = "com.catmanpro"
        ApkProject        = "CatManPro"
        ApkProduct        = "CatMan Pro"
        StubModuleName    = "main"
        StubHasCrashHooks = $true
    }
    superadmin = @{
        EntryScript       = "superadmin_app.py"
        ExeBaseName       = "CatManSupAdm"
        HiddenImports     = @(
            "flet", "superadmin_dashboard", "database", "config", "logger", "bcrypt", "unittest",
            "views.help_utils", "views.web_site_view", "cryptography", "cryptography.fernet", "tkinter"
        )
        InstallerAppName  = "CatMan SupAdm"
        InstallerAppId    = "{{9F2C1A44-7B3D-4E15-8A6F-2D9B5C7E1F30}"
        ApkModuleName     = "superadmin_app"
        ApkOrg            = "com.catmanpro.supadm"
        ApkProject        = "CatManSupAdm"
        ApkProduct        = "CatMan SupAdm"
        StubModuleName    = "superadmin_app"
        StubHasCrashHooks = $false
    }
}

$appsToBuild = if ($App -eq "both") { @("main", "superadmin") } else { @($App) }

& $python -m pip install --upgrade pip
if (Test-Path "requirements.txt") {
    & $python -m pip install -r requirements.txt
}
# psycopg is deliberately NOT in requirements.txt: that file is also fed
# straight to the Android build's own pip resolution (flet build apk ->
# serious_python), which only resolves against flet's own package index
# (pypi.flet.dev). psycopg[binary] doesn't exist there at all, and asking
# for it there has flipped between silently skipping it and hard-failing
# the whole APK build across otherwise-identical runs. Desktop needs the
# real C-extension psycopg regardless, so it's installed here directly
# instead — this never touches the Android build. Android's DB access goes
# through the pure-Python psycopg_android_shim.py (backed by pg8000),
# vendored as plain project source (pg8000/, scramp/, asn1crypto/ folders)
# so it needs no separate pip resolution on either platform.
& $python -m pip install "psycopg[binary]==3.3.3"
& $python -m pip install pyinstaller pyarmor nuitka

if (-not [string]::IsNullOrWhiteSpace($Version)) {
    $cfg = Get-Content "config.py" -Raw
    $cfg = $cfg -replace 'APP_VERSION\s*=\s*"[^"]+"', "APP_VERSION = `"$Version`""
    Set-Content "config.py" $cfg -Encoding UTF8
    Write-Host "Updated APP_VERSION to $Version"
}

if (-not $buildSetup -and -not $BuildApk) {
    throw "Nothing to build. Enable at least one target: Setup or APK."
}

$resolvedVersion = "0.0.0"
if (-not [string]::IsNullOrWhiteSpace($Version)) {
    # Always trust explicit CLI version from GUI/terminal to avoid stale config confusion.
    $resolvedVersion = $Version.Trim()
}
elseif (Test-Path "config.py") {
    $cfgRaw = Get-Content "config.py" -Raw
    $m = [regex]::Match($cfgRaw, 'APP_VERSION\s*=\s*"([^"]+)"')
    if ($m.Success) {
        $resolvedVersion = $m.Groups[1].Value
    }
}

if ($resolvedVersion -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') {
    throw "Resolved version '$resolvedVersion' is invalid. Use MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH.BUILD (e.g. 1.0.2 or 1.0.2.5)."
}

Write-Host "Effective build version: $resolvedVersion"
Write-Host "Building app(s): $($appsToBuild -join ', ')"

$releaseRoot = Join-Path $programRoot ("CatManPro-" + $resolvedVersion)

# Sanitize local dev data (quick-login accounts + personal .env fields)
# before packaging, so exe/apk never ship the developer's own accounts or
# secrets — see scripts/prepare_release_payload.py. This is a LOCAL build,
# so unlike the throwaway CI checkout, the sanitized files get restored
# from backup once packaging finishes (success or failure) so the dev's
# own working copy isn't wiped out by running a release build.
$sanitizeTargets = @("quick_accounts.json", "super_admin_account.json", "config.txt", ".env")
$sanitizeBackupDir = Join-Path $env:TEMP ("CatManPro_release_backup_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $sanitizeBackupDir -Force | Out-Null
foreach ($target in $sanitizeTargets) {
    if (Test-Path $target) {
        Copy-Item $target (Join-Path $sanitizeBackupDir $target) -Force
    }
}

Write-Host "Sanitizing local data files for release build..."
& $python "scripts/prepare_release_payload.py"
if ($LASTEXITCODE -ne 0) {
    throw "Sanitizing local data files failed (scripts/prepare_release_payload.py)"
}

$builtOutputs = @()
$desktopDir = if ($CopyToDesktop) { [Environment]::GetFolderPath("Desktop") } else { $null }

function Copy-BuildOutputNow($path) {
    # Copies immediately (not just at the very end) so a LATER target failing
    # (e.g. the flaky Kotlin/Gradle daemon during APK builds — see
    # [[project_telegram_site_bot]]) doesn't lose an already-finished exe/apk
    # that never got delivered because the script threw before reaching the
    # old end-of-script copy loop.
    if ($desktopDir) {
        Copy-Item $path (Join-Path $desktopDir (Split-Path $path -Leaf)) -Force
        Write-Host "Copied to Desktop: $path" -ForegroundColor Green
    }
}

try {
    foreach ($appKey in $appsToBuild) {
        $appProfile = $appProfiles[$appKey]
        Write-Host "=== Building '$appKey' ($($appProfile.ExeBaseName)) ===" -ForegroundColor Cyan

        $appRoot = Join-Path $releaseRoot $appProfile.ExeBaseName
        $setupDir = Join-Path $appRoot ".setup"
        $apkDir = Join-Path $appRoot ".apk"
        $obfDir = Join-Path $setupDir "obf"
        $buildDir = Join-Path $setupDir "build"
        $workDir = Join-Path $setupDir "build_tmp"

        New-Item -ItemType Directory -Path $setupDir -Force | Out-Null
        New-Item -ItemType Directory -Path $apkDir -Force | Out-Null

        if ($buildSetup) {
            if (Test-Path $obfDir) { Remove-Item $obfDir -Recurse -Force }
            if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }
            if (Test-Path $workDir) { Remove-Item $workDir -Recurse -Force }

            New-Item -ItemType Directory -Path $obfDir | Out-Null
            New-Item -ItemType Directory -Path $buildDir | Out-Null
            New-Item -ItemType Directory -Path $workDir | Out-Null

            $entryScript = $appProfile.EntryScript
            $extraProtectionPyiArgs = @()

            if (-not $SkipCodeProtection) {
                # PyArmor's trial license is exhausted ("out of license") and has no free
                # tier for continued use. Nuitka (Apache-2.0, no trial limits, already in
                # requirements via .venv) compiles the entry script to genuine machine code
                # instead of just obfuscating source text — stronger protection, permanently
                # free. --module keeps the same scope PyArmor had (protect only the
                # entrypoint, not the whole app). --assume-yes-for-downloads lets Nuitka
                # fetch its own C backend (zig) automatically, so no separate MSVC/MinGW
                # install is needed.
                & $python -m nuitka --module --output-dir=$obfDir --assume-yes-for-downloads --remove-output $appProfile.EntryScript
                $compiledMain = Get-ChildItem -Path $obfDir -Filter "$($appProfile.StubModuleName).*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
                if (($LASTEXITCODE -ne 0) -or (-not $compiledMain)) {
                    throw "Code protection step failed (Nuitka) for $appKey."
                }

                # A compiled extension imported as `import <module>` runs with __name__ ==
                # "<module>", not "__main__", so the entry script's `if __name__ ==
                # "__main__":` launch block would never fire. The stub just imports the
                # compiled module (which runs everything at module level exactly as before)
                # and replicates that launch block directly, calling the module's functions
                # by name — same effect, no loader introspection needed (runpy.run_module
                # doesn't work here: Nuitka-compiled extensions don't implement get_code()).
                $stubPath = Join-Path $obfDir "$($appProfile.StubModuleName)_stub.py"
                if ($appProfile.StubHasCrashHooks) {
                    $stubContent = @'
import os
import sys
from contextlib import suppress

import flet as ft

import main
import local_pg_manager
from logger import logger

main._install_global_crash_hooks()
_lock = main._acquire_single_instance_lock()
if _lock is None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "CatMan Pro",
            "Програма вже запущена!\n\nЗнайдіть вікно CatMan Pro на панелі задач.",
        )
        root.destroy()
    except Exception:
        logger.exception("Failed to show already-running warning via tkinter")
    sys.exit(0)

try:
    logger.info(
        "[session] process_boot | pid=%s | python=%s | cwd=%s",
        os.getpid(),
        sys.executable,
        os.getcwd(),
    )
    ft.run(main.main)
finally:
    with suppress(Exception):
        local_pg_manager.stop_all()
    _lock.close()
'@
                }
                else {
                    $stubContent = @'
import sys

import flet as ft

import superadmin_app
from logger import logger

_lock = superadmin_app._acquire_single_instance_lock()
if _lock is None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "CatMan SupAdm",
            "Програма вже запущена!\n\nЗнайдіть вікно CatMan SupAdm на панелі задач.",
        )
        root.destroy()
    except Exception:
        logger.exception("Failed to show already-running warning via tkinter")
    sys.exit(0)

try:
    ft.run(superadmin_app.main)
finally:
    _lock.close()
'@
                }
                Set-Content -Path $stubPath -Value $stubContent -Encoding UTF8

                $entryScript = $stubPath
                $extraProtectionPyiArgs = @("--add-binary", "$($compiledMain.FullName);.")
                Write-Host "Code protection enabled: using Nuitka-compiled entrypoint ($($compiledMain.Name))"
            }
            else {
                Write-Host "Code protection skipped by flag"
            }

            $basePyiArgs = @(
                "-m", "PyInstaller",
                "--noconfirm",
                "--clean",
                "--windowed",
                "--collect-all", "flet",
                # matplotlib (GSM Parser device-card charts) needs unittest at
                # runtime in a way that neither dropping "--exclude-module
                # unittest" NOR a real top-level `import unittest` in
                # gsm_parser_view.py was enough to make PyInstaller actually
                # bundle (it shows up in the dependency xref, isn't listed as
                # missing/excluded, yet still doesn't land in the packaged
                # _internal folder — verified 3x on this exact toolchain).
                # "--collect-all unittest" is the one flag that reliably forces
                # it in (confirmed with an isolated repro before spending
                # another full build cycle on it). Without this, every chart
                # with real data failed with "No module named 'unittest'".
                "--collect-all", "unittest",
                "--exclude-module", "pytest"
            )

            if ($extraProtectionPyiArgs.Count -gt 0) {
                $basePyiArgs += $extraProtectionPyiArgs
            }

            if (Test-Path $envFile) {
                $basePyiArgs += @("--add-data", "$envFile;.")
            }

            # Bundled portable Postgres for "Локал" storage mode (see
            # local_pg_manager.py) — only meaningful for the main app; the
            # superadmin mini-app only ever talks to the remote general DB.
            if ($appKey -eq "main") {
                $pgVendorDir = "vendor/pgsql"
                if (Test-Path $pgVendorDir) {
                    # PyInstaller resolves relative --add-data source paths against
                    # --specpath (here: a nested folder under .program\...\build_tmp),
                    # not against the current directory — a relative "vendor/pgsql" made
                    # it look for the folder inside that build_tmp path and fail with
                    # "Unable to find ... when adding binary and data files." Absolute
                    # path sidesteps that entirely (same fix already used for $envFile).
                    $pgVendorAbs = (Resolve-Path $pgVendorDir).Path
                    $basePyiArgs += @("--add-data", "$pgVendorAbs;pgsql")
                }
                else {
                    Write-Warning "vendor/pgsql not found — build will not support Локал (local Postgres) storage mode."
                }
            }

            foreach ($moduleName in $appProfile.HiddenImports) {
                $basePyiArgs += @("--hidden-import", $moduleName)
            }

            $setupDistDir = Join-Path $buildDir "setup_dist"
            $setupWorkDir = Join-Path $workDir "setup"

            if (Test-Path $setupDistDir) { Remove-Item $setupDistDir -Recurse -Force }
            if (Test-Path $setupWorkDir) { Remove-Item $setupWorkDir -Recurse -Force }

            $setupPyiArgs = @()
            $setupPyiArgs += $basePyiArgs
            $setupPyiArgs += @(
                "--onedir",
                "--name", $appProfile.ExeBaseName,
                "--distpath", $setupDistDir,
                "--workpath", $setupWorkDir,
                "--specpath", $setupWorkDir,
                $entryScript
            )

            & $python @setupPyiArgs
            if ($LASTEXITCODE -ne 0) {
                throw "PyInstaller setup build failed for $appKey"
            }

            $exePath = "$setupDistDir/$($appProfile.ExeBaseName)/$($appProfile.ExeBaseName).exe"
            if (-not (Test-Path $exePath)) {
                throw "Setup build failed: $exePath not found"
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

            $installerScript = "scripts/CatManPro.installer.iss"
            if (-not (Test-Path $installerScript)) {
                throw "Installer script not found: $installerScript"
            }

            $issOutputDir = (Resolve-Path $setupDir).Path
            $issBuildDir = (Resolve-Path "$setupDistDir/$($appProfile.ExeBaseName)").Path
            $issMediaSource = ((Resolve-Path "media").Path + "\\*")

            $issArgs = @(
                "/DAppVersion=$resolvedVersion",
                "/DOutputBaseFilename=$($appProfile.ExeBaseName)-Setup-$resolvedVersion",
                "/DOutputDir=$issOutputDir",
                "/DBuildDirPath=$issBuildDir",
                "/DMediaSource=$issMediaSource",
                "/DMyAppName=$($appProfile.InstallerAppName)",
                "/DMyAppExeName=$($appProfile.ExeBaseName).exe",
                "/DMyAppId=$($appProfile.InstallerAppId)",
                "/DMyAppDirName=$($appProfile.ExeBaseName)",
                $installerScript
            )

            & $isccExe @issArgs
            if ($LASTEXITCODE -ne 0) {
                throw "Inno Setup compiler failed for $appKey"
            }

            $setupExeVersioned = "$setupDir/$($appProfile.ExeBaseName)-Setup-$resolvedVersion.exe"
            if (-not (Test-Path $setupExeVersioned)) {
                throw "Installer build failed: $setupExeVersioned not found"
            }

            # Also keep a stable name for tooling that expects a fixed path.
            $setupExeStable = "$setupDir/$($appProfile.ExeBaseName)-Setup.exe"
            Copy-Item $setupExeVersioned $setupExeStable -Force
            $builtOutputs += $setupExeVersioned
            Copy-BuildOutputNow $setupExeVersioned
        }

        if ($BuildApk) {
            $fletExe = ".venv/Scripts/flet.exe"
            if (-not (Test-Path $fletExe)) {
                throw "flet.exe not found at $fletExe"
            }

            # Flutter/Android SDK/Java are not on PATH by default on this
            # machine (confirmed empty JAVA_HOME/ANDROID_HOME/FLUTTER_HOME) —
            # same paths as scripts/build_android_stage.ps1's manual staging
            # script, just applied here too so `flet build apk` works from
            # this script without needing that separate one run first.
            if (-not $env:JAVA_HOME) { $env:JAVA_HOME = "$env:USERPROFILE/java/17.0.13+11" }
            if (-not $env:ANDROID_HOME) { $env:ANDROID_HOME = "$env:USERPROFILE/Android/sdk" }
            if (-not $env:FLUTTER_HOME) { $env:FLUTTER_HOME = "$env:USERPROFILE/flutter/3.41.4" }
            $env:Path = "$env:FLUTTER_HOME/bin;$env:ANDROID_HOME/cmdline-tools/12.0/bin;$env:ANDROID_HOME/platform-tools;$env:JAVA_HOME/bin;$env:Path"

            $targetApk = "$apkDir/$($appProfile.ExeBaseName)-android.apk"
            if (Test-Path $targetApk) {
                Remove-Item $targetApk -Force -ErrorAction SilentlyContinue
            }

            # flet build apk prints progress via `rich`, which on this machine's
            # non-UTF-8 console codepage (e.g. cp1251) crashes trying to encode
            # characters like "●" — force Python's own UTF-8 mode for this
            # subprocess so it doesn't fall back to the legacy console codepage.
            $env:PYTHONUTF8 = "1"
            $env:PYTHONIOENCODING = "utf-8"

            # flet's Android packaging step invokes Flutter's dart.bat directly
            # (bypassing flutter.bat), and dart.bat, unlike flutter.bat, never adds
            # Flutter's bundled portable git (mingit) to PATH. If git isn't also
            # installed system-wide, Flutter's own internal engine-version script
            # fails with "git is not recognized" and the whole build aborts. Add
            # mingit to PATH here so it's found either way.
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
                $mingit = Get-ChildItem -Path "$env:USERPROFILE\flutter" -Filter "git.exe" -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -like "*\mingit\cmd\git.exe" } | Select-Object -First 1
                if ($mingit) {
                    $env:Path = "$($mingit.DirectoryName);$env:Path"
                }
                else {
                    throw "git not found on PATH and Flutter's bundled mingit not found either - install Git for Windows or reinstall Flutter."
                }
            }

            # flet build apk packages the ENTIRE project directory as the app's
            # Python source bundle (unlike the PyInstaller desktop build above,
            # which only embeds what's explicitly passed via --add-data) — so any
            # dev-only file present in the repo root ends up shipped inside the
            # installed APK for every user unless excluded here. See build_release.ps1
            # history for the full rationale behind each excluded path below —
            # unchanged regardless of which app (main/superadmin) is being packaged,
            # since both live in the same monorepo full of sibling dev-only folders.
            & $fletExe build apk --yes --verbose --clear-cache --module-name $appProfile.ApkModuleName --project $appProfile.ApkProject --product $appProfile.ApkProduct --org $appProfile.ApkOrg --exclude .venv .program build dist __pycache__ logs .git .github .vscode ".ruff_cache" ".claude" "EDB PostgreSQL" quick_accounts.json super_admin_account.json config.txt media GSM_Parser database.py.bak backend backups secrets tools vendor "Telegram Боти" "Web Сайт" "Створення програми" "assets/ek_cache" pyarmor.bug.log gsm_last_parse.log __noop_delete_me.tmp "--exclude=.venv"
            if ($LASTEXITCODE -ne 0) {
                throw "flet build apk failed for $appKey"
            }

            $apk = $null
            if (Test-Path "build/apk") {
                $apk = Get-ChildItem -Path "build/apk" -Filter *.apk -Recurse | Sort-Object LastWriteTime -Descending | Select-Object -First 1
            }
            if (-not $apk) {
                $apk = Get-ChildItem -Path . -Filter *.apk -Recurse | Where-Object { $_.FullName -notlike "*$releaseRoot*" } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
            }
            if (-not $apk) {
                throw "APK file not found after build for $appKey"
            }

            Copy-Item $apk.FullName $targetApk -Force
            $builtOutputs += $targetApk
            Copy-BuildOutputNow $targetApk
        }

        Write-Host "$appKey done. Setup: $setupDir | APK: $apkDir"
    }

    Write-Host "Release root: $releaseRoot"
    foreach ($output in $builtOutputs) {
        Write-Host "Built: $output"
    }
}
finally {
    Write-Host "Restoring local dev data files (quick accounts / .env)..."
    foreach ($target in $sanitizeTargets) {
        $backupPath = Join-Path $sanitizeBackupDir $target
        if (Test-Path $backupPath) {
            Copy-Item $backupPath $target -Force
        }
    }
    Remove-Item $sanitizeBackupDir -Recurse -Force -ErrorAction SilentlyContinue
}
