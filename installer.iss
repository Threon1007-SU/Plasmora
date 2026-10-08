#define AppName "Plasmora"
#define AppVersion "0.9.0"
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
SetupMutex=Local\PlasmoraSetupMutex
CloseApplications=no
RestartApplications=no

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

[Code]
const
  AppRunningMutex = 'Local\PlasmoraDesktopMutex';
  InstallInProgressMutex = 'Local\PlasmoraInstallInProgress';
  ShutdownEventName = 'Local\PlasmoraInstallerShutdown';

var
  InstallGuard: THandle;
  ShutdownRequest: THandle;

function CreateMutexW(Attributes: THandle; InitialOwner: Boolean; Name: String): THandle;
  external 'CreateMutexW@kernel32.dll stdcall';
function CreateEventW(Attributes: THandle; ManualReset, InitialState: Boolean; Name: String): THandle;
  external 'CreateEventW@kernel32.dll stdcall';
function SetEvent(Handle: THandle): Boolean;
  external 'SetEvent@kernel32.dll stdcall';
function ResetEvent(Handle: THandle): Boolean;
  external 'ResetEvent@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function CreateFileW(Name: String; Access, ShareMode: Cardinal; Security: THandle;
  Creation, Flags: Cardinal; Template: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall';

function ProgramFilesAvailable: Boolean;
var
  FileHandle: THandle;
  ProgramPath: String;
begin
  Result := False;
  if CheckForMutexes(AppRunningMutex) then Exit;
  ProgramPath := ExpandConstant('{app}\{#AppExeName}');
  if FileExists(ProgramPath) then begin
    { The frozen EXE launcher may outlive its window and mutex briefly. }
    FileHandle := CreateFileW(ProgramPath, $C0000000, 0, 0, 3, 128, 0);
    if FileHandle = THandle(-1) then Exit;
    CloseHandle(FileHandle);
  end;
  Result := True;
end;

procedure ReleaseInstallGuard;
begin
  if ShutdownRequest <> 0 then begin
    ResetEvent(ShutdownRequest);
    CloseHandle(ShutdownRequest);
    ShutdownRequest := 0;
  end;
  if InstallGuard <> 0 then begin
    CloseHandle(InstallGuard);
    InstallGuard := 0;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Attempt: Integer;
begin
  Result := '';
  if InstallGuard = 0 then
    InstallGuard := CreateMutexW(0, False, InstallInProgressMutex);
  if InstallGuard = 0 then begin
    Result := '无法准备程序更新，请退出安装器后重新运行。';
    Exit;
  end;
  if ProgramFilesAvailable then Exit;
  if ShutdownRequest = 0 then
    ShutdownRequest := CreateEventW(0, False, False, ShutdownEventName);
  if ShutdownRequest <> 0 then SetEvent(ShutdownRequest);
  for Attempt := 1 to 50 do begin
    if ProgramFilesAvailable then begin
      if ShutdownRequest <> 0 then ResetEvent(ShutdownRequest);
      Exit;
    end;
    Sleep(100);
  end;
  Result := 'Plasmora 仍在运行或程序文件尚未释放，安装尚未修改程序文件。' + #13#10#13#10 +
    '如正在导入、备份、恢复或迁移，请等待当前操作完成后点击“重试”。' + #13#10 +
    '从 0.7.2 或更早版本更新时，请先保存备注，在任务栏托盘中右键 Plasmora 图标并选择“退出 Plasmora”，再点击“重试”。' + #13#10 +
    '关闭窗口可能只是最小化到托盘。也可取消安装，稍后再更新。';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then begin
    if not ProgramFilesAvailable then
      RaiseException('Plasmora 又被打开了。请从托盘完全退出后重新安装。');
  end;
  if CurStep = ssPostInstall then ReleaseInstallGuard;
end;

procedure DeinitializeSetup;
begin
  ReleaseInstallGuard;
end;

function InitializeUninstall: Boolean;
begin
  Result := not CheckForMutexes(AppRunningMutex);
  if not Result then
    MsgBox('请先保存备注，从任务栏托盘右键 Plasmora 图标并选择“退出 Plasmora”，再运行卸载程序。', mbInformation, MB_OK);
end;
