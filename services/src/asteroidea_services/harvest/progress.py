"""Progress lines on stderr: one per 50 records per source, then a summary."""

import sys
import threading
from typing import TextIO

EVERY = 50


class Progress:
    """Thread-safe writer of progress lines (``sys.stderr.write``, never ``print``)."""

    def __init__(self, stream: TextIO | None = None, every: int = EVERY) -> None:
        """Write to ``stream`` (default: ``sys.stderr`` at call time)."""
        self._stream = stream
        self._every = every
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def line(self, message: str) -> None:
        """Write one line."""
        with self._lock:
            (self._stream or sys.stderr).write(f"{message}\n")

    def tick(self, source: str, total: int | None = None) -> None:
        """Count one record for ``source``; write a line on every 50th."""
        with self._lock:
            count = self._counts.get(source, 0) + 1
            self._counts[source] = count
            if count % self._every == 0:
                of = f" of {total}" if total is not None else ""
                (self._stream or sys.stderr).write(f"{source}: {count}{of} records\n")
