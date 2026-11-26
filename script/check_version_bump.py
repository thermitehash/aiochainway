"""Fail if src/ changed but pyproject's version is not above the latest v* tag.

Usage: python script/check_version_bump.py <base-ref>
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tomllib


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def _parse(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    if not match:
        raise SystemExit(f"not a SemVer X.Y.Z version: {version!r}")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    changed = _run("git", "diff", "--name-only", f"{base}...HEAD").split()
    if not any(path.startswith("src/") for path in changed):
        print("No changes under src/; no version bump needed.")
        return 0
    version = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"][
        "version"
    ]
    tags = [
        t
        for t in _run("git", "tag", "--list", "v*").split()
        if re.fullmatch(r"v\d+\.\d+\.\d+", t)
    ]
    if not tags:
        print(f"No release tags yet; {version} will be the first release.")
        return 0
    latest = max(tags, key=_parse)
    if _parse(version) <= _parse(latest):
        print(
            f"src/ changed but version {version} is not above the latest tag {latest}. Bump pyproject.toml."
        )
        return 1
    notes = pathlib.Path(f"docs/releases/v{version}.md")
    if not notes.is_file():
        print(f"Missing release notes: {notes}")
        return 1
    print(f"Version {version} > {latest}; release notes present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
