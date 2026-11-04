from datetime import UTC, datetime
from io import BytesIO, StringIO
from pathlib import Path
from typing import TYPE_CHECKING, override

import h5py
import numpy as np
import pytest
from requests import PreparedRequest, Response
from requests.adapters import BaseAdapter

from asteroidea_services.harvest.model import Clock
from asteroidea_services.harvest.progress import Progress

if TYPE_CHECKING:
    from requests import _types as _t

FIXTURES = Path(__file__).parent / "fixtures" / "harvest"
FROZEN = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def clock() -> Clock:
    return Clock(lambda: FROZEN, lambda: 0.0)


@pytest.fixture
def stderr() -> StringIO:
    return StringIO()


@pytest.fixture
def progress(stderr: StringIO) -> Progress:
    return Progress(stderr)


def _nwb_group(parent, name, kind):
    group = parent.create_group(name)
    group.attrs["neurodata_type"] = kind
    return group


def _series(parent: h5py.Group, name: str, shape: tuple[int, ...]) -> None:
    series = _nwb_group(parent, name, "ElectricalSeries")
    series.create_dataset("data", data=np.zeros(shape, dtype="float32"))
    start = series.create_dataset("starting_time", data=0.0)
    start.attrs["rate"] = 1000.0


@pytest.fixture
def nwb_path(tmp_path: Path) -> Path:
    """Write a small NWB-style file: Units, LFP at a custom path, a stray series."""
    path = tmp_path / "small.nwb"
    with h5py.File(path, "w") as nwb:
        units = _nwb_group(nwb, "units", "Units")
        units.create_dataset("id", data=np.arange(7))
        ecog = _nwb_group(nwb.create_group("processing"), "ecog", "ProcessingModule")
        lfp = _nwb_group(ecog, "LFP", "LFP")
        _series(lfp, "filtered", (500, 4))
        _series(nwb.create_group("acquisition"), "raw", (900, 8))
    return path


class RangeStub(BaseAdapter):
    """Serve ranged reads of ``data``; ``ranged`` False answers 200 with all of it."""

    def __init__(self, data: bytes, *, ranged: bool = True) -> None:
        super().__init__()
        self.data = data
        self.ranged = ranged

    @override
    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: _t.TimeoutType = None,
        verify: _t.VerifyType = True,
        cert: _t.CertType = None,
        proxies: dict[str, str] | None = None,
    ) -> Response:
        response = Response()
        response.url = str(request.url)
        spec = request.headers.get("Range")
        if spec is None or not self.ranged:
            response.status_code = 200
            body = self.data
        else:
            first, last = str(spec).removeprefix("bytes=").split("-")
            response.status_code = 206
            body = self.data[int(first) : int(last) + 1]
        response.raw = BytesIO(body)
        response.headers["Content-Length"] = str(len(body))
        return response

    @override
    def close(self) -> None:
        pass


@pytest.fixture
def range_stub(nwb_path: Path) -> RangeStub:
    return RangeStub(nwb_path.read_bytes())
