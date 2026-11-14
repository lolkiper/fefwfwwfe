; Сборка на Windows: установите Inno Setup 6 и выполните iscc installer\TwitchDropsUnlink.iss
; Папка dist\TwitchDropsUnlink должна существовать (см. scripts\build_windows.ps1).

#define MyAppName "Twitch Drops Unlink"
#define MyAppVersion "1.3.3"
#define MyAppPublisher "Twitch Drops Unlink"
#define MyAppURL "https://github.com/lolkiper/fefwfwwfe"
#define MyAppExeName "TwitchDropsUnlink.exe"

[Setup]
AppId={{A3F4B2E1-9D8C-4F6A-B5E0-DROPUNLINK01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=..\release
OutputBaseFilename=TwitchDropsUnlink-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64

[Files]
Source: "..\dist\TwitchDropsUnlink\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent shellexec