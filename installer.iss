#ifndef MyVer
#define MyVer "1.0"
#endif

[Setup]
AppId={{9F2B6A1C-4D3E-4F7A-8B21-5C6D7E8F9A01}
SetupIconFile=assets\icon.ico
AppName=ACE
AppVersion={#MyVer}
DefaultDirName={autopf}\ACE
DefaultGroupName=ACE
OutputDir=installer
OutputBaseFilename=ACE-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\ACE.exe

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Zusätzliche Symbole:"

[Files]
Source: "dist\ACE\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Registry]
Root: HKCU; Subkey: "Software\Classes\ace"; ValueType: string; ValueData: "URL:ACE"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\ace"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\ace\shell\open\command"; ValueType: string; ValueData: """{app}\ACE.exe"" ""%1"""

[InstallDelete]
Type: files; Name: "{app}\KillApp.exe"
Type: files; Name: "{group}\Kill App.lnk"
Type: files; Name: "{autodesktop}\Kill App.lnk"

[Icons]
Name: "{group}\ACE"; Filename: "{app}\ACE.exe"
Name: "{autodesktop}\ACE"; Filename: "{app}\ACE.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ACE.exe"; Description: "ACE starten"; Flags: nowait postinstall
