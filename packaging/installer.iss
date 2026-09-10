#define MyAppName "VANIOR PRINT"
#ifndef MyAppVersion
  #define MyAppVersion "0.6.6"
#endif
#define MyAppPublisher "Ivan Valevich"
#define MyAppExeName "VANIOR PRINT.exe"
#define MyAppURL "https://vanior-print.jonni2k25.chatgpt.site/"
#ifdef IndependentPreview
  #define MyInstallerBaseName "VANIOR PRINT Independent Setup v" + MyAppVersion
#else
  #define MyInstallerBaseName "VANIOR PRINT Setup v" + MyAppVersion
#endif

[Setup]
AppId={{58C2F64C-75AB-4F0F-9A15-4C6F46D7AA53}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename={#MyInstallerBaseName}
SetupIconFile=..\src\ai_print_optimizer\assets\vanior_print.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; A conventional non-solid ZIP payload is larger, but easier for security
; products to inspect than an ultra-compressed opaque stream.
Compression=zip/9
SolidCompression=no
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Установщик VANIOR PRINT
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCopyright=Copyright (C) 2026 Ivan Valevich
VersionInfoOriginalFileName=VANIOR PRINT Setup.exe
LicenseFile=TERMS_OF_USE_RU.txt
InfoBeforeFile=TESTER_README_RU.txt
SetupMutex=VANIOR_PRINT_SETUP_58C2F64C75AB4F0F9A154C6F46D7AA53

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"; Flags: unchecked

[Files]
Source: "..\dist\VANIOR PRINT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

#ifdef IndependentPreview
[InstallDelete]
; Inno Setup upgrades preserve files absent from a newer payload. Remove only
; the legacy bundled slicer when switching this installation to VANIOR Slice.
Type: filesandordirs; Name: "{app}\_internal\slicer"
#endif

[Icons]
Name: "{group}\VANIOR PRINT"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Удалить VANIOR PRINT"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VANIOR PRINT"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить VANIOR PRINT"; Flags: nowait postinstall skipifsilent
