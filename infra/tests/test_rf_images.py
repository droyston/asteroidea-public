"""The SHA-pinned image rows of `make rf-budget` (`rf-images.sh`) and the tag step.

A fake `docker` on PATH answers `image inspect` from an environment table and records
`docker tag`, so the tests run offline. The gate reads only an image tagged with the
current `git rev-parse HEAD`: one tagged for another commit, or only the moving `:local`
tag, is not-measured and fails (unless `RF_BUDGET_OFFLINE=1`, which marks it).
"""

import os
from pathlib import Path
from typing import Final

import pytest

from asteroidea_infra.runner import Proc, execute

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "scripts" / "gates" / "rf-images.sh"
BUDGET: Final = REPO / "scripts" / "gates" / "rf-budget.sh"
EXTRACT_BASELINE: Final = 1061799673
MB: Final = 1024 * 1024
OTHER_SHA: Final = "0" * 40
FAKE_DOCKER: Final = """#!/bin/sh
# image inspect -f '{{.Size}}' <tag>: print the size FAKE_IMAGES holds, else fail.
if [ "$1" = image ] && [ "$2" = inspect ]; then
  tag=$5
  for pair in $(echo "$FAKE_IMAGES" | tr ';' ' '); do
    if [ "${pair%%=*}" = "$tag" ]; then echo "${pair#*=}"; exit 0; fi
  done
  exit 1
fi
if [ "$1" = tag ]; then echo "$2 $3" >> "$FAKE_LOG"; exit 0; fi
exit 1
"""


def head() -> str:
    done = execute(["git", "rev-parse", "HEAD"], cwd=REPO)
    assert done.code == 0
    return done.out.strip()


@pytest.fixture
def fake_docker(tmp_path: Path) -> Path:
    """Return a directory holding a `docker` that answers from FAKE_IMAGES."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "docker"
    exe.write_text(FAKE_DOCKER)
    exe.chmod(0o755)
    return bin_dir


def gate(fake_docker: Path, images: dict[str, int], **extra: str) -> Proc:
    env = {
        key: value for key, value in os.environ.items() if key != "RF_BUDGET_OFFLINE"
    }
    env.update(
        {
            "PATH": f"{fake_docker}:{os.environ['PATH']}",
            "FAKE_IMAGES": ";".join(f"{tag}={size}" for tag, size in images.items()),
            **extra,
        }
    )
    return execute(["sh", str(SCRIPT)], cwd=REPO, env=env)


def good_images(sha: str) -> dict[str, int]:
    return {
        f"asteroidea-compute:{sha}": 900_000_000,
        f"asteroidea-extract:{sha}": EXTRACT_BASELINE + 100,
    }


def test_images_tagged_for_head_are_measured_and_pass(fake_docker: Path) -> None:
    done = gate(fake_docker, good_images(head()))
    assert done.code == 0, done.out
    assert "900000000" in done.out
    assert str(EXTRACT_BASELINE + 100) in done.out
    assert "not-measured" not in done.out


def test_red_proof_images_tagged_for_another_sha_are_not_used(
    fake_docker: Path,
) -> None:
    done = gate(fake_docker, good_images(OTHER_SHA))
    assert done.code == 1
    assert done.out.count("not-measured") >= 2
    assert "900000000" not in done.out
    assert f"asteroidea-compute:{head()}" in done.out
    assert "another commit is not used" in done.out


def test_red_proof_only_the_moving_local_tag_is_not_used(fake_docker: Path) -> None:
    done = gate(
        fake_docker,
        {
            "asteroidea-compute:local": 1,
            "asteroidea-extract:local": EXTRACT_BASELINE,
        },
    )
    assert done.code == 1
    assert done.out.count("not-measured") >= 2


def test_one_image_missing_for_head_fails_that_row_only(fake_docker: Path) -> None:
    images = good_images(head())
    del images[f"asteroidea-extract:{head()}"]
    done = gate(fake_docker, images)
    assert done.code == 1
    assert "900000000" in done.out
    assert "asteroidea-extract:" in done.out
    assert done.out.count("not-measured") == 2  # the row value and the STOP line


def test_offline_marks_the_missing_images_and_passes(fake_docker: Path) -> None:
    done = gate(fake_docker, good_images(OTHER_SHA), RF_BUDGET_OFFLINE="1")
    assert done.code == 0, done.out
    assert done.out.count("RF_BUDGET_OFFLINE=1: passes, marked") == 2


def test_offline_does_not_hide_an_image_over_the_cap(fake_docker: Path) -> None:
    images = good_images(head())
    images[f"asteroidea-compute:{head()}"] = 1_200_000_001
    done = gate(fake_docker, images, RF_BUDGET_OFFLINE="1")
    assert done.code == 1
    assert "STOP (over IMAGE_MAX_BYTES)" in done.out


def test_the_extract_image_may_grow_by_1_mb_and_no_more(fake_docker: Path) -> None:
    images = good_images(head())
    images[f"asteroidea-extract:{head()}"] = EXTRACT_BASELINE + MB
    assert gate(fake_docker, images).code == 0
    images[f"asteroidea-extract:{head()}"] = EXTRACT_BASELINE + MB + 1
    done = gate(fake_docker, images)
    assert done.code == 1
    assert "STOP (growth over 1 MB)" in done.out


def test_rf_budget_calls_the_image_script_and_names_no_local_tag() -> None:
    text = BUDGET.read_text()
    assert "scripts/gates/rf-images.sh" in text
    code = [x for x in text.splitlines() if not x.lstrip().startswith("#")]
    assert not any(":local" in x for x in code)
    mine = [
        x for x in SCRIPT.read_text().splitlines() if not x.lstrip().startswith("#")
    ]
    assert not any(":local" in x for x in mine)


# --- the tag step of the two image targets ---------------------------------


def tag_run(
    tmp_path: Path, fake_docker: Path, *, dirty: bool
) -> tuple[str, list[str], str]:
    """Run TAG_IMAGE_SHA in a scratch repo; return its sha, the tags and the output."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_docker}:{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "tags.log"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def run(*cmd: str) -> str:
        done = execute(list(cmd), cwd=repo, env=env)
        assert done.code == 0, done.err
        return done.out

    run("git", "init", "-q")
    (repo / "a.txt").write_text("a")
    run("git", "add", "a.txt")
    run("git", "commit", "-q", "-m", "one")
    if dirty:
        (repo / "a.txt").write_text("changed")
    sha = run("git", "rev-parse", "HEAD").strip()
    include = f"include {REPO / 'Makefile'}"
    recipe = "\t@$(TAG_IMAGE_SHA) asteroidea-compute"
    (tmp_path / "tag.mk").write_text(
        f"{include}\n.PHONY: tagtest\ntagtest:\n{recipe}\n"
    )
    out = run("make", "-s", "-f", str(tmp_path / "tag.mk"), "tagtest")
    log = tmp_path / "tags.log"
    return sha, (log.read_text().splitlines() if log.exists() else []), out


def test_the_tag_step_adds_the_head_sha_tag_on_a_clean_tree(
    tmp_path: Path, fake_docker: Path
) -> None:
    sha, tags, out = tag_run(tmp_path, fake_docker, dirty=False)
    assert tags == [f"asteroidea-compute:local asteroidea-compute:{sha}"]
    assert f"tagged asteroidea-compute:{sha}" in out


def test_the_tag_step_refuses_to_tag_a_tree_with_uncommitted_changes(
    tmp_path: Path, fake_docker: Path
) -> None:
    _, tags, out = tag_run(tmp_path, fake_docker, dirty=True)
    assert tags == []
    assert "uncommitted changes" in out


@pytest.mark.parametrize(
    ("target", "repository", "build"),
    [
        ("compute-image", "asteroidea-compute", "-t asteroidea-compute:local ."),
        ("extract-image", "asteroidea-extract", "-t asteroidea-extract:local ."),
    ],
)
def test_the_image_targets_tag_the_sha_right_after_the_build(
    target: str, repository: str, build: str
) -> None:
    recipe = (REPO / "Makefile").read_text().split(f"\n{target}:", 1)[1]
    recipe = recipe.split("\n\n", 1)[0].splitlines()
    built = next(i for i, line in enumerate(recipe) if build in line)
    assert recipe[built + 1].strip() == f"@$(TAG_IMAGE_SHA) {repository}"
