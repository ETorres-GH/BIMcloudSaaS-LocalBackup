import re
from pathlib import Path

from bimcloud_backup.paths import APP_DIR_NAME
from bimcloud_backup.scheduler import TASK_NAME

ROOT = Path(__file__).resolve().parent.parent
ISS = (ROOT / "installer" / "BIMcloudBackup.iss").read_text(encoding="utf-8-sig")


def define(name: str) -> str:
    match = re.search(rf'^#define {name} "([^"]*)"', ISS, re.MULTILINE)
    assert match, f"#define {name} ausente"
    return match.group(1)


def setting(name: str) -> str:
    match = re.search(rf"^{name}=(.*)$", ISS, re.MULTILINE)
    assert match, f"{name} ausente"
    return match.group(1).strip()


def test_installs_per_user_without_administrator():
    assert setting("PrivilegesRequired") == "lowest"
    assert "PrivilegesRequiredOverridesAllowed" not in ISS
    assert setting("DefaultDirName").startswith("{autopf}")


def test_uninstall_removes_our_scheduled_task():
    assert define("TaskName") == TASK_NAME
    assert re.search(r"schtasks\.exe.*/Delete /F /TN \"\"\{#TaskName\}\"\"", ISS)


def test_uninstall_only_touches_program_files_when_the_user_agrees():
    assert define("DataDir") == APP_DIR_NAME
    assert "MB_DEFBUTTON2, IDNO) = IDYES" in ISS
    # Never a recursive delete of the data folders themselves, where backups could live.
    assert "DelTree(ExpandConstant('{userappdata}\\{#DataDir}')" not in ISS
    assert "DelTree(ExpandConstant('{localappdata}\\{#DataDir}')," not in ISS


def test_a_failed_logout_keeps_the_configuration_for_another_try():
    code = ISS[ISS.index("[Code]") :]
    assert re.search(r"not Exec\(ExpandConstant\('\{app\}\\{#AppExe\}'\), 'logout'", code)
    assert "(ResultCode <> 0)" in code
    failure = code[code.index("(ResultCode <> 0)") : code.index("usPostUninstall")]
    assert "RemoveUserData := False;" in failure
    assert "SuppressibleMsgBox(" in failure


def test_packages_the_built_executable_and_uses_the_program_icon():
    assert define("AppExe") == "BIMcloudBackup.exe"
    assert define("IconFile") == r"..\src\bimcloud_backup\assets\icon.ico"
    assert setting("OutputBaseFilename") == "BIMcloudBackup-Setup"


def test_release_attaches_the_installer_and_its_hash():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "install_inno.ps1" in workflow
    assert "dist/BIMcloudBackup-Setup.exe dist/BIMcloudBackup-Setup.exe.sha256" in workflow


def test_inno_setup_download_is_pinned_and_verified():
    script = (ROOT / "scripts" / "install_inno.ps1").read_text(encoding="utf-8-sig")
    assert re.search(r'^\$Version = "\d+\.\d+\.\d+"$', script, re.MULTILINE)
    assert re.search(r'^\$Sha256 = "[0-9a-f]{64}"$', script, re.MULTILINE)
    assert "Get-AuthenticodeSignature" in script
