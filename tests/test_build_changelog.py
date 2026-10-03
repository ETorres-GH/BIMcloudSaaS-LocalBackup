import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_changelog.py"

spec = importlib.util.spec_from_file_location("build_changelog", SCRIPT)
build_changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_changelog)


def changelog_text() -> str:
    return """# Changelog

Introdução.

## [Unreleased]

### Added

- Item que já estava no Unreleased.

## [0.1.0] - 2026-09-01

### Added

- Primeira versão.
"""


def test_builds_release_notes_and_consumes_fragments(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(changelog_text(), encoding="utf-8")
    changes = tmp_path / "changes"
    changes.mkdir()
    first = changes / "12.added.md"
    first.write_text("- Item vindo do fragmento.\n", encoding="utf-8")
    second = changes / "corrigir-login.fixed.md"
    second.write_text("- Login corrigido.\n", encoding="utf-8")
    output = tmp_path / "release-notes.md"

    notes = build_changelog.build_changelog("0.2.0", "2026-09-28", changelog, changes, output)

    assert notes == (
        "### Added\n\n"
        "- Item que já estava no Unreleased.\n"
        "- Item vindo do fragmento.\n\n"
        "### Fixed\n\n"
        "- Login corrigido."
    )
    text = changelog.read_text(encoding="utf-8")
    assert "## [Unreleased]\n\n## [0.2.0] - 2026-09-28" in text
    assert text.index("## [0.2.0]") < text.index("## [0.1.0]")
    assert output.read_text(encoding="utf-8") == notes + "\n"
    assert not first.exists() and not second.exists()


def test_existing_release_is_only_extracted(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    original = changelog_text().replace(
        "## [0.1.0]", "## [0.2.0] - 2026-09-28\n\n### Corrigido\n\n- Pronto.\n\n## [0.1.0]"
    )
    changelog.write_text(original, encoding="utf-8")
    changes = tmp_path / "changes"
    changes.mkdir()

    notes = build_changelog.build_changelog("0.2.0", "2026-09-29", changelog, changes)

    assert notes == "### Corrigido\n\n- Pronto."
    assert changelog.read_text(encoding="utf-8") == original


def test_existing_release_rejects_remaining_fragment_without_changing_files(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    original = changelog_text().replace(
        "## [0.1.0]", "## [0.2.0] - 2026-09-28\n\n### Corrigido\n\n- Pronto.\n\n## [0.1.0]"
    )
    changelog.write_text(original, encoding="utf-8")
    changes = tmp_path / "changes"
    changes.mkdir()
    fragment = changes / "proxima-versao.alterado.md"
    fragment_text = "- Mudança posterior.\n"
    fragment.write_text(fragment_text, encoding="utf-8")
    output = tmp_path / "release-notes.md"

    with pytest.raises(
        build_changelog.ChangelogError,
        match="Prepare a new version or delete the fragments",
    ):
        build_changelog.build_changelog("0.2.0", "2026-09-29", changelog, changes, output)

    assert changelog.read_text(encoding="utf-8") == original
    assert fragment.read_text(encoding="utf-8") == fragment_text
    assert not output.exists()


@pytest.mark.parametrize("name", ["sem-tipo.md", "x.feature.md", ".adicionado.md", "README.md"])
def test_rejects_invalid_fragment_name_without_changing_files(tmp_path, name):
    changelog = tmp_path / "CHANGELOG.md"
    original = changelog_text()
    changelog.write_text(original, encoding="utf-8")
    changes = tmp_path / "changes"
    changes.mkdir()
    fragment = changes / name
    fragment.write_text("- Texto.\n", encoding="utf-8")

    with pytest.raises(build_changelog.ChangelogError, match="Invalid fragment"):
        build_changelog.build_changelog("0.2.0", "2026-09-28", changelog, changes)

    assert changelog.read_text(encoding="utf-8") == original
    assert fragment.exists()


def test_rejects_fragment_without_markdown_item(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(changelog_text(), encoding="utf-8")
    changes = tmp_path / "changes"
    changes.mkdir()
    (changes / "x.docs.md").write_text("Texto sem marcador.\n", encoding="utf-8")

    with pytest.raises(build_changelog.ChangelogError, match="must start"):
        build_changelog.build_changelog("0.2.0", "2026-09-28", changelog, changes)


def test_accepts_the_portuguese_types_of_earlier_fragments(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(changelog_text(), encoding="utf-8")
    changes = tmp_path / "changes"
    changes.mkdir()
    fragment = changes / "validacao.seguranca.md"
    fragment.write_text("- Validação reforçada.\n", encoding="utf-8")

    notes = build_changelog.build_changelog("0.2.0", "2026-09-28", changelog, changes)

    assert "### Security\n\n- Validação reforçada." in notes
    assert not fragment.exists()


def test_output_failure_does_not_consume_fragments_or_change_changelog(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    original = changelog_text()
    changelog.write_text(original, encoding="utf-8")
    changes = tmp_path / "changes"
    changes.mkdir()
    fragment = changes / "x.adicionado.md"
    fragment.write_text("- Texto.\n", encoding="utf-8")
    blocked_parent = tmp_path / "arquivo"
    blocked_parent.write_text("não é uma pasta", encoding="utf-8")

    with pytest.raises(OSError):
        build_changelog.build_changelog(
            "0.2.0",
            "2026-09-28",
            changelog,
            changes,
            blocked_parent / "release-notes.md",
        )

    assert changelog.read_text(encoding="utf-8") == original
    assert fragment.exists()


@pytest.mark.parametrize("tag", ["v0.2.0", "0.2.0", "v1.0.0-rc.1"])
def test_parse_version(tag):
    assert build_changelog.parse_version(tag) == tag.removeprefix("v")


def test_main_reports_invalid_version(capsys):
    assert build_changelog.main(["versão"]) == 1
    assert "Invalid version" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["28/09/2026", "2026-9-28", "2026-02-30"])
def test_rejects_invalid_release_date_without_changing_files(tmp_path, value):
    changelog = tmp_path / "CHANGELOG.md"
    original = changelog_text()
    changelog.write_text(original, encoding="utf-8")

    with pytest.raises(build_changelog.ChangelogError, match="Invalid date"):
        build_changelog.build_changelog("0.2.0", value, changelog, tmp_path / "changes")

    assert changelog.read_text(encoding="utf-8") == original
