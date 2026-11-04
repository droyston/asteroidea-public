"""Import run states and a file's outcome, with no heavy imports.

ImportsFunction is a zip Lambda: it carries `ingest/src` plus the runtime's boto3, not
pyiceberg or `asteroidea_infra`. `import_plan` imports both, so what `imports_api`
needs lives here (importing the heavy modules at load made the function fail with a
500; `infra/tests/test_api_bundle.py` pins it).
"""

from typing import TYPE_CHECKING, Any, Final

from asteroidea_ingest import sessions

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["DONE", "PLANNING", "RUNNING", "outcome"]

PLANNING: Final = "PLANNING"
RUNNING: Final = "RUNNING"
DONE: Final = "DONE"


def outcome(item: Mapping[str, Any] | None) -> str | None:
    """Return "PASS", "FAIL <why>", or None while the file is still moving."""
    if item is None:
        return None
    state, extract = item.get("state"), item.get("extract_state")
    if state == sessions.INCOMPLETE:
        why = f"{item.get('error')} {item.get('commit_error', '')}".rstrip()
        return f"FAIL INCOMPLETE {why}"
    if extract == "FAILED":
        return f"FAIL extraction {item.get('extract_error')}"
    if state == sessions.COMMITTED and extract in {"EXTRACTED", "SKIPPED"}:
        return "PASS"
    return None
