"""Open a raw object as an NWB file over HTTPS range reads."""

from typing import TYPE_CHECKING

import h5py
import remfile
from pynwb import NWBHDF5IO

if TYPE_CHECKING:
    from requests.adapters import BaseAdapter


def open_remote(url: str, adapter: BaseAdapter | None = None) -> NWBHDF5IO:
    """Return a read-only `NWBHDF5IO` over ``url`` (a presigned GET URL).

    ``adapter``, when given, is mounted on remfile's session for ``https://``: the
    byte-counting run uses it to count bytes. The caller closes the returned IO.
    """
    remote = remfile.File(url)
    if adapter is not None:
        session = remote.session
        if session is None:
            msg = "remfile opened without a requests session"
            raise RuntimeError(msg)
        session.mount("https://", adapter)
    return NWBHDF5IO(file=h5py.File(remote, "r"), load_namespaces=True)
