import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_pr.py"

spec = importlib.util.spec_from_file_location("check_pr", SCRIPT)
check_pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_pr)


def messages(findings):
    return [finding.format() for finding in findings]


def test_commit_requires_signoff():
    findings = check_pr.check_commit("abc123", "feat: mudança")
    assert any("commit abc123:1" in item and "Signed-off-by" in item for item in messages(findings))


def test_commit_accepts_signoff():
    message = "feat: mudança\n\nSigned-off-by: Ettore Torres <ettoretorres@hotmail.com>"
    assert check_pr.check_commit("abc123", message) == []


@pytest.mark.parametrize(
    "attribution",
    [
        "Co-Authored-By: Pessoa <dev" + "@example.invalid>",
        "co-authored-by: Ferramenta <dev" + "@example.invalid>",
        "Generated with Ferramenta",
        "- Generated with [Ferramenta](https://example.invalid)",
        "🤖 Generated with [Ferramenta](https://example.invalid)",
        "> Co-authored-by: Ferramenta <dev" + "@example.invalid>",
        "- Co-authored-by: Ferramenta <dev" + "@example.invalid>",
        "_Generated with Ferramenta_",
        "🤖",
    ],
)
def test_rejects_tool_attribution(attribution):
    assert check_pr.check_attribution("descrição do PR", attribution)


def test_allows_generated_with_as_regular_prose():
    assert check_pr.check_attribution("descrição do PR", "Arquivo generated with PyInstaller") == []


def test_commit_signoff_must_match_author():
    message = "feat: mudança\n\nSigned-off-by: Outra Pessoa <outra@example.invalid>"
    findings = check_pr.check_commit("abc123", message, "Ettore Torres", "ettoretorres@hotmail.com")
    assert any("does not match the commit author" in finding.message for finding in findings)


def test_commit_accepts_dependabot_signoff_email():
    message = "chore: atualizar\n\nSigned-off-by: dependabot[bot] <support@github.com>"
    assert (
        check_pr.check_commit(
            "abc123",
            message,
            "dependabot[bot]",
            "49699333+dependabot[bot]@users.noreply.github.com",
        )
        == []
    )


@pytest.mark.parametrize(
    "path",
    [
        "cliente/modelo.pln",
        "cliente/modelo.BIMProject",
        "cliente/modelo.bimproject22",
        "cliente/base.bimlibrary",
        "cliente/copia.archive",
        "cliente/modelo.pla",
    ],
)
def test_rejects_project_and_backup_files(path):
    assert check_pr.check_path(path)


def test_allows_unrelated_file_suffixes():
    assert check_pr.check_path("docs/formatos-bimproject.md") == []


@pytest.mark.parametrize(
    "path",
    [
        "changes/24.adicionado.md",
        "changes/cancelar-backup.alterado.md",
        "changes/correção.corrigido.md",
        "changes/login.added.md",
        "changes/limits.security.md",
        "changes/segredos.segurança.md",
        "changes/segredos.seguranca.md",
        "changes/guia.docs.md",
    ],
)
def test_accepts_changelog_fragment_names(path):
    assert check_pr.check_path(path) == []


@pytest.mark.parametrize(
    "path",
    ["changes/sem-tipo.md", "changes/x.feature.md", "changes/x/y.docs.md", "changes/README.md"],
)
def test_rejects_invalid_changelog_fragment_names(path):
    assert "invalid changelog fragment name" in check_pr.check_path(path)[0].message


def test_changelog_warning_accepts_fragment_or_direct_release_update():
    assert check_pr.changelog_warnings(["src/a.py", "changes/24.corrigido.md"]) == []
    assert check_pr.changelog_warnings(["CHANGELOG.md"]) == []
    assert check_pr.changelog_warnings(["src/a.py"])


def test_rejects_real_bimcloud_hosts_and_allows_placeholders():
    real_host = "https://" + "cliente" + ".bimcloud.com/api"
    data_host = "https://" + "cliente-data" + ".bimcloud.com:443/api"
    assert len(check_pr.check_text("config.toml", f"{real_host}\n{data_host}")) == 2
    allowed = "https://<escritorio>.bimcloud.com e https://example.invalid"
    assert check_pr.check_text("README.md", allowed) == []


def test_allows_known_fictitious_hosts_and_test_values():
    fixtures = (
        "https://example.bimcloud.com\n"
        "https://exemplo.bimcloud.com\n"
        "https://attacker.bimcloud.com\n"
        "https://escritorio-data.bimcloud.com\n"
        "https://api.example.invalid\n"
        "access_token=T0K3N\n"
        "Bearer eyJhbGciOi.abc-def_ghi-jkl\n"
        "usuario@exemplo.invalid"
    )
    assert check_pr.check_text("tests/test_security.py", fixtures) == []


def test_allows_public_graphisoft_pages():
    text = "https://graphisoft.com/legal e https://help.graphisoft.com/guide"
    assert check_pr.check_text("README.md", text) == []


def test_rejects_tokens_and_allows_redacted_values():
    jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
    bearer = "Bearer " + "x" * 24
    access = "access_" + "token=segredo-real"
    findings = check_pr.check_text("captura.txt", f"{jwt}\n{bearer}\n{access}")
    assert len(findings) == 3

    safe = "access_" + "token=<REMOVIDO>\nrefresh_" + "token=<token>\nsession-id=000000"
    assert check_pr.check_text("exemplo.txt", safe) == []


def test_rejects_secret_values_in_json_config_query_and_header():
    access_key = "access_" + "token"
    refresh_key = "refresh_" + "token"
    lines = [
        f'"{access_key}": "segredo-json"',
        f'{refresh_key} = "segredo-config"',
        f"url?{access_key}=segredo-query",
        "session-" + "id: segredo-header",
    ]
    assert len(check_pr.check_text("captura.txt", "\n".join(lines))) == 4


def test_allows_secret_variables_and_expressions_in_source_code():
    access_key = "access_" + "token"
    text = f'{access_key} = response["{access_key}"]\n{access_key} = get_token()'
    assert check_pr.check_text("client.py", text) == []


def test_rejects_third_party_email_and_allows_approved_addresses():
    third_party = "pessoa" + "@cliente.com.br"
    assert check_pr.check_text("doc.md", third_party)
    allowed = (
        "ettoretorres@hotmail.com\n"
        "123+user@users.noreply.github.com\n"
        "49699333+dependabot[bot]@users.noreply.github.com\n"
        "noreply@github.com\n"
        "support@github.com\n"
        "usuario@exemplo.com\n"
        "usuario@exemplo.com.br\n"
        "usuario@exemplo.invalid\n"
        "usuario@sub.example.invalid"
    )
    assert check_pr.check_text("doc.md", allowed) == []


def test_every_finding_explains_how_to_fix():
    third_party = "pessoa" + "@cliente.com.br"
    finding = check_pr.check_text("doc.md", third_party)[0]
    assert finding.format().startswith("doc.md:1:")
    assert "How to fix:" in finding.format()


def git(repo, *args, env=None):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def test_validate_pull_request_reads_git_range(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Ettore Torres")
    git(tmp_path, "config", "user.email", "ettoretorres@hotmail.com")
    tracked = tmp_path / "arquivo.txt"
    tracked.write_text("inicial\n", encoding="utf-8")
    git(tmp_path, "add", "arquivo.txt")
    git(tmp_path, "commit", "-s", "-m", "chore: base")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    tracked.write_text("mudança\n", encoding="utf-8")
    git(tmp_path, "commit", "-am", "feat: sem assinatura")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    monkeypatch.chdir(tmp_path)

    findings = check_pr.validate_pull_request(base, head, "Título", "Descrição")
    assert any("missing Signed-off-by line" in finding.message for finding in findings)


def test_only_new_lines_of_a_changed_file_are_scanned(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Ettore Torres")
    git(tmp_path, "config", "user.email", "ettoretorres@hotmail.com")
    tracked = tmp_path / "arquivo.txt"
    old_email = "pessoa" + "@cliente.test"
    tracked.write_text(f"legado: {old_email}\n", encoding="utf-8")
    git(tmp_path, "add", "arquivo.txt")
    git(tmp_path, "commit", "-s", "-m", "chore: base")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    tracked.write_text(f"legado: {old_email}\nnova linha segura\n", encoding="utf-8")
    git(tmp_path, "commit", "-sam", "test: alterar arquivo")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    monkeypatch.chdir(tmp_path)

    assert check_pr.read_added_lines(base, head, "arquivo.txt") == [(2, "nova linha segura")]


def test_main_validates_commit_message_file(tmp_path, capsys):
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("feat: mudança\n", encoding="utf-8")
    assert check_pr.main(["--commit-message-file", str(message)]) == 1
    assert "git commit -s" in capsys.readouterr().out


def test_pull_request_metadata_is_scanned_for_sensitive_data(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Ettore Torres")
    git(tmp_path, "config", "user.email", "ettoretorres@hotmail.com")
    tracked = tmp_path / "arquivo.txt"
    tracked.write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "arquivo.txt")
    git(tmp_path, "commit", "-s", "-m", "chore: base")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    git(tmp_path, "switch", "-c", "feature")
    tracked.write_text("base\nnova\n", encoding="utf-8")
    git(tmp_path, "commit", "-sam", "feat: mudança")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    monkeypatch.chdir(tmp_path)

    body = "Servidor https://cliente-real" + ".bimcloud.com"
    findings = check_pr.validate_pull_request(base, head, "Título", body)
    assert any(finding.location == "PR description:1" for finding in findings)


def test_added_lines_are_compared_with_merge_base_when_main_advanced(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Ettore Torres")
    git(tmp_path, "config", "user.email", "ettoretorres@hotmail.com")
    tracked = tmp_path / "arquivo.txt"
    tracked.write_text("legado: pessoa@cliente.test\nsegunda\n", encoding="utf-8")
    git(tmp_path, "add", "arquivo.txt")
    git(tmp_path, "commit", "-s", "-m", "chore: base")
    git(tmp_path, "switch", "-c", "feature")
    tracked.write_text(
        "legado: pessoa@cliente.test\nsegunda\nnova linha segura\n", encoding="utf-8"
    )
    git(tmp_path, "commit", "-sam", "feat: mudança")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    git(tmp_path, "switch", "main")
    tracked.write_text("segunda\n", encoding="utf-8")
    git(tmp_path, "commit", "-sam", "chore: limpar legado")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    monkeypatch.chdir(tmp_path)

    assert check_pr.read_added_lines(check_pr.merge_base(base, head), head, "arquivo.txt") == [
        (3, "nova linha segura")
    ]
