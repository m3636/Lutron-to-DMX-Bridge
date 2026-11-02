"""Bump the app version (the single source is __version__ in lutron_dmx/__init__.py).

    python tools/bump_version.py patch|minor|major|X.Y.Z

Normally run for you by the "New release" workflow (Actions tab), which also tests, commits and tags.
By hand: merge the change, then `git tag vX.Y.Z && git push origin vX.Y.Z`; CI publishes the GitHub Release.
"""

import re
import sys
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent / "lutron_dmx" / "__init__.py"
PATTERN = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.M)


def next_version(current: tuple[int, int, int], what: str) -> str:
    major, minor, patch = current
    if what == "major":
        return f"{major + 1}.0.0"
    if what == "minor":
        return f"{major}.{minor + 1}.0"
    if what == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if re.fullmatch(r"\d+\.\d+\.\d+", what):
        return what
    raise SystemExit(f"Unknown version step {what!r}: use patch, minor, major or X.Y.Z")


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit(__doc__)
    text = INIT.read_text(encoding="utf-8")
    match = PATTERN.search(text)
    if not match:
        raise SystemExit(f'No `__version__ = "X.Y.Z"` line found in {INIT}')
    old = ".".join(match.groups())
    current = tuple(int(g) for g in match.groups())
    new = next_version(current, argv[0])
    if tuple(int(n) for n in new.split(".")) <= current:
        raise SystemExit(f"Version {new} is not newer than the current {old}")
    INIT.write_text(PATTERN.sub(f'__version__ = "{new}"', text, count=1), encoding="utf-8")
    print(f"Version {old} -> {new}")


if __name__ == "__main__":
    main(sys.argv[1:])
