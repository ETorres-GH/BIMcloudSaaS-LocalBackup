import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCKS = ("requirements-pip.txt", "requirements-build.txt", "requirements-ci.txt")
INPUTS = ("requirements-pip.in", "requirements-build.in", "requirements-ci.in")
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "release.yml",
)


def _requirement_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def test_locked_requirements_pin_every_package_and_hash():
    for name in LOCKS:
        blocks = _requirement_blocks((ROOT / name).read_text(encoding="utf-8"))
        assert blocks, name
        for block in blocks:
            assert "==" in block[0], f"versão solta em {name}: {block[0]}"
            assert any(re.search(r"--hash=sha256:[0-9a-f]{64}", line) for line in block), block[0]


def test_lock_inputs_use_exact_versions():
    for name in INPUTS:
        requirements = [
            line
            for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        assert requirements and all(
            re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", r) for r in requirements
        )


def test_workflow_actions_use_commit_sha_with_version_comment():
    action = re.compile(r"uses:\s+[\w.-]+/[\w.-]+@([0-9a-f]{40})\s+#\s+v\d+\.\d+\.\d+")
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        assert text.count("uses:") == len(action.findall(text)), path.name

    dependabot = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: github-actions" in dependabot


def test_ci_and_build_only_install_verified_dependencies():
    ci = WORKFLOWS[0].read_text(encoding="utf-8")
    release = WORKFLOWS[1].read_text(encoding="utf-8")
    build = (ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8-sig")

    for text in (ci, release):
        assert (
            "run: python -m pip install --upgrade --require-hashes -r requirements-pip.txt" in text
        )
        assert "run: python -m pip install --require-hashes -r requirements-ci.txt" in text
        assert "run: python -m pip install --no-deps --no-build-isolation -e ." in text
    assert "--require-hashes -r requirements-pip.txt" in build
    assert "--require-hashes -r requirements-build.txt" in build
    assert "--no-deps --no-build-isolation -e ." in build
    assert "python -m pip_audit --strict --require-hashes" in ci
    assert "pip_audit --strict --require-hashes -r requirements-build.txt" in ci
    assert "--noupx" in build
