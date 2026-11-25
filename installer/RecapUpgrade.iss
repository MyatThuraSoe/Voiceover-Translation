; RecapUpgrade production installer (Inno Setup 6).
;
; Build with:
;   "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\RecapUpgrade.iss
; or simply run build_exe.bat (it builds the app, verifies it, then runs this).
;
; The build is per-user: program files go to
;   %LOCALAPPDATA%\Programs\RecapUpgrade
; and no administrator rights are required. User data (config, presets, jobs,
; logs, models) is written to %LOCALAPPDATA%\RecapUpgrade by the app itself and
; is intentionally left in place when the app is uninstalled.

#define MyAppName "RecapUpgrade"
#define MyAppVersion "2.4.0"
#define MyAppPublisher "RecapUpgrade"
#define MyAppExeName "RecapUpgrade.exe"
#define MyAppId "{{8F0B4E8E-5A4C-4F2B-9E2E-6C1D7A3B9F10}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\RecapUpgrade
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=RecapUpgradeSetup
SetupIconFile=..\assets\RecapUpgrade.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion=2.4.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Everything PyInstaller and build_exe.ps1 produced, except personal user data
; (config.json / presets.json / .env are excluded by the build script).
Source: "..\dist\RecapUpgrade\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; User data under %LOCALAPPDATA%\RecapUpgrade is deliberately NOT deleted on
; uninstall, so settings, presets and finished jobs survive reinstall/upgrade.
