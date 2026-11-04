"""Deterministic asset ids: uuid5 over the raw key and its version id."""

import uuid
from typing import Final

NAMESPACE: Final = uuid.UUID("6f3c1a52-8d0e-5b7a-9c41-2e5d7a0b13f4")


def asset_id(raw_key: str, raw_version_id: str) -> str:
    """Return `uuid5(NS, raw_key@raw_version_id)` as a string."""
    return str(uuid.uuid5(NAMESPACE, f"{raw_key}@{raw_version_id}"))
