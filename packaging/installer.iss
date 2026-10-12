; Cody MCP Server - Windows installer (Inno Setup 6)
; Build with packaging\build.ps1, or:  iscc /DAppVersion=1.0.0 packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Cody MCP Server"
#define AppExe  "CodyMCPServer.exe"
#define RegKey  "Software\CodyMCPServer"

[Setup]
AppId={{6F1E5C3A-8B2D-4E97-A4C1-3D5B9E7F2A10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Joshua
DefaultDirName=D:\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
UsePreviousAppDir=yes
; Per-user install: no admin prompt, and the app's data folder is always writable.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer_output
OutputBaseFilename=CodyMCPServer-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start {#AppName} automatically when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist_app\CodyMCPServer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; The app reads this to find where logs and settings go (see app_paths.py).
Root: HKCU; Subkey: "{#RegKey}"; ValueType: string; ValueName: "DataDir"; ValueData: "{app}\data"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CodyMCPServer"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ Stops only the processes that run from THIS install folder (the GUI, the server it started,
  and the bundled ngrok). taskkill /IM would also kill an unrelated copy of the app running from
  somewhere else, e.g. a dev build. }
procedure StopInstalledApp;
var
  Ps: String;
  ResultCode: Integer;
begin
  Ps := '$d=''' + ExpandConstant('{app}') + '\''; ' +
        'Get-Process -ErrorAction SilentlyContinue | ' +
        'Where-Object { $_.Path -and $_.Path.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) -and $_.Name -notlike ''unins*'' } | ' +
        'Stop-Process -Force -ErrorAction SilentlyContinue';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Ps + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

var
  NgrokPage: TInputQueryWizardPage;
  UninstallDataDir: String;

procedure InitializeWizard;
begin
  NgrokPage := CreateInputQueryPage(wpSelectDir,
    'ngrok Setup',
    'Optional: your ngrok authtoken',
    '{#AppName} uses ngrok to give your computer a public HTTPS address that AI clients can ' +
    'connect to. ngrok is included, but it needs a free account.' + #13#10#13#10 +
    'Copy your authtoken from https://dashboard.ngrok.com/get-started/your-authtoken and paste it below. ' +
    'You can leave this empty if ngrok is already set up on this PC.');
  NgrokPage.Add('Authtoken:', False);
  { /NgrokToken=... pre-fills this (silent or pre-configured installs). }
  NgrokPage.Values[0] := ExpandConstant('{param:NgrokToken|}');
end;

function GetDataDir(Param: String): String;
begin
  Result := ExpandConstant('{app}\data');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Token: String;
begin
  Result := True;

  if CurPageID = NgrokPage.ID then
  begin
    Token := Trim(NgrokPage.Values[0]);
    if (Token <> '') and ((Pos(' ', Token) > 0) or (Pos('"', Token) > 0) or (Pos('''', Token) > 0)) then
    begin
      MsgBox('That does not look like an ngrok authtoken (it must not contain spaces or quotes). ' +
             'Paste it again, or leave the box empty to skip.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ Runs before Setup checks for locked files. The tray app hides instead of quitting and the server
  is a console child, so Restart Manager cannot close them; stop them ourselves on upgrade. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopInstalledApp;
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Token, DataDir: String;
begin
  if CurStep = ssPostInstall then
  begin
    DataDir := GetDataDir('');
    if not ForceDirectories(DataDir) then
      MsgBox('Could not create the data folder:' + #13#10 + DataDir + #13#10#13#10 +
             'The app will fall back to its default location.', mbError, MB_OK);

    { The token goes in the app's own ngrok config, never touching a system-wide ngrok setup. }
    Token := Trim(NgrokPage.Values[0]);
    if (Token <> '') and DirExists(DataDir) then
      SaveStringToFile(DataDir + '\ngrok.yml',
        'version: "3"' + #13#10 + 'agent:' + #13#10 + '  authtoken: ' + Token + #13#10, False);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    StopInstalledApp;
    RegQueryStringValue(HKCU, '{#RegKey}', 'DataDir', UninstallDataDir);
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    { Autostart may have been switched on from inside the app rather than by this installer. }
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CodyMCPServer');

    if (UninstallDataDir <> '') and DirExists(UninstallDataDir) and (not UninstallSilent) then
      if MsgBox('Also delete your {#AppName} data (logs, settings, workspaces, edit history)?' + #13#10#13#10 +
                UninstallDataDir + #13#10#13#10 +
                'Choose No to keep it for a future reinstall.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(UninstallDataDir, True, True, True);
  end;
end;
