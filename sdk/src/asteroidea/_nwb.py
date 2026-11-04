"""Open a catalogued NWB asset lazily with pynwb over a block-cached remote file."""

from typing import TYPE_CHECKING, Any

from asteroidea import _image
from asteroidea._blocks import BlockFile
from asteroidea._sample import optional
from asteroidea.errors import NotNwbError

if TYPE_CHECKING:
    from asteroidea._auth import Auth
    from asteroidea._remote import RemoteFile


def read_nwb(remote: RemoteFile) -> object:
    """Return the NWBFile of `remote`; raise `NotNwbError` when it is not HDF5/NWB."""
    h5py: Any = optional("h5py", "nwb")
    pynwb: Any = optional("pynwb", "nwb")
    blocks = BlockFile(remote)
    try:
        handle = h5py.File(blocks, "r")
        io = pynwb.NWBHDF5IO(file=handle, load_namespaces=True)
        nwbfile = io.read()
    except (OSError, ValueError, KeyError) as err:
        raise NotNwbError from err
    # keep the reader and the counters reachable from the returned file
    nwbfile.remote_file = remote
    nwbfile.block_file = blocks
    nwbfile.reader = io
    return nwbfile


def open_nwb(auth: Auth, asset_id: str) -> object:
    """Return a pynwb NWBFile whose datasets are read lazily from the asset."""
    return read_nwb(_image.open_asset(auth, asset_id))
