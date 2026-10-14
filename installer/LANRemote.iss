; ============================================================================
;  LAN Remote 安装包脚本（Inno Setup 6）
;
;  编译：
;      "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\LANRemote.iss
;      或指定版本：ISCC.exe /DAppVersion=2.0.1 installer\LANRemote.iss
;
;  产出：dist\LANRemote-Setup-<版本>.exe
;
;  设计说明：
;    * 默认安装到「当前用户目录」而非 Program Files —— 程序会在自身目录写入
;      cache/（设备记录、密钥）、logs/、downloads/，装在 Program Files 下会因
;      权限不足而无法写入；按用户安装同时避免 UAC 弹窗。
;    * 卸载时一并清理上述运行时目录，避免残留隐私数据。
; ============================================================================

#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif
#define AppName "LAN Remote"
#define AppNameCN "局域网远程控制"
#define AppPublisher "景同志"
#define AppExeName "LANRemote.exe"
#define AppURL "https://github.com/jingpanlong/LAN-Remote-Control"
#define SourceExe "..\dist\LANRemote.exe"

[Setup]
AppId={{7F1C3A84-5B62-4E1D-9C7A-2D8E4B6F0A31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion} ({#AppNameCN})
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=LANRemote-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} {#AppNameCN} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=Copyright (C) 2026 {#AppPublisher}. MIT License.

[Languages]
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 清理运行期生成的隐私数据（设备记录、密钥、日志、接收的文件）
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\downloads"
Type: dirifempty; Name: "{app}"
