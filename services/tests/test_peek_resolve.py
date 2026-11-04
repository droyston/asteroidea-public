import json
from pathlib import Path

import pytest

from asteroidea_services.http import Request, Response
from asteroidea_services.peek.count import PeekError
from asteroidea_services.peek.resolve import asset_url, resolve

ASSET = Path(__file__).parent / "fixtures" / "peek" / "asset-20257c14.json"
ARGS = ("000576", "0.231010.1811", "20257c14-af6a-4f3a-86c4-0b9bfc120714")


def serve(body: bytes, seen: list[Request] | None = None):
    def send(request: Request) -> Response:
        if seen is not None:
            seen.append(request)
        return Response(200, body)

    return send


def test_resolve_picks_s3_blob_and_size():
    seen: list[Request] = []
    resolved = resolve(*ARGS, serve(ASSET.read_bytes(), seen))
    assert resolved.url.startswith("https://dandiarchive.s3.amazonaws.com/blobs/")
    assert resolved.size == 14105034
    assert resolved.path == "sub-03/sub-03_ses-20150304T164400_ecephys.nwb"
    assert [r.url for r in seen] == [asset_url(*ARGS)]


def test_resolve_refuses_non_nwb():
    meta = json.loads(ASSET.read_text()) | {"encodingFormat": "text/csv"}
    with pytest.raises(PeekError, match="text/csv"):
        resolve(*ARGS, serve(json.dumps(meta).encode()))


def test_resolve_needs_an_s3_entry():
    meta = json.loads(ASSET.read_text()) | {"contentUrl": ["https://example.org/x"]}
    with pytest.raises(PeekError, match=r"dandiarchive\.s3\.amazonaws\.com"):
        resolve(*ARGS, serve(json.dumps(meta).encode()))
