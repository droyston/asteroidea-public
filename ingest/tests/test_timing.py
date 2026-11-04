"""The lease rule, asserted from the constants."""

from asteroidea_ingest import athena_query, sessions, timing


def test_every_lease_is_above_its_function_timeout_and_below_its_visibility() -> None:
    # commit claim, extract claim, extract at COMMITTED, reextract tracking item,
    # lfp build delivery (its own queue and lease row)
    assert len(timing.LEASES) == 5
    for name, lease, timeout, visibility in timing.LEASES:
        assert timeout < lease, f"{name}: lease {lease} <= timeout {timeout}"
        assert lease < visibility, f"{name}: lease {lease} >= visibility {visibility}"


def test_visibility_is_six_times_the_timeout() -> None:
    assert timing.COMMIT_VISIBILITY_SECONDS == 6 * timing.COMMIT_TIMEOUT_SECONDS
    assert timing.EXTRACT_VISIBILITY_SECONDS == 6 * timing.EXTRACT_TIMEOUT_SECONDS
    assert timing.LFP_BUILD_VISIBILITY_SECONDS == 6 * timing.LFP_BUILD_TIMEOUT_SECONDS


def test_the_lfp_build_fits_lambda_and_never_borrows_the_extract_limits() -> None:
    """A timeout up to Lambda's 900 s, its own row, not the extract FIFO's."""
    assert timing.LFP_BUILD_TIMEOUT_SECONDS == 900
    assert timing.LFP_BUILD_LEASE_SECONDS == 930
    assert timing.LFP_BUILD_VISIBILITY_SECONDS == 5400
    (row,) = [r for r in timing.LEASES if r[0] == "lfp build delivery"]
    assert row[2:] != (
        timing.EXTRACT_TIMEOUT_SECONDS,
        timing.EXTRACT_VISIBILITY_SECONDS,
    )


def test_the_session_store_writes_exactly_these_leases() -> None:
    assert sessions.COMMIT_LEASE_SECONDS == timing.COMMIT_LEASE_SECONDS == 150
    assert sessions.EXTRACT_LEASE_SECONDS == timing.EXTRACT_LEASE_SECONDS == 150
    assert sessions.EXTRACT_BACKLOG_SECONDS == timing.EXTRACT_BACKLOG_SECONDS == 600
    assert sessions.REEXTRACT_LEASE_SECONDS == timing.REEXTRACT_LEASE_SECONDS == 600


def test_the_rule_rejects_a_lease_at_or_above_the_visibility() -> None:
    assert timing.lease_is_valid(150, 120, 720)
    assert not timing.lease_is_valid(720, 120, 720)
    assert not timing.lease_is_valid(
        3600, 120, 720
    )  # the backlog lease of the earlier timing
    assert not timing.lease_is_valid(120, 120, 720)


def test_the_reextract_lease_covers_one_derive_and_its_commit() -> None:
    """DeriveFunction fits Lambda; one asset's lease covers both."""
    assert timing.DERIVE_TIMEOUT_SECONDS == 300
    assert timing.DERIVE_TIMEOUT_SECONDS <= 900  # Lambda's maximum
    assert timing.REEXTRACT_DERIVE_AND_COMMIT_SECONDS == 300 + 120 + 30
    assert timing.REEXTRACT_DERIVE_AND_COMMIT_SECONDS <= timing.REEXTRACT_LEASE_SECONDS


def test_the_athena_poll_budget_fits_the_catalog_and_labels_timeout() -> None:
    """The handlers stop polling Athena before Lambda ends the request."""
    assert athena_query.BUDGET_SECONDS < timing.CATALOG_TIMEOUT_SECONDS
    assert timing.CATALOG_TIMEOUT_SECONDS < 30  # the HTTP API integration limit
    assert timing.API_TIMEOUT_SECONDS < 30
    assert timing.COMPUTE_TIMEOUT_SECONDS < 30
    assert timing.IMPORT_TIMEOUT_SECONDS <= 900  # Lambda's maximum
