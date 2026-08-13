#ifndef AppVersion
  #define AppVersion "1.2.1"
#endif

#ifndef OutputDir
  #define OutputDir "..\\.setup"
#endif

#ifndef OutputBaseFilename
  #define OutputBaseFilename "CatManPro-Setup"
#endif

#ifndef BuildDirPath
  #define BuildDirPath "..\\.setup\\build\\setup_dist\\CatManPro"
#endif

#ifndef MediaSource
  #define MediaSource "..\\media\\*"
#endif

#ifndef MyAppName
  #define MyAppName "CatMan Pro"
#endif

#ifndef MyAppExeName
  #define MyAppExeName "CatManPro.exe"
#endif

#ifndef MyAppId
  // Doubled "{{" is Inno's own escape for a literal "{" (a bare "{" starts a
  // constant reference like {app}) — the /DMyAppId override passed from
  // build_release.ps1 must use the same doubled-brace form.
  #define MyAppId "{{3E7A9DB5-6C9E-4E80-94A8-1B8D6D7F04A2}"
#endif

#ifndef MyAppDirName
  #define MyAppDirName "CatManPro"
#endif

#define MyAppPublisher "CatMan Pro"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppDirName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#BuildDirPath}\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MediaSource}"; DestDir: "{app}\media"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
// Belt-and-suspenders: force-close any lingering old-version process at the
// EXACT moment we're about to launch the freshly-installed one (both are
// "postinstall", run in listed order, right after the Finish page) — the
// user should never see the new version open side-by-side with a stale
// old window. The earlier file-copy step is already handled separately by
// CloseApplications=yes/RestartApplications=yes above (+ the /CLOSEAPPLICATIONS
// /FORCECLOSEAPPLICATIONS switches main.py's updater passes on the command
// line), via Inno's own Restart Manager integration — this entry does NOT
// replace that, it's an extra guarantee for this specific instant.
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F /T >nul 2>&1"; Flags: runhidden postinstall skipifsilent; StatusMsg: "Закриття попередньої версії..."; Check: IsUpdateInstall
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// ============================================================================
// ACCOUNT & DATA PRESERVATION POLICY
// ============================================================================
// This installer implements the following policies for user data:
//
// 1. FIRST INSTALL:
//    - No user accounts exist in {userappdata}\CatManPro\
//    - App will create empty/default account storage on first run
//    - Fresh start with no saved credentials
//
// 2. UPDATES:
//    - User's saved accounts (config.txt, quick_accounts.json,
//      super_admin_account.json) are preserved in {userappdata}\CatManPro\
//    - App continues to work with existing saved credentials
//    - User does not need to re-enter account information
//
// 3. RELEASE BUILD:
//    - Test account files are not included in the release
//    - All account data is created/managed by the app at runtime
//    - Separation: dev test accounts != release production accounts
// ============================================================================

var
  WasAlreadyInstalled: Boolean;

// {app} cannot be expanded inside InitializeSetup — Inno Setup hasn't
// resolved the install directory that early yet, and doing so crashes the
// installer immediately with "Internal error: An attempt was made to
// expand the 'app' constant before it was initialized" (confirmed live —
// this broke BOTH CatMan Pro's and CatMan SupAdm's installers outright,
// before the wizard could show anything at all). ssInstall is the correct,
// documented point: it fires right as installation is about to begin,
// after the directory page has already resolved {app}, and BEFORE [Files]
// has copied anything (so FileExists still reflects the pre-update state).
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    WasAlreadyInstalled := FileExists(ExpandConstant('{app}\{#MyAppExeName}'));
end;

function IsUpdateInstall: Boolean;
begin
  Result := WasAlreadyInstalled;
end;
