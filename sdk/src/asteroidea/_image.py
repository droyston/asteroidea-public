"""Open a catalogued asset as a lazy remote file or a NIfTI image."""

import gzip
from typing import TYPE_CHECKING, Any, cast

import boto3

from asteroidea import _creds, _query
from asteroidea._blocks import BlockFile
from asteroidea._remote import ObjectClient, RemoteFile, Source
from asteroidea._sample import optional

if TYPE_CHECKING:
    from asteroidea._auth import Auth


def object_client(auth: Auth, *, renew: bool = False) -> ObjectClient:
    """Return an object-store client holding the researcher's credentials."""
    held = _creds.credentials(auth, force=renew)
    return cast(
        "ObjectClient",
        boto3.client(
            "s3",
            region_name=auth.config.region,
            aws_access_key_id=held.access_key_id,
            aws_secret_access_key=held.secret_key,
            aws_session_token=held.session_token,
        ),
    )


def open_asset(auth: Auth, asset_id: str) -> RemoteFile:
    """Return a read-only seekable file pinned to the asset's catalogued version."""
    ref = _query.find_asset(
        _query.open_catalog(auth).load_table(_query.TABLE), asset_id
    )
    source = Source(
        auth.config.raw_bucket, ref.s3_key, ref.s3_version_id, ref.size_bytes
    )
    return RemoteFile(
        source,
        client=lambda: object_client(auth),
        renewed=lambda: object_client(auth, renew=True),
    )


def image_over(remote: RemoteFile) -> object:
    """Return a NIfTI image over `remote`.

    A `.nii.gz` is read through a block cache and a gzip stream (gzip cannot seek,
    so a volume costs the compressed bytes up to its end); a `.nii` keeps one
    request per read.
    """
    nibabel: Any = optional("nibabel")
    if remote.source.key.endswith(".gz"):
        blocks = BlockFile(remote)
        holder = nibabel.FileHolder(fileobj=gzip.GzipFile(fileobj=blocks))
    else:
        holder = nibabel.FileHolder(fileobj=remote)
    image = nibabel.Nifti1Image.from_file_map({"header": holder, "image": holder})
    image.extra["remote_file"] = remote
    return image


def open_image(auth: Auth, asset_id: str) -> object:
    """Return a NIfTI image (.nii or .nii.gz) over the lazy file.

    `img.extra["remote_file"]` is the file; it counts the bytes fetched.
    """
    return image_over(open_asset(auth, asset_id))
