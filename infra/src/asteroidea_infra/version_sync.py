"""Write the project version from the root `VERSION` file into each place that holds it.

The one project version lives in `VERSION` (SemVer `X.Y.Z`). `make version-sync` runs
this module, then regenerates `uv.lock` and the web lockfile. The places it writes:

- `version` in the `[project]` table of each workspace member's `pyproject.toml`;
- `version` in `web/package.json`;
- the harvester version in the harvest golden index, which the harvester reads from the
  installed `asteroidea-services` metadata.

The services User-Agent reads the installed `asteroidea-services` metadata at import,
so it needs no write. `infra/tests/test_version.py` pins that each place agrees with
`VERSION`.
"""

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[3]
SEMVER: Final = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
MEMBERS: Final = ("infra", "ingest", "services", "sdk")
PACKAGE_JSON: Final = Path("web/package.json")
HARVEST_GOLDEN: Final = Path("services/tests/fixtures/harvest/expected-index.json")

_PYPROJECT_VERSION: Final = re.compile(r'(?m)^version = "[^"\n]*"$')
_JSON_VERSION: Final = re.compile(r'"version": "[^"\n]*"')
_HARVESTER_VERSION: Final = re.compile(
    r'("harvester": \{\s*"package": "asteroidea-services",\s*"version": ")[^"\n]*(")'
)


class VersionError(ValueError):
    """`VERSION` is not SemVer, or a file lacks the field this module rewrites."""


def read_version(root: Path = REPO) -> str:
    """Return the project version from `root/VERSION`; raise unless it is `X.Y.Z`."""
    text = (root / "VERSION").read_text(encoding="utf-8")
    version = text.strip()
    if text != f"{version}\n" or not SEMVER.fullmatch(version):
        msg = f"{root / 'VERSION'} must hold one line X.Y.Z, got {text!r}"
        raise VersionError(msg)
    return version


def pyproject_paths(root: Path = REPO) -> list[Path]:
    """Return the `pyproject.toml` of each workspace member."""
    return [root / member / "pyproject.toml" for member in MEMBERS]


def _replace_once(path: Path, pattern: re.Pattern[str], replacement: str) -> bool:
    text = path.read_text(encoding="utf-8")
    new, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        msg = f"{path}: no version field matching {pattern.pattern!r}"
        raise VersionError(msg)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def sync(root: Path = REPO) -> list[Path]:
    """Write `VERSION` into each manifest under `root`; return the changed files."""
    version = read_version(root)
    changed: list[Path] = []
    for path in pyproject_paths(root):
        if _replace_once(path, _PYPROJECT_VERSION, f'version = "{version}"'):
            changed.append(path)
        project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        if project["version"] != version:
            msg = f"{path}: the first version line is not the [project] version"
            raise VersionError(msg)
    package_json = root / PACKAGE_JSON
    if _replace_once(package_json, _JSON_VERSION, f'"version": "{version}"'):
        changed.append(package_json)
    if json.loads(package_json.read_text(encoding="utf-8"))["version"] != version:
        msg = f"{package_json}: the first version field is not the package version"
        raise VersionError(msg)
    golden = root / HARVEST_GOLDEN
    if _replace_once(golden, _HARVESTER_VERSION, rf"\g<1>{version}\g<2>"):
        changed.append(golden)
    return changed


def main() -> int:
    """Sync the manifests of this checkout and report each file that changed."""
    try:
        changed = sync()
    except VersionError as err:
        sys.stderr.write(f"version-sync: {err}\n")
        return 1
    version = read_version()
    for path in changed:
        sys.stdout.write(
            f"version-sync: wrote {version} into {path.relative_to(REPO)}\n"
        )
    if not changed:
        sys.stdout.write(f"version-sync: every manifest already holds {version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
