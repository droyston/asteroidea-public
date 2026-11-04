"""One structured timing line per API request.

A line is JSON on stdout, so it lands in CloudWatch Logs: `{"timing": "<route>",
"phases": {"<name>_ms": int, ...}, "cold": bool, ...}`, plus `asset_id`, `cache`,
`status` and `request_id` when the writer has them. The fixed `timing` key lets a
Logs Insights query select them with `filter ispresent(timing)`. A line never carries
a token, a user sub or a URL (a presigned URL is a credential). `cold` is true for the
first request an execution environment serves: the module-level flag is taken once
per process, in `begin_request`.
"""

import json
import sys
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["begin_request", "emit", "is_cold", "reset"]

ID_MAX: Final = 128

_state = {"first": True, "cold": False}


def begin_request() -> None:
    """Mark the start of one request; the first one of the process is cold."""
    _state["cold"] = _state["first"]
    _state["first"] = False


def reset() -> None:
    """Forget that any request was served (a test starts a fresh environment)."""
    _state.update(first=True, cold=False)


def is_cold() -> bool:
    """Return whether the current request is the first of this execution environment."""
    return _state["cold"]


def emit(
    route: str,
    phases: Mapping[str, int],
    asset_id: str | None = None,
    *,
    cache: str | None = None,
    answer: Mapping[str, int | str] | None = None,
) -> None:
    """Write one timing line for `route` with whole-millisecond `phases`.

    `cache` ("hit" or "miss") is added as a top-level key when the route has a cache.
    `answer` holds the `status` and `request_id` of the answered request
    (`api_common.serve` passes it); its keys are added top level.
    """
    line: dict[str, object] = {
        "timing": route,
        "phases": dict(phases),
        "cold": is_cold(),
    }
    if asset_id is not None:
        line["asset_id"] = asset_id[:ID_MAX]
    if cache is not None:
        line["cache"] = cache
    if answer is not None:
        line.update(answer)
    sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
    sys.stdout.flush()
