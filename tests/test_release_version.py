import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_version.py"

spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
release_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_version)


def project_toml():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("tag", "version", "prerelease"),
    [
        ("v0.2.0", "0.2.0", False),
        ("v1.10.3", "1.10.3", False),
        ("v0.2.0-alpha.1", "0.2.0a1", True),
        ("v0.2.0-beta.2", "0.2.0b2", True),
        ("v0.2.0-rc.0", "0.2.0rc0", True),
    ],
)
def test_parse_valid_tag(tag, version, prerelease):
    assert release_version.parse_tag(tag) == (version, prerelease)


@pytest.mark.parametrize(
    "tag",
    [
        "0.2.0",
        "v0.2",
        "v0.2.0.1",
        "v01.2.0",
        "v0.2.0-rc",
        "v0.2.0-dev.1",
        "v0.2.0+build",
        "",
        "v1.2٢.3",  # Arabic-Indic digit
        "v1.2.3-rc.１",  # fullwidth digit
        "v1.2.3\n",
    ],
)
def test_parse_invalid_tag(tag):
    with pytest.raises(release_version.TagError):
        release_version.parse_tag(tag)


def test_write_version(tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text('"""Doc."""\n\n__version__ = "0.1.0.dev0"\n', encoding="utf-8")
    release_version.write_version("0.2.0", init)
    assert init.read_text(encoding="utf-8") == '"""Doc."""\n\n__version__ = "0.2.0"\n'


def test_write_version_requires_single_line(tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text('"""Doc."""\n', encoding="utf-8")
    with pytest.raises(RuntimeError):
        release_version.write_version("0.2.0", init)


def test_main_check_writes_github_output(tmp_path, monkeypatch, capsys):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert release_version.main(["v0.3.1-rc.2", "--check"]) == 0
    assert capsys.readouterr().out.strip() == "0.3.1rc2"
    expected = "tag=v0.3.1-rc.2\nversion=0.3.1rc2\nprerelease=true\n"
    assert output.read_text(encoding="utf-8") == expected


def test_main_rejects_invalid_tag(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert release_version.main(["versao-1", "--check"]) == 1
    assert "Tag inválida" in capsys.readouterr().err


def test_version_has_single_source():
    project = project_toml()["project"]
    assert "version" not in project
    assert project["dynamic"] == ["version"]
    hatch_path = ROOT / project_toml()["tool"]["hatch"]["version"]["path"]
    assert hatch_path.resolve() == release_version.INIT_FILE
    assert release_version.VERSION_LINE.search(hatch_path.read_text(encoding="utf-8"))
