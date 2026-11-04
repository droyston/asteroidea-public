"""Deployment constants. No `aws_cdk` import, so this module stays cheap to import."""

import os
from typing import Final

REGION: Final = "us-east-1"
DEFAULT_ACCOUNT: Final = "000000000000"
# Role names are fixed so the raw bucket policy (DataStack) can name its two writers
# without a reference into ServicesStack, which would make the two stacks cyclic.
COMMIT_ROLE_NAME: Final = "asteroidea-commit-role"
SESSION_API_ROLE_NAME: Final = "asteroidea-session-api-role"
PLATFORM_ROLE_NAME: Final = "asteroidea-platform-role"  # GET /get-started
CATALOG_ROLE_NAME: Final = "asteroidea-catalog-role"  # GET /catalog/*
COMPUTE_ROLE_NAME: Final = "asteroidea-compute-role"  # GET /compute/*
IMPORT_ROLE_NAME: Final = "asteroidea-import-role"  # landing only
IMPORT_PLAN_ROLE_NAME: Final = "asteroidea-import-plan-role"  # web imports
EXTRACT_ROLE_NAME: Final = "asteroidea-extract-role"  # not a raw writer
IMPORTS_ROLE_NAME: Final = "asteroidea-imports-role"  # /imports
LABELS_ROLE_NAME: Final = "asteroidea-labels-role"  # /files/*/labels
LFP_BUILD_ROLE_NAME: Final = (
    "asteroidea-lfp-build-role"  # the derived store's one writer
)
DERIVE_ROLE_NAME: Final = "asteroidea-derive-role"  # the derived store's rows/ writer
TAGS: Final = {"project": "asteroidea", "managed-by": "cdk"}


def account() -> str:
    """Return the pinned account id; `ASTEROIDEA_ACCOUNT` overrides it."""
    return os.environ.get("ASTEROIDEA_ACCOUNT", DEFAULT_ACCOUNT)
