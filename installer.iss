#ifndef MyVer
#define MyVer "1.0"
#endif

[Setup]
AppId={{9F2B6A1C-4D3E-4F7A-8B21-5C6D7E8F9A01}
SetupIconFile=assets\icon.ico
AppName=Kill App
AppVersion={#MyVer}
DefaultDirName={autopf}\Kill App
DefaultGroupName=Kill App
OutputDir=installer
OutputBaseFilename=KillApp-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\KillApp.exe

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Zusätzliche Symbole:"

[Files]
Source: "dist\KillApp\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Registry]
Root: HKCU; Subkey: "Software\Classes\killapp"; ValueType: string; ValueData: "URL:Kill App"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\killapp"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\killapp\shell\open\command"; ValueType: string; ValueData: """{app}\KillApp.exe"" ""%1"""

[Icons]
Name: "{group}\Kill App"; Filename: "{app}\KillApp.exe"
Name: "{autodesktop}\Kill App"; Filename: "{app}\KillApp.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KillApp.exe"; Description: "Kill App starten"; Flags: nowait postinstall
