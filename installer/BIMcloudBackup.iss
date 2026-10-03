; Instalador do BIMcloud Backup Local (Inno Setup 6).
;
; Instala só para o usuário atual, sem pedir administrador, em %LOCALAPPDATA%\Programs.
; Gerado por scripts\build_exe.ps1 (quando o Inno Setup está instalado) e pela Release:
;   ISCC.exe /DAppVersion=0.2.0 /DAppVersionNumeric=0.2.0 installer\BIMcloudBackup.iss
; Precisa de dist\BIMcloudBackup.exe já gerado. O resultado é dist\BIMcloudBackup-Setup.exe.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric "0.0.0"
#endif

#define AppName "BIMcloud Backup Local"
#define AppExe "BIMcloudBackup.exe"
; Mesmo nome de scheduler.TASK_NAME e de paths.APP_DIR_NAME.
#define TaskName "BIMcloudSaaS-LocalBackup"
#define DataDir "BIMcloudSaaS-LocalBackup"
#define IconFile "..\src\bimcloud_backup\assets\icon.ico"

[Setup]
; Identificador fixo: não mude, senão as atualizações viram uma segunda instalação.
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
VersionInfoDescription=Instalador do {#AppName}
; Por usuário, sem administrador: {autopf} vira %LOCALAPPDATA%\Programs.
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
  #pragma message "Ícone ainda não disponível (" + IconFile + "); o instalador sai com o ícone padrão."
#endif
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
; O programa fica na área de notificação: pede para fechá-lo antes de instalar ou remover.
; Mesmo nome de tray.MUTEX_NAME.
AppMutex=Local\{#TaskName}

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar um atalho na área de trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir o {#AppName}"; Flags: nowait postinstall skipifsilent

[Registry]
; Só remove, na desinstalação, a abertura com o Windows que o programa cria (tray.RUN_VALUE).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#TaskName}"; Flags: uninsdeletevalue dontcreatekey

[UninstallRun]
; Remove o backup automático; sem a tarefa, o schtasks só avisa e a desinstalação segue.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""{#TaskName}"""; Flags: runhidden; RunOnceId: "RemoverTarefaAgendada"

[Code]
var
  RemoveUserData: Boolean;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Os backups nunca são apagados. Configuração, logs e o acesso guardado só com um "Sim";
    // numa desinstalação silenciosa a resposta é "Não".
    RemoveUserData := SuppressibleMsgBox(
      'Apagar também a configuração, os logs e o acesso guardado ao BIMcloud deste computador?' + #13#10#13#10 +
      'As pastas de backup NÃO são apagadas, em nenhum caso.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
    // Ainda com o programa instalado: apaga o acesso do Gerenciador de Credenciais. Se falhar,
    // a configuração fica: é ela que diz de qual servidor é o acesso, para tentar de novo.
    if RemoveUserData then
      if not Exec(ExpandConstant('{app}\{#AppExe}'), 'logout', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
         or (ResultCode <> 0) then
      begin
        RemoveUserData := False;
        SuppressibleMsgBox(
          'Não foi possível apagar o acesso guardado ao BIMcloud. A configuração e os logs foram mantidos.' + #13#10#13#10 +
          'Para tentar de novo, instale e desinstale o programa outra vez, ou apague a credencial ' +
          '"{#DataDir}" no Gerenciador de Credenciais do Windows.',
          mbError, MB_OK, IDOK);
      end;
  end;
  if (CurUninstallStep = usPostUninstall) and RemoveUserData then
  begin
    // Só os arquivos do programa, pelo nome: nada que o usuário tenha posto nessas pastas.
    DeleteFile(ExpandConstant('{userappdata}\{#DataDir}\config.toml'));
    DeleteFile(ExpandConstant('{userappdata}\{#DataDir}\last_run.json'));
    DelTree(ExpandConstant('{localappdata}\{#DataDir}\logs'), True, True, True);
    RemoveDir(ExpandConstant('{userappdata}\{#DataDir}'));
    RemoveDir(ExpandConstant('{localappdata}\{#DataDir}'));
  end;
end;
