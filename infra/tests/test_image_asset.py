"""The staged commit-image build context holds what the Dockerfile needs.

CDK stages the asset directory with `.dockerignore` before Docker sees it, so the
docker CLI's own handling of the same file proves nothing.
"""

import json
from pathlib import Path

import pytest

from asteroidea_infra.app import build_app

pytestmark = pytest.mark.synth

DOCKERFILE = "ingest/Dockerfile"


def _staged_context() -> Path:
    assembly = build_app().synth()
    root = Path(assembly.directory)
    contexts: list[Path] = []
    for manifest in root.glob("*.assets.json"):
        images = json.loads(manifest.read_text()).get("dockerImages", {})
        contexts += [
            root / image["source"]["directory"]
            for image in images.values()
            if image["source"].get("dockerFile") == DOCKERFILE
        ]
    assert len(contexts) == 1, contexts
    return contexts[0]


def test_staged_context_has_the_dockerfile_and_the_ingest_code() -> None:
    context = _staged_context()
    assert (context / "ingest/Dockerfile").is_file()
    assert (context / "ingest/src/asteroidea_ingest/commit.py").is_file()
    assert (context / "ingest/src/asteroidea_ingest/uploads_api.py").is_file()


def test_staged_context_keeps_harvester_code_out() -> None:
    context = _staged_context()
    assert not (context / "services/src/asteroidea_services/harvest").exists()
    assert not (context / "services/src/asteroidea_services/peek").exists()
