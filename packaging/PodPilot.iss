; Inno Setup script. Build the PyInstaller folder first, then:
;   ISCC.exe /DAppVersion=0.2.0 packaging\PodPilot.iss
; Installs per-user (no admin prompt), so in-app updates can run silently.

#ifndef AppVersion
  #error Pass the version with /DAppVersion=x.y.z
#endif

[Setup]
AppId={{6F0C6B1E-6E2C-4C55-9B4B-3E1F0E3A9D21}
AppName=PodPilot
AppVersion={#AppVersion}
AppVerName=PodPilot {#AppVersion}
AppPublisher=mystic
AppPublisherURL=https://github.com/YliasR/PodPilot
AppSupportURL=https://github.com/YliasR/PodPilot/issues
AppUpdatesURL=https://github.com/YliasR/PodPilot/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\PodPilot
DefaultGroupName=PodPilot
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=PodPilot-Setup-{#AppVersion}
SetupIconFile=..\assets\podpilot.ico
UninstallDisplayIcon={app}\PodPilot.exe
UninstallDisplayName=PodPilot
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; drop libraries from the previous version so stale files don't linger after an update
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\PodPilot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PodPilot"; Filename: "{app}\PodPilot.exe"
Name: "{autodesktop}\PodPilot"; Filename: "{app}\PodPilot.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PodPilot.exe"; Description: "{cm:LaunchProgram,PodPilot}"; Flags: nowait postinstall skipifsilent
; in-app updates run the installer with /SILENT /RELAUNCH=1 and expect the app back afterwards
Filename: "{app}\PodPilot.exe"; Flags: nowait; Check: ShouldRelaunch

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;
