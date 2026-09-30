#define AppName "Plasmora"
#define AppVersion "0.7.0"
#define AppPublisher "本机个人工具"
#define AppExeName "Plasmora.exe"

[Setup]
AppId={{4BAE8D69-9BB3-43DD-A730-CCB99231653F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Plasmora
UsePreviousAppDir=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir=release
OutputBaseFilename=Plasmora-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=app-icon.ico
UninstallDisplayIcon={app}\app-icon.ico
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "app-icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\PlasmidVault.exe"
Type: files; Name: "{autodesktop}\质粒仓库.lnk"
Type: files; Name: "{autoprograms}\质粒仓库.lnk"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\app-icon.ico"; IconIndex: 0
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\app-icon.ico"; IconIndex: 0

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 Plasmora"; Flags: nowait postinstall skipifsilent
