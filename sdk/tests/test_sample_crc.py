import base64
import re
from typing import TYPE_CHECKING, Any

from asteroidea import _crc, _sample

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_crc64nvme_check_value(tmp_path: Path) -> None:
    path = tmp_path / "check"
    path.write_bytes(b"123456789")
    assert base64.b64decode(_crc.crc64nvme(path)).hex() == "ae8b14860a799888"


def test_crc64nvme_is_independent_of_the_chunk_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data"
    path.write_bytes(bytes(range(256)) * 100)
    whole = _crc.crc64nvme(path)
    monkeypatch.setattr(_crc, "CHUNK", 7)
    assert _crc.crc64nvme(path) == whole


def test_dataset_names_are_unique_and_match_the_api_pattern() -> None:
    names = {_sample.dataset_name() for _ in range(20)}
    assert len(names) == 20
    assert all(re.fullmatch(r"sample-\d{14}-[0-9a-f]{4}", n) for n in names)
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", n) for n in names)


def test_sample_is_a_3_mib_t1w_nifti_in_a_bids_folder() -> None:
    nibabel: Any = _sample.optional("nibabel")
    dataset, path = _sample.write_sample()
    assert path.as_posix().endswith("sub-01/anat/sub-01_T1w.nii")
    assert dataset.startswith("sample-")
    assert 3 * 1024 * 1024 <= path.stat().st_size < 3 * 1024 * 1024 + 1024
    image = nibabel.load(path)
    assert image.shape == (128, 128, 96)
    assert str(image.get_data_dtype()) == "int16"
    numpy = _sample.optional("numpy")
    assert numpy.array_equal(image.get_fdata().astype("int16"), _sample.sample_array())
