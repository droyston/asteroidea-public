"""Helpers the LIVE headless-browser runs share (`research_app_check`, `web_smoke`).

A new researcher is made by `analysis_check.create_researcher`; the sign-in and
the API calls are in `runner`; this module holds the pinned Chromium Playwright runs.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Final

from asteroidea_infra import first_hour
from asteroidea_infra.runner import execute
from asteroidea_infra.steps import StepFailureError

if TYPE_CHECKING:
    from collections.abc import Mapping

HTTP_OK: Final = 200
API_TIMEOUT_S: Final = 120.0
INSTALL_TIMEOUT_S: Final = 600.0


def browsers_path() -> str:
    """Return where the pinned Chromium lives (`.tools/ms-playwright`)."""
    return str(first_hour.REPO / ".tools" / "ms-playwright")


def ensure_browser(env: Mapping[str, str]) -> None:
    """Install the pinned Chromium into `.tools/ms-playwright` when it is missing."""
    if list(Path(env["PLAYWRIGHT_BROWSERS_PATH"]).glob("chromium*")):
        return
    web = first_hour.REPO / "web"
    done = execute(
        ["./node_modules/.bin/playwright", "install", "chromium"],
        cwd=web,
        env=env,
        timeout=INSTALL_TIMEOUT_S,
    )
    if done.code != 0:
        msg = f"playwright install chromium exit {done.code}"
        raise StepFailureError(msg, "Run make web-install, then retry.")
