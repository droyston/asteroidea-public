"""Image-dependency gate: no harvester code in the image.

`uv export --package asteroidea-ingest --no-dev --frozen` is the dependency set the
Dockerfile installs. It must list none of the harvester-only packages: h5py, remfile,
numpy or `asteroidea-services`. `requests` stays (pyiceberg depends on it). The export
reads `uv.lock` only; it needs no network (`--offline` makes that a requirement).
"""

import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Final

from asteroidea_infra.runner import execute

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INGEST: Final = "asteroidea-ingest"
SERVICES: Final = "asteroidea-services"
# awscrt: its S3 signer seeks the import's streamed body.
BANNED: Final = frozenset({"h5py", "remfile", "numpy", "awscrt", SERVICES})
EXPORT_FLAGS: Final = ("--no-dev", "--frozen", "--no-hashes", "--offline")
PINNED: Final = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")
EDITABLE: Final = re.compile(r"^-e (\S+)")


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _member_name(directory: str) -> str:
    """Return the project name of a workspace member from its pyproject.toml."""
    pyproject = REPO_ROOT / directory / "pyproject.toml"
    return str(tomllib.loads(pyproject.read_text())["project"]["name"])


def exported_packages(package: str, *extra_flags: str) -> set[str]:
    """Return the normalised package names `uv export` lists for `package`."""
    uv = shutil.which("uv")
    assert uv is not None, "uv is not on PATH"
    # `execute` is the one subprocess runner; no new suppression here.
    done = execute(
        [uv, "export", "--package", package, *EXPORT_FLAGS, *extra_flags],
        cwd=REPO_ROOT,
    )
    assert done.code == 0, done.err
    names: set[str] = set()
    for line in done.out.splitlines():
        if pinned := PINNED.match(line):
            names.add(_normal(pinned.group(1)))
        elif editable := EDITABLE.match(line):
            names.add(_normal(_member_name(editable.group(1))))
    return names


def test_commit_image_carries_no_harvester_dependency() -> None:
    listed = exported_packages(INGEST)
    assert listed & BANNED == set()
    assert "pyiceberg" in listed  # the export is real, not empty


def test_the_gate_can_fail() -> None:
    """Red proof, kept: the same export for the harvester lists the banned names."""
    assert exported_packages(SERVICES) & BANNED >= {"h5py", "remfile", "numpy"}


# -- asteroidea_infra modules in the commit image --------------

DOCKERFILE: Final = REPO_ROOT / "ingest" / "Dockerfile"
INGEST_SRC: Final = REPO_ROOT / "ingest" / "src" / "asteroidea_ingest"
INFRA_SRC: Final = "infra/src/asteroidea_infra/"
INFRA_IMPORT: Final = re.compile(
    r"^(?:from asteroidea_infra(?:\.(\w+))? import (\w+)"
    r"|import asteroidea_infra\.(\w+))",
    re.MULTILINE,
)
STDLIB_ONLY: Final = re.compile(r"^(?:from|import) (\w+)", re.MULTILINE)


def _imported_infra_modules() -> set[str]:
    """Return the `asteroidea_infra` module names the ingest package imports."""
    modules: set[str] = set()
    for source in INGEST_SRC.glob("*.py"):
        for sub, name, plain in INFRA_IMPORT.findall(source.read_text()):
            modules.add(sub or plain or name)
    return modules


def _copied_infra_modules() -> set[str]:
    """Return the `asteroidea_infra` module names the Dockerfile COPYs."""
    copied: set[str] = set()
    for line in DOCKERFILE.read_text().splitlines():
        if line.startswith("COPY ") and INFRA_SRC in line:
            copied.update(
                token.removeprefix(INFRA_SRC).removesuffix(".py")
                for token in line.split()
                if token.startswith(INFRA_SRC)
            )
    return copied


def test_every_infra_module_the_commit_imports_is_copied_into_the_image() -> None:
    imported = _imported_infra_modules()
    assert imported == {"schema"}  # the scan is real: commit.py imports schema
    assert imported | {"__init__"} <= _copied_infra_modules()


def test_copied_infra_modules_import_only_the_standard_library() -> None:
    for module in _copied_infra_modules():
        text = (REPO_ROOT / INFRA_SRC / f"{module}.py").read_text()
        roots = set(STDLIB_ONLY.findall(text))
        assert roots <= set(sys.stdlib_module_names), (module, roots)


def test_docker_context_admits_every_copied_infra_module() -> None:
    """`.dockerignore` excludes everything unless a line admits it.

    Measured: a build without these lines fails with "schema.py: not found".
    """
    admitted = set((REPO_ROOT / ".dockerignore").read_text().splitlines())
    for module in _copied_infra_modules():
        assert f"!{INFRA_SRC}{module}.py" in admitted, module


def test_extract_package_is_not_on_the_commit_image_import_path() -> None:
    """`extract/` needs pynwb, which the commit image does not install.

    Both images copy the whole package (one build context), so the commit image holds
    `extract/` files it never imports: no module outside `extract/` imports it.
    """
    package = REPO_ROOT / "ingest/src/asteroidea_ingest"
    modules = list(package.glob("*.py"))
    assert len(modules) > 5  # the scan is real
    for path in modules:
        assert "asteroidea_ingest.extract" not in path.read_text(), path.name
        assert "import extract" not in path.read_text(), path.name


def test_extract_export_lists_pynwb_and_h5py_and_no_services_package() -> None:
    """The extract image installs `--extra extract`; the commit export does not."""
    listed = exported_packages(INGEST, "--extra", "extract")
    assert {"pynwb", "h5py", "remfile", "nibabel", "pyiceberg"} <= listed
    assert SERVICES not in listed
    assert not {"pynwb", "nibabel", "hdmf", "pandas"} & exported_packages(INGEST)
