"""Dataset ownership claims."""

from typing import Any

from asteroidea_ingest import owners

from .conftest import BUCKET, FakeOwners, owned_by

NOW = 500


class FakeS3:
    """Answers `list_objects_v2`; records the call."""

    def __init__(self, *keys: str) -> None:
        self.keys = keys
        self.calls: list[dict[str, Any]] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        listed = [{"Key": k} for k in self.keys if k.startswith(kwargs["Prefix"])]
        return {"KeyCount": len(listed), "Contents": listed[: kwargs["MaxKeys"]]}


def _claim(store: FakeOwners, s3: FakeS3, sub: str = "user-1") -> bool:
    return owners.claim_for_researcher(
        owners.Owners(store, "owners"),
        owners.RawProbe(s3, BUCKET),
        "ds1",
        sub,
        NOW,
    )


def test_same_owner_continues_without_listing_or_writing() -> None:
    store, s3 = FakeOwners(owned_by("user-1")), FakeS3()
    assert _claim(store, s3) is True
    assert s3.calls == []
    assert store.puts == []


def test_a_different_owner_is_refused_without_listing_or_writing() -> None:
    store, s3 = FakeOwners(owned_by("user-2")), FakeS3()
    assert _claim(store, s3) is False
    assert s3.calls == []
    assert store.puts == []


def test_no_row_and_an_existing_raw_object_is_admin_owned_and_refused() -> None:
    store, s3 = FakeOwners(), FakeS3("raw/ds1/old/file.nwb")
    assert _claim(store, s3) is False
    assert s3.calls == [{"Bucket": BUCKET, "Prefix": "raw/ds1/", "MaxKeys": 1}]
    (put,) = store.puts
    assert put["condition"] == "attribute_not_exists(dataset)"
    assert store.rows["ds1"] == {
        "dataset": "ds1",
        "owner_sub": "admin",
        "owner_kind": "admin",
        "claimed_at": NOW,
    }
    assert _claim(store, s3) is False  # the next call reads the admin row


def test_the_prefix_probe_does_not_match_a_longer_dataset_name() -> None:
    store, s3 = FakeOwners(), FakeS3("raw/ds10/file.nwb")
    assert _claim(store, s3) is True  # `raw/ds1/` does not list `raw/ds10/`


def test_nothing_listed_claims_the_dataset_for_the_caller() -> None:
    store, s3 = FakeOwners(), FakeS3()
    assert _claim(store, s3) is True
    (put,) = store.puts
    assert put["condition"] == "attribute_not_exists(dataset) OR owner_sub = :sub"
    assert store.rows["ds1"] == {
        "dataset": "ds1",
        "owner_sub": "user-1",
        "owner_kind": "researcher",
        "claimed_at": NOW,
    }


def test_losing_the_claim_race_re_reads_and_refuses() -> None:
    store, s3 = FakeOwners(), FakeS3()
    store.before_put = lambda _dataset: store.rows.update(owned_by("user-2"))
    assert _claim(store, s3) is False
    assert store.rows["ds1"]["owner_sub"] == "user-2"  # the winner is untouched


def test_a_racing_claim_by_the_same_caller_continues() -> None:
    store, s3 = FakeOwners(), FakeS3()
    store.before_put = lambda _dataset: store.rows.update(owned_by("user-1"))
    assert _claim(store, s3) is True


def test_admin_claim_on_an_unowned_dataset_writes_an_admin_row() -> None:
    store = FakeOwners()
    assert owners.claim_for_admin(owners.Owners(store, "owners"), "ds1", "import:c", 7)
    (put,) = store.puts
    assert put["condition"] == "attribute_not_exists(dataset) OR owner_kind = :admin"
    assert store.rows["ds1"]["owner_kind"] == "admin"
    assert store.rows["ds1"]["owner_sub"] == "import:c"


def test_admin_claim_continues_on_an_admin_row_and_rewrites_nothing() -> None:
    store = FakeOwners()
    table = owners.Owners(store, "owners")
    assert owners.claim_for_admin(table, "ds1", "import:c", 7)
    store.puts.clear()
    assert owners.claim_for_admin(table, "ds1", "import:d", 8)
    assert store.puts == []
    assert store.rows["ds1"]["claimed_at"] == 7


def test_admin_claim_on_a_researcher_dataset_is_refused() -> None:
    store = FakeOwners(owned_by("user-1"))
    assert not owners.claim_for_admin(owners.Owners(store, "owners"), "ds1", "i:c", 7)
    assert store.puts == []


def test_admin_claim_losing_a_race_to_a_researcher_is_refused() -> None:
    store = FakeOwners()
    store.before_put = lambda _dataset: store.rows.update(owned_by("user-1"))
    assert not owners.claim_for_admin(owners.Owners(store, "owners"), "ds1", "i:c", 7)
