"""Validate pull-request metadata, commits, changed paths, and changed files."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import NamedTuple

LINE_START = r"(?im)^\s*(?:(?:[^\w\s]|_)\s*)*"
CO_AUTHOR = re.compile(LINE_START + r"co-authored-by\s*:.*$")
GENERATED_WITH = re.compile(LINE_START + r"generated\s+with\b.*$")
FORBIDDEN_ATTRIBUTION = (
    (CO_AUTHOR, "each commit has a single author; remove the Co-Authored-By line"),
    (GENERATED_WITH, "remove the 'Generated with' signature"),
    (re.compile("🤖"), "remove the signature emoji"),
)
SIGNOFF = re.compile(r"(?im)^signed-off-by\s*:\s*(.+?)\s*<([^<>]+)>\s*$")
FORBIDDEN_SUFFIX = re.compile(r"(?i)(?:\.pln|\.bimproject[^/\\]*|\.bimlibrary|\.archive|\.pla)$")
# English types, plus the Portuguese ones of earlier fragments.
CHANGE_TYPES = (
    "added",
    "changed",
    "fixed",
    "security",
    "docs",
    "adicionado",
    "alterado",
    "corrigido",
    "segurança",
    "seguranca",
)
CHANGE_FRAGMENT = re.compile(rf"changes/[^/]+\.(?:{'|'.join(CHANGE_TYPES)})\.md")
BIMCLOUD_HOST = re.compile(
    r"(?i)(?<![a-z0-9-])"
    r"(?:<[^>\s/]+>|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:<[^>\s/]+>|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*"
    r"\.bimcloud\.com(?::[0-9]+)?"
)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
BEARER = re.compile(r"(?i)\bbearer\s+(?!<(?:removido|token)>)([A-Za-z0-9._~+/=-]{20,})")
SECRET_VALUE = re.compile(
    r"(?ix)(?:"
    r"[\"']?(?:session[-_]id|access_token|refresh_token)[\"']?\s*:\s*"
    r"[\"']([^\"']+)[\"']"
    r"|\b(?:session[-_]id|access_token|refresh_token)\b\s*=\s*[\"']([^\"']+)[\"']"
    r"|\b(?:session[-_]id|access_token|refresh_token)\b=([A-Z0-9._~+/=-]+)"
    r"|\bsession-id\b\s*:\s*([A-Z0-9._~+/=-]+)"
    r")"
)
EMAIL = re.compile(r"(?i)\b[A-Z0-9.!#$%&'*+/=?^_`{|}\[\]~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+\b")
ALLOWED_EMAILS = {"ettoretorres@hotmail.com", "noreply@github.com", "support@github.com"}
ALLOWED_SECRET_VALUES = {"<removido>", "<token>"}
ALLOWED_BIMCLOUD_HOSTS = {
    "attacker.bimcloud.com",
    "escritorio.bimcloud.com",
    "escritorio-data.bimcloud.com",
    "exemplo.bimcloud.com",
    "example.bimcloud.com",
    "outro.bimcloud.com",
    "tenant.bimcloud.com",
    "x.bimcloud.com",
}
RESERVED_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "exemplo.com",
    "exemplo.com.br",
}


class Finding(NamedTuple):
    location: str
    message: str
    fix: str

    def format(self) -> str:
        return f"{self.location}: {self.message}. How to fix: {self.fix}."


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def check_attribution(origin: str, text: str) -> list[Finding]:
    findings = []
    for pattern, fix in FORBIDDEN_ATTRIBUTION:
        for match in pattern.finditer(text):
            findings.append(
                Finding(
                    f"{origin}:{line_number(text, match.start())}",
                    "tool signature not allowed",
                    fix,
                )
            )
    return findings


def check_commit(
    commit: str,
    message: str,
    author_name: str | None = None,
    author_email: str | None = None,
) -> list[Finding]:
    findings = check_attribution(f"commit {commit}", message)
    signoffs = SIGNOFF.findall(message)
    if not signoffs:
        findings.append(
            Finding(
                f"commit {commit}:1",
                "missing Signed-off-by line",
                "redo the commit with git commit -s",
            )
        )
    elif (
        author_name is not None
        and author_email is not None
        and not any(
            signoff_matches_author(name, email, author_name, author_email)
            for name, email in signoffs
        )
    ):
        findings.append(
            Finding(
                f"commit {commit}:1",
                "Signed-off-by does not match the commit author",
                f"sign off as {author_name} <{author_email}>",
            )
        )
    return findings


def signoff_matches_author(
    signoff_name: str, signoff_email: str, author_name: str, author_email: str
) -> bool:
    same_name = signoff_name.strip() == author_name
    return same_name and (
        signoff_email.casefold() == author_email.casefold() or author_name.endswith("[bot]")
    )


def check_path(path: str) -> list[Finding]:
    if path.startswith("changes/") and not CHANGE_FRAGMENT.fullmatch(path):
        return [
            Finding(
                f"{path}:1",
                "invalid changelog fragment name",
                "use changes/<identifier>.<type>.md, with type added, changed, fixed, "
                "security or docs",
            )
        ]
    if FORBIDDEN_SUFFIX.search(path):
        return [
            Finding(
                f"{path}:1",
                "real project or backup file not allowed",
                "remove the file from the commit and use only anonymized text fixtures",
            )
        ]
    return []


def changelog_warnings(paths: list[str]) -> list[str]:
    """Non-blocking reminder for PRs that have no changelog entry."""
    if "CHANGELOG.md" in paths or any(CHANGE_FRAGMENT.fullmatch(path) for path in paths):
        return []
    return [
        "pull request without a changelog fragment; add changes/<identifier>.<type>.md "
        "if the change matters to users"
    ]


def is_allowed_email(address: str) -> bool:
    normalized = address.casefold()
    domain = normalized.rpartition("@")[2]
    return (
        normalized in ALLOWED_EMAILS
        or normalized.endswith("@users.noreply.github.com")
        or domain in RESERVED_EMAIL_DOMAINS
        or domain.endswith((".invalid", ".test"))
    )


def is_allowed_secret(value: str) -> bool:
    normalized = value.casefold()
    return normalized in ALLOWED_SECRET_VALUES or bool(normalized) and set(normalized) == {"0"}


def is_test_fixture(path: str, value: str) -> bool:
    """Whether a secret-like value is visibly fictitious and lives under tests/."""
    if not PurePosixPath(path).parts or PurePosixPath(path).parts[0] != "tests":
        return False
    normalized = value.casefold()
    markers = ("t0k3n", "s3gr3d0", "segredo", "dummy", "fake", "falso")
    return any(marker in normalized for marker in markers) or (
        normalized.startswith("eyj") and normalized.count(".") != 2
    )


def check_lines(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    findings = []
    for number, line in lines:
        location = f"{path}:{number}"
        for match in BIMCLOUD_HOST.finditer(line):
            host = match.group(0)
            if "<" not in host and host.casefold().split(":", 1)[0] not in ALLOWED_BIMCLOUD_HOSTS:
                findings.append(
                    Finding(
                        location,
                        f"real BIMcloud host not allowed ({host})",
                        "replace the server name with a placeholder such as <office>.bimcloud.com",
                    )
                )
        for match in JWT.finditer(line):
            if not is_test_fixture(path, match.group(0)):
                findings.append(
                    Finding(location, "possibly real JWT", "replace the value with <REMOVIDO>")
                )
        for match in BEARER.finditer(line):
            if not is_test_fixture(path, match.group(1)):
                findings.append(
                    Finding(
                        location,
                        "possibly real Bearer token",
                        "replace the value with Bearer <token>",
                    )
                )
        for match in SECRET_VALUE.finditer(line):
            value = next(group for group in match.groups() if group is not None)
            if not is_allowed_secret(value) and not is_test_fixture(path, value):
                findings.append(
                    Finding(
                        location,
                        "possibly real identifier or token",
                        "replace the value with <REMOVIDO>, <token> or only zeros",
                    )
                )
        for match in EMAIL.finditer(line):
            address = match.group(0)
            if not is_allowed_email(address):
                findings.append(
                    Finding(
                        location,
                        f"third-party e-mail not allowed ({address})",
                        "remove or anonymize the address",
                    )
                )
    return findings


def check_text(path: str, text: str) -> list[Finding]:
    return check_lines(path, list(enumerate(text.splitlines(), start=1)))


def run_git(*args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
    )
    return result.stdout


def read_commits(base: str, head: str) -> list[tuple[str, str, str, str]]:
    output = run_git("log", "--format=%H%x00%an%x00%ae%x00%B%x1e", f"{base}..{head}")
    commits = []
    for record in output.split("\x1e"):
        record = record.strip("\r\n")
        if not record:
            continue
        commit, author_name, author_email, message = record.split("\x00", maxsplit=3)
        commits.append((commit, author_name, author_email, message))
    return commits


def merge_base(base: str, head: str) -> str:
    return run_git("merge-base", base, head).strip()


def read_changed_paths(base: str, head: str) -> list[str]:
    output = run_git(
        "diff", "--name-only", "-z", "--diff-filter=ACMR", f"{base}...{head}", text=False
    )
    return [item.decode("utf-8", errors="replace") for item in output.split(b"\x00") if item]


def read_file_at_revision(revision: str, path: str) -> str | None:
    try:
        content = run_git("show", f"{revision}:{path}", text=False)
    except subprocess.CalledProcessError:
        return None
    if b"\x00" in content:
        return None
    return content.decode("utf-8", errors="replace")


def read_added_lines(base: str, head: str, path: str) -> list[tuple[int, str]]:
    before = read_file_at_revision(base, path)
    after = read_file_at_revision(head, path)
    if after is None:
        return []
    before_lines = before.splitlines() if before is not None else []
    after_lines = after.splitlines()
    added = []
    for tag, _start_before, _end_before, start_after, end_after in SequenceMatcher(
        None, before_lines, after_lines, autojunk=False
    ).get_opcodes():
        if tag in {"insert", "replace"}:
            added.extend((index + 1, after_lines[index]) for index in range(start_after, end_after))
    return added


def validate_pull_request(base: str, head: str, title: str, body: str) -> list[Finding]:
    findings = check_attribution("PR title", title)
    findings.extend(check_attribution("PR description", body))
    findings.extend(check_text("PR title", title))
    findings.extend(check_text("PR description", body))
    comparison_base = merge_base(base, head)
    for commit, author_name, author_email, message in read_commits(base, head):
        findings.extend(check_commit(commit, message, author_name, author_email))
    for path in read_changed_paths(base, head):
        findings.extend(check_path(path))
        findings.extend(check_lines(path, read_added_lines(comparison_base, head, path)))
    return findings


def validate_commit_message_file(path: str) -> list[Finding]:
    with Path(path).open(encoding="utf-8") as file:
        return check_commit("message", file.read())


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base SHA of the pull request")
    parser.add_argument("--head", help="head SHA of the pull request")
    parser.add_argument("--commit-message-file", help="validate only a commit message")
    args = parser.parse_args(argv)
    if args.commit_message_file and (args.base or args.head):
        parser.error("--commit-message-file cannot be combined with --base/--head")
    if not args.commit_message_file and (not args.base or not args.head):
        parser.error("give --base and --head")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    warnings: list[str] = []
    try:
        if args.commit_message_file:
            findings = validate_commit_message_file(args.commit_message_file)
        else:
            findings = validate_pull_request(
                args.base,
                args.head,
                os.environ.get("PR_TITLE", ""),
                os.environ.get("PR_BODY", ""),
            )
            warnings = changelog_warnings(read_changed_paths(args.base, args.head))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Could not validate the pull request: {exc}", file=sys.stderr)
        return 2

    for finding in findings:
        print(f"::error::{finding.format()}")
    for warning in warnings:
        print(f"::warning::{warning}")
    if findings:
        print(f"Validation failed with {len(findings)} problem(s).", file=sys.stderr)
        return 1
    print("Pull request rules validated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
