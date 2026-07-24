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

function IsFirstInstall: Boolean;
begin
  Result := not FileExists(ExpandConstant('{app}\{#MyAppExeName}'));
end;

function InitializeSetup: Boolean;
var
  ResultCode: Integer;
begin
  // Extra safety for in-app updates: terminate running app so files can be replaced
  // and post-install launch starts the new version, not the old locked process.
  Exec(
    ExpandConstant('{cmd}'),
    '/C taskkill /IM {#MyAppExeName} /F /T >nul 2>&1',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Result := True;
end;
