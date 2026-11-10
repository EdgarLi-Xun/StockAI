; Per-user StockAI setup. Research data stays in %LOCALAPPDATA%\StockAI\workspace.
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef VersionInfo
  #error VersionInfo is required
#endif
#ifndef SourceDir
  #error SourceDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef IconFile
  #error IconFile is required
#endif

[Setup]
AppId={{7C4E9A2B-1D58-4E0F-9B33-6A8F0C2D5E17}
AppName=StockAI
AppVersion={#AppVersion}
AppPublisher=StockAI
VersionInfoVersion={#VersionInfo}
VersionInfoProductVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\StockAI
DefaultGroupName=StockAI
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=StockAI-{#AppVersion}-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\StockAI.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
MinVersion=10.0
UsePreviousAppDir=yes

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\StockAI"; Filename: "{app}\StockAI.exe"
Name: "{autodesktop}\StockAI"; Filename: "{app}\StockAI.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\StockAI.exe"; Description: "启动 StockAI"; Flags: nowait postinstall skipifsilent

[Code]
function IsWorkspace(Dir: String): Boolean;
begin
  Result := Pos('\stockai\workspace', LowerCase(Dir)) > 0;
end;

// 软件内升级会静默装到工作区 control\upgrades\installations\<计划号>\app，检查后再换到程序目录。
function IsUpgradeStaging(Dir: String): Boolean;
var
  Value: String;
begin
  Value := LowerCase(RemoveBackslash(Dir));
  Result := (Pos('\stockai\workspace\control\upgrades\installations\', Value) > 0) and (ExtractFileName(Value) = 'app');
end;

function IsBlockedInstallDir(Dir: String): Boolean;
begin
  Result := IsWorkspace(Dir) and not (WizardSilent and IsUpgradeStaging(Dir));
end;

function InitializeSetup(): Boolean;
var
  Dir: String;
begin
  Result := True;
  Dir := ExpandConstant('{param:DIR|}');
  if (Dir <> '') and IsBlockedInstallDir(Dir) then
  begin
    MsgBox('请不要把 StockAI 安装到工作区目录。工作区只保存研究资料。', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssInstall) and IsBlockedInstallDir(ExpandConstant('{app}')) then
  begin
    MsgBox('请不要把 StockAI 安装到工作区目录。工作区只保存研究资料。', mbError, MB_OK);
    Abort;
  end;
end;

function NextButtonClick(PageId: Integer): Boolean;
begin
  Result := True;
  if (PageId = wpSelectDir) and IsWorkspace(WizardDirValue) then
  begin
    MsgBox('请不要把 StockAI 安装到工作区目录。工作区只保存研究资料。', mbError, MB_OK);
    Result := False;
  end;
end;
