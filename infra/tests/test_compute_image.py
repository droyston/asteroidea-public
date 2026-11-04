"""The compute image holds exactly the shared analysis files.

The SDK wheel is not installed in the image: `ingest/Dockerfile.compute` COPYs
five `sdk/src/asteroidea` files and the `recipes` directory into a stub package
with an empty `__init__.py`. These docker tests run in `make compute-image`, after
the build (they need the daemon and the local tag). The tmpfs mount derives from
the Dockerfile's `ENV XDG_CACHE_HOME` line.
"""

import hashlib
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Final

import pytest

from asteroidea_infra.runner import execute

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DOCKERFILE: Final = REPO_ROOT / "ingest/Dockerfile.compute"
SDK_SRC: Final = REPO_ROOT / "sdk/src/asteroidea"
ENV_LINE: Final = re.compile(r"^ENV XDG_CACHE_HOME=(\S+)$", re.MULTILINE)
COPY_LINE: Final = re.compile(
    r"^COPY (?P<sources>sdk/src/asteroidea/\S+(?: sdk/src/asteroidea/\S+)*) "
    r"\$\{LAMBDA_TASK_ROOT\}/asteroidea/$",
    re.MULTILINE,
)
IMAGE: Final = "asteroidea-compute:local"
LAMBDA_HOME: Final = "/home/sbx_user1051"
TASK_ROOT: Final = "/var/task"
IMPORT: Final = (
    "import asteroidea.analysis, asteroidea._remote, asteroidea._blocks, "
    "asteroidea_ingest.compute_api, asteroidea_ingest.lfp_build"
)
SDK_FILES: Final = (
    "analysis.py",
    "_remote.py",
    "_blocks.py",
    "errors.py",
    "analysis_defaults.json",
)
RECIPES_DIR: Final = SDK_SRC / "recipes"
RECIPES_COPY: Final = (
    "COPY sdk/src/asteroidea/recipes ${LAMBDA_TASK_ROOT}/asteroidea/recipes"
)
RECIPE_FILES: Final = tuple(sorted(p.name for p in RECIPES_DIR.glob("*.json")))


def _cache_home() -> str:
    found = ENV_LINE.findall(DOCKERFILE.read_text())
    assert len(found) == 1, "Dockerfile.compute needs one ENV XDG_CACHE_HOME line"
    return str(found[0])


def _copied_files() -> list[str]:
    found = COPY_LINE.findall(DOCKERFILE.read_text())
    assert len(found) == 1, "Dockerfile.compute needs one COPY of the SDK files"
    return [PurePosixPath(p).name for p in str(found[0]).split()]


def test_the_dockerfile_copies_five_sdk_files_and_the_recipes_directory() -> None:
    assert sorted(_copied_files()) == sorted(SDK_FILES)
    assert RECIPE_FILES, "sdk/src/asteroidea/recipes holds no recipe file"
    lines = DOCKERFILE.read_text().splitlines()
    assert lines.count(RECIPES_COPY) == 1, "Dockerfile.compute needs the recipes COPY"
    for name in SDK_FILES:
        assert (SDK_SRC / name).is_file(), name


def _docker_python(code: str) -> tuple[int, str]:
    """Run ``code`` in a read-only container with Lambda's temp directory and HOME."""
    docker = shutil.which("docker")
    assert docker is not None, "docker is not on PATH"
    tmpfs = str(PurePosixPath(_cache_home()).parent)
    done = execute(
        [
            docker, "run", "--rm", "--platform", "linux/arm64", "--read-only",
            "--tmpfs", tmpfs, "-e", f"HOME={LAMBDA_HOME}",
            "--entrypoint", "python3", IMAGE, "-c", code,
        ],
        cwd=None,
    )  # fmt: skip
    return done.code, done.out + done.err


@pytest.mark.docker
def test_the_modules_import_on_a_read_only_filesystem() -> None:
    code, output = _docker_python(IMPORT)
    assert code == 0, output[-600:]


@pytest.mark.docker
def test_the_import_fails_for_a_module_the_image_does_not_hold() -> None:
    """The import test can fail: the stub package holds five files and no more."""
    code, output = _docker_python(f"{IMPORT}, asteroidea._sql")
    assert code != 0
    assert "No module named" in output


@pytest.mark.docker
def test_each_copied_sdk_file_equals_the_source_by_sha256() -> None:
    script = (
        "import hashlib, pathlib\n"
        f"for n in {list(SDK_FILES)!r}:\n"
        f"    data = pathlib.Path('{TASK_ROOT}/asteroidea', n).read_bytes()\n"
        "    print(n, hashlib.sha256(data).hexdigest())\n"
    )
    code, output = _docker_python(script)
    assert code == 0, output[-600:]
    in_image = dict(line.split() for line in output.strip().splitlines())
    assert set(in_image) == set(SDK_FILES)
    for name in SDK_FILES:
        want = hashlib.sha256((SDK_SRC / name).read_bytes()).hexdigest()
        assert in_image[name] == want, f"{name} in the image differs from sdk/src"


@pytest.mark.docker
def test_each_copied_recipe_file_equals_the_source_by_sha256() -> None:
    script = (
        "import hashlib, pathlib\n"
        f"d = pathlib.Path('{TASK_ROOT}/asteroidea/recipes')\n"
        "for p in sorted(d.iterdir()):\n"
        "    print(p.name, hashlib.sha256(p.read_bytes()).hexdigest())\n"
    )
    code, output = _docker_python(script)
    assert code == 0, output[-600:]
    in_image = dict(line.split() for line in output.strip().splitlines())
    assert set(in_image) == set(RECIPE_FILES)
    for name in RECIPE_FILES:
        want = hashlib.sha256((RECIPES_DIR / name).read_bytes()).hexdigest()
        assert in_image[name] == want, (
            f"recipes/{name} in the image differs from sdk/src"
        )


@pytest.mark.docker
def test_the_stub_package_holds_five_files_recipes_and_an_empty_init() -> None:
    script = (
        "import pathlib\n"
        f"d = pathlib.Path('{TASK_ROOT}/asteroidea')\n"
        "print(sorted(p.name for p in d.iterdir() if p.is_file()))\n"
        "print(sorted(p.name for p in d.iterdir()\n"
        "             if p.is_dir() and p.name != '__pycache__'))\n"
        "print((d / '__init__.py').stat().st_size)\n"
    )
    code, output = _docker_python(script)
    assert code == 0, output[-600:]
    files, dirs, size = output.strip().splitlines()
    assert files == str(sorted([*SDK_FILES, "__init__.py"]))
    assert dirs == "['recipes']"
    assert size == "0"


@pytest.mark.docker
def test_nibabel_is_not_installed() -> None:
    """Only nibabel is out: pandas stays, because hdmf (via pynwb) requires it."""
    code, output = _docker_python("import nibabel")
    assert code != 0
    assert "No module named 'nibabel'" in output


@pytest.mark.docker
@pytest.mark.parametrize("module", ["pyiceberg", "pyarrow"])
def test_the_commit_only_dependencies_are_not_installed(module: str) -> None:
    """Pyiceberg and pyarrow are excluded from the compute image."""
    code, output = _docker_python(f"import {module}")
    assert code != 0
    assert f"No module named '{module}'" in output
