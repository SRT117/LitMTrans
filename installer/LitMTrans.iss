#define AppName "LitMTrans"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\LitMTrans"
#endif
#ifndef OutputDir
  #define OutputDir "..\installer_dist"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "LitMTrans-" + AppVersion + "-setup"
#endif

[Setup]
AppId={{ED51C826-275A-4DB7-A2B7-7AEE52F74117}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=SRT117
AppPublisherURL=https://github.com/SRT117/LitMTrans
AppSupportURL=https://github.com/SRT117/LitMTrans/issues
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile=..\resources\icon.ico
UninstallDisplayIcon={app}\LitMTrans.exe
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
WizardStyle=modern
LicenseFile=..\LICENSE

[InstallDelete]
; Remove only files owned by the previous PyInstaller build before copying the
; replacement. This avoids an in-place executable rename when a prior install
; was interrupted and its LitMTrans.exe is already absent.
Type: files; Name: "{app}\LitMTrans.exe"
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\LitMTrans.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\LitMTrans.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他选项："; Flags: unchecked

[Run]
Filename: "{app}\LitMTrans.exe"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
