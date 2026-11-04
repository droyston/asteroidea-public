"""The synthetic sample: one 3 MiB T1w NIfTI-1 file in a BIDS folder."""

import importlib
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from asteroidea.errors import ExtraMissingError

if TYPE_CHECKING:
    from types import ModuleType

SHAPE: Final = (128, 128, 96)
RELPATH: Final = "sub-01/anat/sub-01_T1w.nii"
SEED: Final = 20261023
_MAX_VOXEL: Final = 4000


def optional(name: str, extra: str = "nifti") -> ModuleType:
    """Import an optional package, or raise `ExtraMissingError` naming `extra`."""
    try:
        return importlib.import_module(name)
    except ImportError as err:
        msg = f"A package is missing; install asteroidea[{extra}] and try again."
        raise ExtraMissingError(msg) from err


def sample_array() -> object:
    """Return the sample's voxels: a seeded int16 array of shape `SHAPE`."""
    numpy = optional("numpy")
    rng = numpy.random.default_rng(SEED)
    return rng.integers(0, _MAX_VOXEL, size=SHAPE, dtype=numpy.int16)


def dataset_name(prefix: str = "sample") -> str:
    """Return `<prefix>-<UTC yyyymmddhhmmss>-<4 hex>`, unique per call."""
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(2)}"


def write_sample(prefix: str = "sample") -> tuple[str, Path]:
    """Write the sample under a new temporary folder; return (dataset, file path).

    Needs the `nifti` extra. Pass the pair to `upload(path, dataset=dataset)`.
    """
    numpy = optional("numpy")
    nibabel = optional("nibabel")
    path = Path(tempfile.mkdtemp(prefix="asteroidea-sample-")) / RELPATH
    path.parent.mkdir(parents=True)
    image = nibabel.Nifti1Image(sample_array(), numpy.eye(4))
    nibabel.save(image, path)
    return dataset_name(prefix), path
