; Leasha's Windows installer (Inno Setup 6), order 202626082213 sections 4.2 and 4.3.
; Built by packaging\build.ps1 from the PyInstaller folder build\dist\Leasha.
;
; Decisions it carries out (the order's 2026-09-20 note): per-user by default with
; per-machine as an option; asks where the index goes, default
; %LOCALAPPDATA%\Leasha\Data, and checks free space; no update check; unsigned.
; Uninstall removes the program and never the index.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\Leasha"
#endif
#ifndef OutputDir
  #define OutputDir "..\build\installer"
#endif

[Setup]
AppId={{6F1D2C5E-4B7A-4E8B-9C3D-2A5E8F1B7D40}
AppName=Leasha
AppVersion={#AppVersion}
AppVerName=Leasha {#AppVersion}
AppPublisher=Jaymin Patel
DefaultDirName={autopf}\Leasha
DefaultGroupName=Leasha
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=Leasha-Setup-{#AppVersion}
SetupIconFile=..\assets\leasha.ico
UninstallDisplayIcon={app}\Leasha.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
LicenseFile=..\LICENSE

[Messages]
WelcomeLabel2=This installs Leasha {#AppVersion}, which searches the files and mail on this computer from a plain-English description.%n%nEverything stays on this computer: Leasha sends nothing anywhere.%n%nThis copy is not signed, so Windows may have warned you before it started. That is expected.

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Leasha"; Filename: "{app}\Leasha.exe"
Name: "{autodesktop}\Leasha"; Filename: "{app}\Leasha.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Put Leasha on the desktop"; Flags: unchecked
; Section 4.2 step 4: the models, so the first search works offline. Never fails
; the install (acceptance A3) - Settings can download them later.
Name: "models"; Description: "Download the search models now (about 200 MB; needs the internet this once)"
; Section 5: off by default, and never blocks - winget may be absent or refused.
Name: "libreoffice"; Description: "Also read .doc, .ppt and other older Office files (installs LibreOffice, about 400 MB, through winget)"; Flags: unchecked

[Run]
Filename: "{app}\leasha-cli.exe"; Parameters: "-c ""from app.core.model_fetch import fetch_at_install as f; raise SystemExit(f())"""; StatusMsg: "Downloading the search models..."; Flags: waituntilterminated; Tasks: models
Filename: "{cmd}"; Parameters: "/c winget install --id TheDocumentFoundation.LibreOffice -e --silent --accept-package-agreements --accept-source-agreements"; StatusMsg: "Installing LibreOffice through winget..."; Flags: waituntilterminated; Tasks: libreoffice
; Section 4.2 step 6 and acceptance A8: the health check, in a window that stays open.
Filename: "{cmd}"; Parameters: "/k """"{app}\leasha-cli.exe"" ""{app}\_internal\doctor.py"" --quick"""; Description: "Check the installation (a window lists each check)"; Flags: postinstall skipifsilent
Filename: "{app}\Leasha.exe"; Description: "Open Leasha now"; Flags: nowait postinstall skipifsilent

[Code]
var
  DataPage: TInputDirWizardPage;

const
  RequiredFreeGB = 300;

function EnvFile(): String;
begin
  Result := ExpandConstant('{app}\_internal\.env');
end;

procedure InitializeWizard();
begin
  DataPage := CreateInputDirPage(wpSelectDir,
    'Where to keep the index',
    'Leasha keeps what it learns about your files in one folder.',
    'The index can grow large - about a third of the size of everything it reads. ' +
    'Pick a drive with room to spare. Nothing in your own files is changed.',
    False, 'Leasha');
  DataPage.Add('Index folder:');
  DataPage.Values[0] := ExpandConstant('{localappdata}\Leasha\Data');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { An upgrade keeps the settings it already has, so it does not ask again. }
  Result := (PageID = DataPage.ID) and FileExists(EnvFile());
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  FreeMB, TotalMB: Cardinal;
  Drive, Message: String;
begin
  Result := True;
  if CurPageID = DataPage.ID then begin
    Drive := ExtractFileDrive(DataPage.Values[0]);
    if GetSpaceOnDisk(Drive, True, FreeMB, TotalMB) then
      if FreeMB div 1024 < RequiredFreeGB then begin
        { No line in this file may start with "[" (a new section) or "#" (a
          preprocessor command), however it is indented. }
        Message := Format('%s has %d GB free. A large index needs about %d GB.', [Drive, FreeMB div 1024, RequiredFreeGB]);
        Message := Message + #13#10#13#10 + 'Leasha will still work, and stops before the drive is full. Use this folder anyway?';
        Result := MsgBox(Message, mbConfirmation, MB_YESNO) = IDYES;
      end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Data: String;
  Lines: TArrayOfString;
begin
  if (CurStep = ssPostInstall) and not FileExists(EnvFile()) then begin
    Data := DataPage.Values[0];
    ForceDirectories(Data);
    SetArrayLength(Lines, 12);
    Lines[0] := '# Written by the Leasha installer. Settings change it; edit by hand only if you must.';
    Lines[1] := 'DATA_PATH=' + Data;
    Lines[2] := 'PROJECT_PATH=' + ExpandConstant('{app}\_internal');
    Lines[3] := 'LOG_PATH=' + Data + '\logs';
    Lines[4] := 'EMBED_MODEL=BAAI/bge-small-en-v1.5';
    Lines[5] := 'EMBED_DIM=384';
    Lines[6] := 'RERANK_ENABLED=true';
    Lines[7] := 'OLLAMA_URL=http://127.0.0.1:11434';
    Lines[8] := 'OLLAMA_MODEL=mistral';
    Lines[9] := 'MIN_FREE_GB=5';
    Lines[10] := 'REQUIRED_FREE_GB=' + IntToStr(RequiredFreeGB);
    Lines[11] := '';
    SaveStringsToUTF8File(EnvFile(), Lines, False);
  end;
end;
