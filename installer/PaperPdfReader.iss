#define AppName "Paper PDF Viewer"
#define AppExe "PaperPdfViewer.exe"
#define AppGuid "1A0146B3-695D-426D-B16F-E48F98CCFA4A"
#define LegacyAppName "Paper PDF Reader"
#define LegacyExe "PaperPdfReader.exe"
#ifndef AppVersion
  #error AppVersion is required
#endif

[Setup]
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={userpf}\{#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
SetupIconFile=..\paper_reader\icons\app.ico
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\release
OutputBaseFilename=PaperPdfViewer-{#AppVersion}-setup

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startmenuicon"; Description: "Create a Start menu shortcut"

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\PaperPdfViewer\*"; DestDir: "{app}"; Excludes: "data,data\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startmenuicon
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  UpdatingLegacyInstall: Boolean;

function IsLegacyInstall: Boolean;
var
  InstalledDir: String;
begin
  Result := RegQueryStringValue(HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppGuid}}_is1',
    'InstallLocation', InstalledDir) and
    (CompareText(RemoveBackslashUnlessRoot(InstalledDir), RemoveBackslashUnlessRoot(WizardDirValue)) = 0) and
    FileExists(ExpandConstant('{app}\{#LegacyExe}'));
end;

procedure RegisterExtraCloseApplicationsResources;
begin
  if IsLegacyInstall then
    RegisterExtraCloseApplicationsResource(ExpandConstant('{app}\{#LegacyExe}'));
end;

function UpdateShortcut(const LinkName: String): Boolean;
var
  Shell, Link: Variant;
  LegacyPath, NewPath, IconLocation: String;
begin
  Result := True;
  if not FileExists(LinkName) then
    Exit;
  LegacyPath := ExpandConstant('{app}\{#LegacyExe}');
  NewPath := ExpandConstant('{app}\{#AppExe}');
  try
    Shell := CreateOleObject('WScript.Shell');
    Link := Shell.CreateShortcut(LinkName);
    if CompareText(Link.TargetPath, LegacyPath) <> 0 then
      Exit;
    Link.TargetPath := NewPath;
    IconLocation := Link.IconLocation;
    if CompareText(Copy(IconLocation, 1, Length(LegacyPath) + 1), LegacyPath + ',') = 0 then
      Link.IconLocation := NewPath + Copy(IconLocation, Length(LegacyPath) + 1, Length(IconLocation));
    Link.Save;
  except
    Result := False;
    Log('Could not update previous shortcut: ' + LinkName + ' (' + GetExceptionMessage + ')');
  end;
end;

procedure CurStepChanged(Step: TSetupStep);
var
  DesktopLink, MenuLink: Boolean;
begin
  if Step = ssInstall then
    UpdatingLegacyInstall := IsLegacyInstall;
  if (Step = ssPostInstall) and UpdatingLegacyInstall then
  begin
    DesktopLink := UpdateShortcut(ExpandConstant('{autodesktop}\{#LegacyAppName}.lnk'));
    MenuLink := UpdateShortcut(ExpandConstant('{autoprograms}\{#LegacyAppName}.lnk'));
    if DesktopLink and MenuLink then
      DeleteFile(ExpandConstant('{app}\{#LegacyExe}'));
  end;
end;

procedure CurUninstallStepChanged(Step: TUninstallStep);
var
  DataPath: String;
begin
  if (Step <> usPostUninstall) or UninstallSilent then
    Exit;
  DataPath := ExpandConstant('{app}\data');
  if DirExists(DataPath) and
     (MsgBox('Also delete settings and PDF backups in:' + #13#10 + DataPath,
       mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
    DelTree(DataPath, True, True, True);
  RemoveDir(ExpandConstant('{app}'));
end;
