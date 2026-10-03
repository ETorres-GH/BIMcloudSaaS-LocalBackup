; Installer of BIMcloud Backup Local (Inno Setup 6), in English or Brazilian Portuguese.
;
; Installs only for the current user, without asking for administrator rights, in
; %LOCALAPPDATA%\Programs. Built by scripts\build_exe.ps1 (when Inno Setup is installed) and by
; the release:
;   ISCC.exe /DAppVersion=0.2.0 /DAppVersionNumeric=0.2.0 installer\BIMcloudBackup.iss
; Needs dist\BIMcloudBackup.exe already built. The result is dist\BIMcloudBackup-Setup.exe.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric "0.0.0"
#endif

#define AppName "BIMcloud Backup Local"
#define AppExe "BIMcloudBackup.exe"
; Same name as scheduler.TASK_NAME and paths.APP_DIR_NAME.
#define TaskName "BIMcloudSaaS-LocalBackup"
#define DataDir "BIMcloudSaaS-LocalBackup"
#define IconFile "..\src\bimcloud_backup\assets\icon.ico"

[Setup]
; Fixed identifier: do not change it, or updates become a second installation.
AppId={{3700C2F1-04B8-4938-92F0-91363BC75842}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Ettore Torres
AppPublisherURL=https://github.com/ETorres-GH/BIMcloudSaaS-LocalBackup
AppSupportURL=https://github.com/ETorres-GH/BIMcloudSaaS-LocalBackup/issues
AppUpdatesURL=https://github.com/ETorres-GH/BIMcloudSaaS-LocalBackup/releases
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductVersion={#AppVersionNumeric}
VersionInfoDescription={#AppName} Setup
; Per user, without administrator: {autopf} becomes %LOCALAPPDATA%\Programs.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=BIMcloudBackup-Setup
#if FileExists(AddBackslash(SourcePath) + IconFile)
SetupIconFile={#IconFile}
#else
  #pragma message "Icon not available yet (" + IconFile + "); the installer gets the default icon."
#endif
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
; The program stays in the notification area: asks to close it before installing or removing.
; Same name as tray.MUTEX_NAME.
AppMutex=Local\{#TaskName}
; The first screen asks for the language, English first. The program then opens in the same one.
ShowLanguageDialog=yes
LanguageDetectionMethod=none

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[CustomMessages]
en.ProgramLanguage=en
ptbr.ProgramLanguage=pt-BR
en.RemoveUserData=Also delete the configuration, the logs and the saved BIMcloud sign-in from this computer?
ptbr.RemoveUserData=Apagar também a configuração, os logs e o acesso guardado ao BIMcloud deste computador?
en.BackupsKept=The backup folders are NOT deleted, in any case.
ptbr.BackupsKept=As pastas de backup NÃO são apagadas, em nenhum caso.
en.LogoutFailed=Could not delete the saved BIMcloud sign-in. The configuration and the logs were kept.
ptbr.LogoutFailed=Não foi possível apagar o acesso guardado ao BIMcloud. A configuração e os logs foram mantidos.
en.LogoutRetry=To try again, install and uninstall the program once more, or delete the "%1" credential in the Windows Credential Manager.
ptbr.LogoutRetry=Para tentar de novo, instale e desinstale o programa outra vez, ou apague a credencial "%1" no Gerenciador de Credenciais do Windows.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Registry]
; On uninstall, only removes the start with Windows that the program creates (tray.RUN_VALUE).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#TaskName}"; Flags: uninsdeletevalue dontcreatekey

[UninstallRun]
; Removes the automatic backup; without the task, schtasks only warns and the uninstall goes on.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""{#TaskName}"""; Flags: runhidden; RunOnceId: "RemoverTarefaAgendada"

[Code]
var
  RemoveUserData: Boolean;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Config: String;
begin
  // A new installation opens in the language chosen here, without asking again. An existing
  // configuration is never touched.
  if CurStep = ssPostInstall then
  begin
    Config := ExpandConstant('{userappdata}\{#DataDir}\config.toml');
    if not FileExists(Config) then
    begin
      ForceDirectories(ExtractFileDir(Config));
      SaveStringToFile(Config, '[interface]' + #13#10 + 'language = "' +
        CustomMessage('ProgramLanguage') + '"' + #13#10, False);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // The backups are never deleted. Configuration, logs and the saved sign-in only with a "Yes";
    // in a silent uninstall the answer is "No".
    RemoveUserData := SuppressibleMsgBox(
      CustomMessage('RemoveUserData') + #13#10#13#10 + CustomMessage('BackupsKept'),
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
    // With the program still installed: deletes the sign-in from the Credential Manager. If it
    // fails, the configuration stays: it says which server the sign-in is for, to try again.
    if RemoveUserData then
      if not Exec(ExpandConstant('{app}\{#AppExe}'), 'logout', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
         or (ResultCode <> 0) then
      begin
        RemoveUserData := False;
        SuppressibleMsgBox(
          CustomMessage('LogoutFailed') + #13#10#13#10 +
          FmtMessage(CustomMessage('LogoutRetry'), ['{#DataDir}']),
          mbError, MB_OK, IDOK);
      end;
  end;
  if (CurUninstallStep = usPostUninstall) and RemoveUserData then
  begin
    // Only the program's own files, by name: nothing the user may have put in these folders.
    DeleteFile(ExpandConstant('{userappdata}\{#DataDir}\config.toml'));
    DeleteFile(ExpandConstant('{userappdata}\{#DataDir}\last_run.json'));
    DelTree(ExpandConstant('{localappdata}\{#DataDir}\logs'), True, True, True);
    RemoveDir(ExpandConstant('{userappdata}\{#DataDir}'));
    RemoveDir(ExpandConstant('{localappdata}\{#DataDir}'));
  end;
end;
