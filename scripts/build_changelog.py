"""Build a release section from ``changes/`` fragments.

Usage:
    python scripts/build_changelog.py v0.2.0 --output release-notes.md

The script moves the current ``[Unreleased]`` text and every valid fragment into a dated
release section, writes the same section body to ``--output`` (or stdout), and deletes the
consumed fragments. Running it again for a version already present only extracts its notes.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
CHANGES_DIR = ROOT / "changes"
UNRELEASED = "## [Unreleased]"
TYPE_HEADINGS = {
    "added": "Added",
    "changed": "Changed",
    "fixed": "Fixed",
    "security": "Security",
    "docs": "Documentation",
    # The Portuguese types of earlier fragments, under the same headings.
    "adicionado": "Added",
    "alterado": "Changed",
    "corrigido": "Fixed",
    "segurança": "Security",
    "seguranca": "Security",
}
FRAGMENT_PATTERN = re.compile(rf"(?P<identifier>.+)\.(?P<type>{'|'.join(TYPE_HEADINGS)})\.md")
TAG_PATTERN = re.compile(
    r"v?(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:alpha|beta|rc)\.(?:0|[1-9][0-9]*))?)"
)
SECTION_PATTERN = re.compile(r"(?m)^### (?P<title>[^\n]+?)[ \t]*$")


class ChangelogError(ValueError):
    """The changelog or one of its fragments is invalid."""


def parse_version(tag: str) -> str:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ChangelogError(f"Invalid version: {tag!r}. Use vMAJOR.MINOR.PATCH.")
    return match["version"]


def validate_release_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ChangelogError(f"Invalid date: {value!r}. Use YYYY-MM-DD.") from exc
    if parsed.isoformat() != value:
        raise ChangelogError(f"Invalid date: {value!r}. Use YYYY-MM-DD.")
    return value


def read_fragments(changes_dir: Path) -> tuple[dict[str, list[str]], list[Path]]:
    """Return fragment bodies by type and the files to remove, in deterministic order."""
    fragments: dict[str, list[str]] = defaultdict(list)
    consumed = []
    if not changes_dir.exists():
        return fragments, consumed
    for path in sorted(changes_dir.glob("*.md"), key=lambda item: item.name.casefold()):
        match = FRAGMENT_PATTERN.fullmatch(path.name)
        if match is None:
            allowed = ", ".join(TYPE_HEADINGS)
            raise ChangelogError(
                f"Invalid fragment: {path.name}. Use <identifier>.<type>.md; types: {allowed}."
            )
        body = path.read_text(encoding="utf-8").strip()
        if not body or not body.startswith("- "):
            raise ChangelogError(f"Fragment {path.name} must start with a Markdown item '- '.")
        fragments[match["type"]].append(body)
        consumed.append(path)
    return fragments, consumed


def split_unreleased(text: str) -> tuple[str, str, str]:
    """Return text through the Unreleased heading, its body, and all later releases."""
    marker = re.search(rf"(?m)^{re.escape(UNRELEASED)}[ \t]*$", text)
    if marker is None:
        raise ChangelogError(f"{UNRELEASED} not found in CHANGELOG.md.")
    next_release = re.search(r"(?m)^## ", text[marker.end() :])
    end = marker.end() + next_release.start() if next_release else len(text)
    return text[: marker.end()], text[marker.end() : end].strip(), text[end:].lstrip()


def split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(SECTION_PATTERN.finditer(body))
    if not matches:
        return body.strip(), []
    preamble = body[: matches[0].start()].strip()
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((match["title"].strip(), body[match.end() : end].strip()))
    return preamble, sections


def merge_fragments(body: str, fragments: dict[str, list[str]]) -> str:
    """Append fragments to matching Keep a Changelog sections."""
    preamble, sections = split_sections(body)
    by_title = {title: index for index, (title, _content) in enumerate(sections)}
    for fragment_type, title in TYPE_HEADINGS.items():
        additions = fragments.get(fragment_type, [])
        if not additions:
            continue
        addition = "\n".join(additions)
        if title in by_title:
            index = by_title[title]
            old_title, content = sections[index]
            sections[index] = (old_title, "\n".join(part for part in (content, addition) if part))
        else:
            by_title[title] = len(sections)
            sections.append((title, addition))
    parts = [preamble] if preamble else []
    parts.extend(f"### {title}\n\n{content}" for title, content in sections if content)
    return "\n\n".join(parts).strip()


def release_body(text: str, version: str) -> str | None:
    heading_pattern = (
        rf"(?m)^## \[{re.escape(version)}\]"
        rf"(?: - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})?[ \t]*$"
    )
    heading = re.search(heading_pattern, text)
    if heading is None:
        return None
    next_release = re.search(r"(?m)^## ", text[heading.end() :])
    end = heading.end() + next_release.start() if next_release else len(text)
    return text[heading.end() : end].strip()


def build_changelog(
    version: str,
    release_date: str,
    changelog: Path = CHANGELOG,
    changes_dir: Path = CHANGES_DIR,
    output: Path | None = None,
) -> str:
    """Build or extract ``version`` and return the Markdown used as release notes."""
    validate_release_date(release_date)
    text = changelog.read_text(encoding="utf-8")
    fragments, consumed = read_fragments(changes_dir)
    existing = release_body(text, version)
    new_text = None
    if existing is not None:
        if consumed:
            raise ChangelogError(
                f"Version {version} already exists, but there are fragments left. "
                "Prepare a new version or delete the fragments."
            )
        notes = existing
    else:
        prefix, unreleased_body, later_releases = split_unreleased(text)
        notes = merge_fragments(unreleased_body, fragments)
        if not notes:
            raise ChangelogError("There is no text in [Unreleased] nor fragments for the release.")
        new_text = f"{prefix}\n\n## [{version}] - {release_date}\n\n{notes}\n"
        if later_releases:
            new_text += f"\n{later_releases.rstrip()}\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{notes}\n", encoding="utf-8", newline="\n")
    if new_text is not None:
        changelog.write_text(new_text, encoding="utf-8", newline="\n")
        for path in consumed:
            path.unlink()
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="release tag, e.g. v0.2.0")
    parser.add_argument("--date", default=date.today().isoformat(), help="date YYYY-MM-DD")
    parser.add_argument("--output", type=Path, help="Markdown file for the release body")
    args = parser.parse_args(argv)
    try:
        version = parse_version(args.tag)
        notes = build_changelog(version, args.date, output=args.output)
    except (ChangelogError, OSError) as exc:
        print(f"Could not build the CHANGELOG: {exc}", file=sys.stderr)
        return 1
    if args.output is None:
        print(notes)
    else:
        print(f"CHANGELOG and notes of version {version} built; fragments consumed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
