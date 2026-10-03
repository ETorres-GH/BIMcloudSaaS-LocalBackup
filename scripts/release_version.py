"""Validate a release tag and write its version into the package.

The single source of the program version is ``__version__`` in
``src/bimcloud_backup/__init__.py`` (``pyproject.toml`` reads it through hatchling).
The release workflow calls this script with the Git tag before building the executable.

Usage:
    python scripts/release_version.py v0.2.0          # validate and write
    python scripts/release_version.py v0.2.0 --check  # validate only

Accepted tags: ``vMAJOR.MINOR.PATCH`` with an optional ``-alpha.N``, ``-beta.N`` or ``-rc.N``.
The version written to the package follows PEP 440 (``v0.2.0-rc.1`` -> ``0.2.0rc1``).
When ``GITHUB_OUTPUT`` is set, ``version``, ``tag`` and ``prerelease`` are appended to it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

INIT_FILE = Path(__file__).resolve().parent.parent / "src" / "bimcloud_backup" / "__init__.py"

# ASCII digits only (``\d`` also matches other Unicode digits); used with ``fullmatch``
# because ``$`` would accept a trailing newline.
TAG_PATTERN = re.compile(
    r"v(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>alpha|beta|rc)\.(?P<pre_n>0|[1-9][0-9]*))?"
)
VERSION_LINE = re.compile(r'^__version__ = "[^"]*"$', re.MULTILINE)
PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}


class TagError(ValueError):
    """The tag is not a valid release tag."""


def parse_tag(tag: str) -> tuple[str, bool]:
    """Return the PEP 440 version for ``tag`` and whether it is a pre-release."""
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise TagError(
            f"Invalid tag: {tag!r}. Use vMAJOR.MINOR.PATCH, with an optional -alpha.N, -beta.N "
            "or -rc.N (e.g. v0.2.0 or v0.2.0-rc.1)."
        )
    version = f"{match['major']}.{match['minor']}.{match['patch']}"
    if match["pre"] is None:
        return version, False
    return f"{version}{PEP440_PRE[match['pre']]}{match['pre_n']}", True


def write_version(version: str, init_file: Path = INIT_FILE) -> None:
    """Replace the ``__version__`` line of ``init_file`` with ``version``."""
    text = init_file.read_text(encoding="utf-8")
    new_text, count = VERSION_LINE.subn(f'__version__ = "{version}"', text)
    if count != 1:
        raise RuntimeError(f"Expected one __version__ line in {init_file}, found {count}.")
    init_file.write_text(new_text, encoding="utf-8")


def write_github_output(tag: str, version: str, prerelease: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as fh:
        fh.write(f"tag={tag}\nversion={version}\nprerelease={str(prerelease).lower()}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="Git tag, e.g. v0.2.0")
    parser.add_argument("--check", action="store_true", help="only validate, without writing")
    args = parser.parse_args(argv)

    try:
        version, prerelease = parse_tag(args.tag)
    except TagError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not args.check:
        write_version(version)
    write_github_output(args.tag, version, prerelease)
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
