[Setup]
AppName=KillClip
AppVersion=1.0
DefaultDirName={autopf}\KillClip
DefaultGroupName=KillClip
OutputDir=installer
OutputBaseFilename=KillClip-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\KillClip.exe

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Zusätzliche Symbole:"

[Files]
Source: "dist\KillClip\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\KillClip"; Filename: "{app}\KillClip.exe"
Name: "{autodesktop}\KillClip"; Filename: "{app}\KillClip.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KillClip.exe"; Description: "KillClip starten"; Flags: nowait postinstall skipifsilent
