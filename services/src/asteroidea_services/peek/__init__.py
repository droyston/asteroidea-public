"""``make peek``: read NWB Units, LFP and ElectricalSeries presence over HTTP ranges."""

import argparse
import sys
import time
from typing import TYPE_CHECKING, TextIO

import h5py
import remfile
from requests.adapters import BaseAdapter, HTTPAdapter

from asteroidea_services.http import Transport, live_transport, retrying
from asteroidea_services.peek.count import CountingAdapter, PeekError
from asteroidea_services.peek.inspect import Inspected, inspect_file
from asteroidea_services.peek.resolve import Resolved, resolve

if TYPE_CHECKING:
    from collections.abc import Sequence


def render(
    found: Inspected, resolved: Resolved, counter: CountingAdapter, seconds: float
) -> list[str]:
    """Return the report lines."""
    lines = [f"units: present ({u.rows} rows) at {u.path}" for u in found.units] or [
        "units: absent"
    ]
    lines.extend(
        f"lfp: {item.path} ({len(item.series)} electrical series)" for item in found.lfp
    )
    for s in found.series:
        where = f" in lfp {s.lfp}" if s.lfp is not None else ""
        lines.append(
            f"electricalseries: {s.path} shape={s.shape} dtype={s.dtype} "
            f"{s.timing}{where}"
        )
    lines.append(
        f"read {counter.bytes_read} of {resolved.size} bytes in {len(counter.records)} "
        f"ranged responses, {seconds:.1f} s (HTTP-counted)"
    )
    return lines


def run(resolved: Resolved, inner: BaseAdapter) -> list[str]:
    """Open the blob over counted range reads, inspect it, return the report lines."""
    counter = CountingAdapter(inner)
    started = time.perf_counter()
    remote = remfile.File(resolved.url, _size=resolved.size)
    session = remote.session
    if session is None:
        msg = "remfile opened without a requests session"
        raise PeekError(msg)
    session.mount("https://", counter)
    session.headers["Accept-Encoding"] = "identity"
    with h5py.File(remote, "r") as nwb:
        found = inspect_file(nwb)
    if counter.violations:
        raise PeekError(counter.violations[0])
    if not counter.records:
        msg = "no HTTP responses counted; remfile bypassed the session"
        raise PeekError(msg)
    return render(found, resolved, counter, time.perf_counter() - started)


def main(
    argv: Sequence[str] | None = None,
    *,
    http: Transport | None = None,
    inner: BaseAdapter | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run peek: return 0, or exit 1 with the exception class and message."""
    out, err = stdout or sys.stdout, stderr or sys.stderr
    parser = argparse.ArgumentParser(prog="peek", description=__doc__)
    parser.add_argument("--dandiset", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--asset", required=True)
    args = parser.parse_args(argv)
    try:
        resolved = resolve(
            args.dandiset, args.version, args.asset, http or retrying(live_transport())
        )
        lines = run(resolved, inner or HTTPAdapter())
    except Exception as exc:
        text = (
            str(exc) if isinstance(exc, PeekError) else f"{type(exc).__name__}: {exc}"
        )
        err.write(f"peek: {text}\n")
        raise SystemExit(1) from exc
    out.write("\n".join(lines) + "\n")
    return 0
