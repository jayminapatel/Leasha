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
; The Leasha pictures, made by make_installer_art.py at each display scaling (2026-10-08).
WizardImageFile=art\wizard-100.png,art\wizard-125.png,art\wizard-150.png,art\wizard-175.png,art\wizard-200.png,art\wizard-250.png
WizardSmallImageFile=art\small-100.png,art\small-125.png,art\small-150.png,art\small-175.png,art\small-200.png,art\small-250.png
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
; the install (acceptance A3) - Settings, Models can download them later.
; 2026-10-08 (owner): one box per model Leasha needs, in the order and with the
; names of app/core/model_catalogue.py (Inno allows no "-" in a task name, so
; photo-tags is models\photo_tags); ticking the parent ticks them all.
; tests/unit/test_installer_script.py holds the two to each other.
Name: "models"; Description: "Download models now (needs the internet this once)"
Name: "models\search"; Description: "Meaning search: finds files by what they mean (about 67 MB)"
Name: "models\rerank"; Description: "Best results first: puts the closest matches first (about 80 MB)"
Name: "models\pictures"; Description: "Picture search: finds pictures from a description (about 590 MB)"; Flags: unchecked
Name: "models\photo_tags"; Description: "Photo tags and captions: tags and a caption for each photo (about 1.0 GB)"; Flags: unchecked
Name: "models\speech"; Description: "Speech in recordings: makes speech in audio and video searchable (about 291 MB)"; Flags: unchecked
Name: "models\chat"; Description: "Chat and Interpret: answers questions about your files (about 1.7 GB)"; Flags: unchecked
Name: "models\faces"; Description: "People in photos: finds faces so people can be named (about 275 MB). Only used when 'Recognise people in photos on this computer' is on"; Flags: unchecked
; Section 5: off by default, and never blocks - winget may be absent or refused.
Name: "libreoffice"; Description: "Also read .doc, .ppt and other older Office files (installs LibreOffice, about 400 MB, through winget)"; Flags: unchecked

[Run]
; One line per model ticked, each writing the settings file first if it is not
; there yet (WriteSettingsFile in [Code]). Inno does not look at a program's
; exit code (checked 2026-10-08), so a model that does not download never
; fails the install; it shows as missing in Settings, Models, which can
; download it later.
Filename: "{app}\leasha-cli.exe"; Parameters: "models download search"; StatusMsg: "Downloading the meaning search model (about 67 MB)..."; Flags: waituntilterminated runhidden; BeforeInstall: WriteSettingsFile; Tasks: models\search
Filename: "{app}\leasha-cli.exe"; Parameters: "models download rerank"; StatusMsg: "Downloading the model that puts the best results first (about 80 MB)..."; Flags: waituntilterminated runhidden; BeforeInstall: WriteSettingsFile; Tasks: models\rerank
Filename: "{app}\leasha-cli.exe"; Parameters: "models download pictures"; StatusMsg: "Downloading the picture search model (about 590 MB)..."; Flags: waituntilterminated runhidden; BeforeInstall: WriteSettingsFile; Tasks: models\pictures
Filename: "{app}\leasha-cli.exe"; Parameters: "models download photo-tags"; StatusMsg: "Downloading the photo tags and captions model (about 1.0 GB)..."; Flags: waituntilterminated runhidden; BeforeInstall: WriteSettingsFile; Tasks: models\photo_tags
Filename: "{app}\leasha-cli.exe"; Parameters: "models download speech"; StatusMsg: "Downloading the speech model (about 291 MB)..."; Flags: waituntilterminated runhidden; BeforeInstall: WriteSettingsFile; Tasks: models\speech
Filename: "{app}\leasha-cli.exe"; Parameters: "models download chat"; StatusMsg: "Downloading the Chat and Interpret model (about 1.7 GB)..."; Flags: waituntilterminated runhidden; BeforeInstall: WriteSettingsFile; Tasks: models\chat
Filename: "{app}\leasha-cli.exe"; Parameters: "models download faces"; StatusMsg: "Downloading the model that finds people in photos (about 275 MB)..."; Flags: waituntilterminated runhidden; BeforeInstall: WriteSettingsFile; Tasks: models\faces
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
    'The index can grow large - about half the size of everything it reads. ' +
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

{ 2026-10-08: written before the first model download as well as at
  ssPostInstall. Inno runs the [Run] entries before CurStepChanged(ssPostInstall)
  (checked with a probe installer that day), so the downloads started with no
  settings file: Leasha could not say where its model folder was, and on a new
  install nothing was downloaded. Each model line in [Run] calls this first
  (BeforeInstall); once the file is there it does nothing. }
procedure WriteSettingsFile();
var
  Data: String;
  Lines: TArrayOfString;
begin
  if not FileExists(EnvFile()) then begin
    Data := DataPage.Values[0];
    ForceDirectories(Data);
    { Only where things live. Every other setting keeps Leasha's own default
      (app/core/config.py), so a later version's better default is not
      overridden by a value this installer wrote once. }
    SetArrayLength(Lines, 5);
    Lines[0] := '# Written by the Leasha installer. Settings change it; edit by hand only if you must.';
    Lines[1] := 'DATA_PATH=' + Data;
    Lines[2] := 'PROJECT_PATH=' + ExpandConstant('{app}\_internal');
    Lines[3] := 'LOG_PATH=' + Data + '\logs';
    Lines[4] := '';
    SaveStringsToUTF8File(EnvFile(), Lines, False);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteSettingsFile();
end;
