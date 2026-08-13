#ifndef AppVersion
  #define AppVersion "1.0.1"
#endif

#ifndef OutputDir
  #define OutputDir "..\\.setup"
#endif

#ifndef OutputBaseFilename
  #define OutputBaseFilename "CatManCalculator-Setup"
#endif

#ifndef BuildDirPath
  #define BuildDirPath "..\\.setup\\build\\setup_dist\\CatManCalculator"
#endif

#ifndef MyAppName
  #define MyAppName "CatMan Калькулятори"
#endif

#ifndef MyAppExeName
  #define MyAppExeName "CatManCalculator.exe"
#endif

#ifndef MyAppId
  // Doubled "{{" is Inno's own escape for a literal "{" (a bare "{" starts a
  // constant reference like {app}) — kept distinct from CatManPro's and
  // CatManSupAdm's own AppId GUIDs so all three can be installed side by side.
  #define MyAppId "{{9C1B51CE-368C-4DDB-921C-F3095004EC35}"
#endif

#ifndef MyAppDirName
  #define MyAppDirName "CatManCalculator"
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

[Dirs]
// The app itself runs unprivileged (only the installer elevates), but it
// still needs to write saved calculations somewhere under {app} (see
// calculator_app.py's _prefs_path) — everything else under Program Files
// stays admin-only, so only this one subfolder gets an explicit grant.
Name: "{app}\SavedCalculations"; Permissions: users-modify

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
// Same belt-and-suspenders force-close as CatManPro.installer.iss — needed
// now that the real auto-updater (app_updater.py) launches this installer
// with /CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS instead of just opening a
// browser link to a page the user had to download+run by hand.
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F /T >nul 2>&1"; Flags: runhidden postinstall skipifsilent; StatusMsg: "Закриття попередньої версії..."; Check: IsUpdateInstall
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  WasAlreadyInstalled: Boolean;

// See CatManPro.installer.iss's own copy of this for why ssInstall (not
// InitializeSetup) is the correct hook point — {app} isn't resolved yet
// during InitializeSetup and expanding it there crashes the installer.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    WasAlreadyInstalled := FileExists(ExpandConstant('{app}\{#MyAppExeName}'));
end;

function IsUpdateInstall: Boolean;
begin
  Result := WasAlreadyInstalled;
end;
