; Manual Phase 7 installer definition. Compile only after building and signing the portable tree.
#ifndef SourceDir
  #define SourceDir "..\..\build\portable-windows-x64\main.dist"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0-alpha.1"
#endif

[Setup]
AppId={{41F4BF36-E5B6-48A4-A631-4DCB4725E9B0}
AppName=AIOrganizer
AppVersion={#AppVersion}
AppPublisher=AIOrganizer contributors
DefaultDirName={autopf}\AIOrganizer
DefaultGroupName=AIOrganizer
OutputDir=..\..\artifacts
OutputBaseFilename=AIOrganizer-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\AIOrganizer.exe
WizardStyle=modern

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AIOrganizer"; Filename: "{app}\AIOrganizer.exe"
Name: "{autodesktop}\AIOrganizer"; Filename: "{app}\AIOrganizer.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\AIOrganizer.exe"; Description: "Launch AIOrganizer"; Flags: nowait postinstall skipifsilent
