#define AppName "Paper PDF Reader"
#define AppExe "PaperPdfReader.exe"
#ifndef AppVersion
  #error AppVersion is required
#endif

[Setup]
AppId={{1A0146B3-695D-426D-B16F-E48F98CCFA4A}
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
OutputBaseFilename=PaperPdfReader-{#AppVersion}-setup

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startmenuicon"; Description: "Create a Start menu shortcut"

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\PaperPdfReader\*"; DestDir: "{app}"; Excludes: "data,data\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startmenuicon
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
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
