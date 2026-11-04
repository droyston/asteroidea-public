"""Walk an open NWB file by ``neurodata_type``: Units, LFP, ElectricalSeries."""

from dataclasses import dataclass, field

import h5py


@dataclass(frozen=True)
class UnitsInfo:
    """A Units table."""

    path: str
    rows: int


@dataclass(frozen=True)
class SeriesInfo:
    """An ElectricalSeries."""

    path: str
    shape: tuple[int, ...]
    dtype: str
    timing: str
    lfp: str | None


@dataclass(frozen=True)
class LfpInfo:
    """An LFP container and the paths of its ElectricalSeries children."""

    path: str
    series: tuple[str, ...]


@dataclass(frozen=True)
class Inspected:
    """What the walk found."""

    units: list[UnitsInfo] = field(default_factory=list)
    lfp: list[LfpInfo] = field(default_factory=list)
    series: list[SeriesInfo] = field(default_factory=list)


def _type_of(obj: h5py.Group | h5py.Dataset) -> str | None:
    """Return the `neurodata_type` attribute of an HDF5 object, or `None`."""
    value = obj.attrs.get("neurodata_type")
    if isinstance(value, bytes):
        return value.decode()
    return value if isinstance(value, str) else None


def _timing(group: h5py.Group) -> str:
    """Return the timing of a series: its rate, its timestamp count or unknown."""
    start = group.get("starting_time")
    if isinstance(start, h5py.Dataset) and "rate" in start.attrs:
        return f"rate={float(start.attrs['rate'])}"
    stamps = group.get("timestamps")
    if isinstance(stamps, h5py.Dataset):
        return f"timestamps={stamps.shape[0] if stamps.shape else 0}"
    return "timing=unknown"


def _series(path: str, group: h5py.Group, lfp: str | None) -> SeriesInfo | None:
    """Return the shape, type and timing of a series, or `None` without data."""
    data = group.get("data")
    if not isinstance(data, h5py.Dataset):
        return None
    return SeriesInfo(path, tuple(data.shape), str(data.dtype), _timing(group), lfp)


def inspect_file(nwb: h5py.File) -> Inspected:
    """Find every Units, LFP and ElectricalSeries object, wherever it sits."""
    found: dict[str, tuple[str, h5py.Group]] = {}

    def visit(name: str, obj: h5py.Group | h5py.Dataset) -> None:
        """Record a group when it is a Units, LFP or ElectricalSeries group."""
        kind = _type_of(obj)
        if kind in {"Units", "LFP", "ElectricalSeries"} and isinstance(obj, h5py.Group):
            found[f"/{name}"] = (kind, obj)

    nwb.visititems(visit)
    result = Inspected()
    lfp_paths = {p for p, (kind, _) in found.items() if kind == "LFP"}
    for path, (kind, group) in sorted(found.items()):
        if kind == "Units":
            ident = group.get("id")
            rows = int(ident.shape[0]) if isinstance(ident, h5py.Dataset) else 0
            result.units.append(UnitsInfo(path, rows))
        elif kind == "LFP":
            children = tuple(
                p
                for p, (k, _) in sorted(found.items())
                if k == "ElectricalSeries" and p.rsplit("/", 1)[0] == path
            )
            result.lfp.append(LfpInfo(path, children))
        else:
            parent = path.rsplit("/", 1)[0]
            info = _series(path, group, parent if parent in lfp_paths else None)
            if info is not None:
                result.series.append(info)
    return result
