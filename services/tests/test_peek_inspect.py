import h5py

from asteroidea_services.peek.inspect import inspect_file


def test_inspect_finds_units_rows(nwb_path):
    with h5py.File(nwb_path, "r") as nwb:
        found = inspect_file(nwb)
    assert [(u.path, u.rows) for u in found.units] == [("/units", 7)]


def test_inspect_finds_lfp_at_nonstandard_path(nwb_path):
    with h5py.File(nwb_path, "r") as nwb:
        found = inspect_file(nwb)
    assert [i.path for i in found.lfp] == ["/processing/ecog/LFP"]
    assert found.lfp[0].series == ("/processing/ecog/LFP/filtered",)


def test_inspect_series_shapes_and_membership(nwb_path):
    with h5py.File(nwb_path, "r") as nwb:
        found = inspect_file(nwb)
    by_path = {s.path: s for s in found.series}
    assert by_path["/processing/ecog/LFP/filtered"].shape == (500, 4)
    assert by_path["/processing/ecog/LFP/filtered"].lfp == "/processing/ecog/LFP"
    assert by_path["/processing/ecog/LFP/filtered"].timing == "rate=1000.0"
    assert by_path["/acquisition/raw"].shape == (900, 8)
    assert by_path["/acquisition/raw"].lfp is None
    assert by_path["/acquisition/raw"].dtype == "float32"


def test_inspect_empty_file(tmp_path):
    path = tmp_path / "empty.nwb"
    with h5py.File(path, "w") as nwb:
        nwb.create_group("general")
    with h5py.File(path, "r") as nwb:
        found = inspect_file(nwb)
    assert (found.units, found.lfp, found.series) == ([], [], [])
