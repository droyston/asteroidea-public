from io import StringIO
from pathlib import Path

import pytest

from asteroidea_services.http import Response
from asteroidea_services.peek import main

ASSET = Path(__file__).parent / "fixtures" / "peek" / "asset-20257c14.json"
ARGV = ["--dandiset", "000576", "--version", "0.231010.1811", "--asset", "a"]


def meta_for(size: int) -> bytes:
    return ASSET.read_bytes().replace(b"14105034", str(size).encode())


def test_cli_prints_report_with_counted_bytes(nwb_path, range_stub):
    size = nwb_path.stat().st_size
    out = StringIO()
    code = main(
        ARGV,
        http=lambda _r: Response(200, meta_for(size)),
        inner=range_stub,
        stdout=out,
    )
    lines = out.getvalue().splitlines()
    assert code == 0
    assert "units: present (7 rows) at /units" in lines
    assert "lfp: /processing/ecog/LFP (1 electrical series)" in lines
    assert any(line.startswith("electricalseries: /acquisition/raw ") for line in lines)
    last = lines[-1]
    assert last.startswith("read ")
    assert f" of {size} bytes in " in last
    assert last.endswith("(HTTP-counted)")


def test_cli_fails_closed_on_unranged_200(nwb_path, range_stub, monkeypatch):
    # remfile retries a failed request with sleeps (about 25 s); skip the sleeps.
    monkeypatch.setattr("remfile.RemFile.time.sleep", lambda _seconds: None)
    size = nwb_path.stat().st_size
    range_stub.ranged = False
    err = StringIO()
    with pytest.raises(SystemExit) as raised:
        main(
            ARGV,
            http=lambda _r: Response(200, meta_for(size)),
            inner=range_stub,
            stdout=StringIO(),
            stderr=err,
        )
    assert raised.value.code == 1
    assert "unranged response 200" in err.getvalue()


def test_cli_reports_hdf5_error_with_class_and_text(range_stub):
    range_stub.data = b"this is not an hdf5 file " * 40
    err = StringIO()
    with pytest.raises(SystemExit) as raised:
        main(
            ARGV,
            http=lambda _r: Response(200, meta_for(len(range_stub.data))),
            inner=range_stub,
            stdout=StringIO(),
            stderr=err,
        )
    assert raised.value.code == 1
    assert err.getvalue().startswith("peek: OSError: ")
