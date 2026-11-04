"""The extract image imports under Lambda's filesystem rules.

Lambda mounts the image read-only with a writable temp directory and a HOME that does
not exist (`/home/sbx_user1051`). pynwb creates a platformdirs cache directory at
import, so `ingest/Dockerfile.extract` sets `ENV XDG_CACHE_HOME` under that temp
directory. The docker tests derive the tmpfs mount from that ENV line (its parent),
so they read one value. The docker tests run in `make extract-image`, after the build
(they need the daemon and the local tag).
"""

import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Final

import pytest

from asteroidea_infra.runner import execute

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DOCKERFILE: Final = REPO_ROOT / "ingest/Dockerfile.extract"
ENV_LINE: Final = re.compile(r"^ENV XDG_CACHE_HOME=(\S+)$", re.MULTILINE)
IMAGE: Final = "asteroidea-extract:local"
LAMBDA_HOME: Final = "/home/sbx_user1051"
IMPORT: Final = "import asteroidea_ingest.extract.handler"


def _cache_home() -> str:
    found = ENV_LINE.findall(DOCKERFILE.read_text())
    assert len(found) == 1, "Dockerfile.extract needs one ENV XDG_CACHE_HOME line"
    return str(found[0])


def test_the_dockerfile_sets_the_cache_home_below_a_writable_directory() -> None:
    cache = PurePosixPath(_cache_home())
    assert cache.is_absolute()
    assert cache.parent != PurePosixPath("/"), "the cache home needs a writable parent"


def _import(*, image_env: bool) -> tuple[int, str]:
    """Run the import in a read-only container; ``image_env`` keeps the image's ENV."""
    docker = shutil.which("docker")
    assert docker is not None, "docker is not on PATH"
    tmpfs = str(PurePosixPath(_cache_home()).parent)
    # The image's ENV applies unless it is overridden with an empty value.
    override = [] if image_env else ["-e", "XDG_CACHE_HOME="]
    done = execute(
        [
            docker, "run", "--rm", "--platform", "linux/arm64", "--read-only",
            "--tmpfs", tmpfs, "-e", f"HOME={LAMBDA_HOME}", *override,
            "--entrypoint", "python3", IMAGE, "-c", IMPORT,
        ],
        cwd=None,
    )  # fmt: skip
    return done.code, done.out + done.err


@pytest.mark.docker
def test_the_handler_imports_on_a_read_only_filesystem() -> None:
    code, output = _import(image_env=True)
    assert code == 0, output[-600:]


@pytest.mark.docker
def test_the_import_fails_without_the_image_env() -> None:
    """Same container with the ENV blanked fails: the test above can fail."""
    code, output = _import(image_env=False)
    assert code != 0
    assert "Errno 30" in output
