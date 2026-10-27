; Inno Setup script for Gama United CIP Maker (built by GitHub Actions).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6E0B7C1A-4C2B-4E8F-9B0E-6A9D3C2F1A55}
AppName=Gama United CIP Maker
AppVersion={#AppVersion}
AppPublisher=Gama United
AppPublisherURL=https://gamaunited.rs
DefaultDirName={autopf}\Gama CIP Maker
DefaultGroupName=Gama CIP Maker
DisableProgramGroupPage=yes
; Installs per user: no administrator rights needed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\installer
OutputBaseFilename=GamaCIPMaker-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\GamaCIPMaker.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut / Prečica na radnoj površini"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\GamaCIPMaker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Gama CIP Maker"; Filename: "{app}\GamaCIPMaker.exe"
Name: "{group}\Uninstall Gama CIP Maker"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Gama CIP Maker"; Filename: "{app}\GamaCIPMaker.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\GamaCIPMaker.exe"; Description: "Pokreni Gama CIP Maker"; Flags: nowait postinstall skipifsilent
