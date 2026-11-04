"""Guard for live (AWS-touching) scripts: refuse without the sandbox env."""

import os
import sys
from typing import Final

EXIT_NO_SANDBOX: Final = 4
REQUIRED: Final = ("ASTEROIDEA_PROFILE", "ASTEROIDEA_ACCOUNT")


def require_sandbox() -> tuple[str, str]:
    """Return (profile, account), or exit 4 naming the missing variables."""
    missing = [v for v in REQUIRED if os.environ.get(v, "") == ""]
    if missing:
        sys.stderr.write(
            f"refusing: {', '.join(missing)} not set. Put them in "
            "~/.config/asteroidea/sandbox.env and load it.\n",
        )
        raise SystemExit(EXIT_NO_SANDBOX)
    return os.environ["ASTEROIDEA_PROFILE"], os.environ["ASTEROIDEA_ACCOUNT"]
