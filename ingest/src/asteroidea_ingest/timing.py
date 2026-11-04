"""Function timeouts, queue visibilities and leases, derived in one place.

The rule for every lease: the function timeout is shorter than the lease (a live
delivery never loses its claim) and the lease is shorter than the queue visibility
(SQS redelivery is never the only retry: an expired lease lets `make reclaim`, or the
redelivery itself, resume before the visibility timeout ends). Visibility is 6 times
the consumer's timeout (AWS guidance for SQS event sources). Plain data: the CDK app,
the Lambdas and `make reclaim` all read these constants.
"""

from typing import Final

VISIBILITY_FACTOR: Final = 6
LEASE_MARGIN_SECONDS: Final = 30

# The API functions. The HTTP API integration limit is 30 s (ADR-0016).
API_TIMEOUT_SECONDS: Final = 10
# CatalogFunction and LabelsFunction: under the 30 s limit, over the handlers' 18 s
# Athena poll budget (`athena_query.BUDGET_SECONDS`).
CATALOG_TIMEOUT_SECONDS: Final = 20
# Under the 30 s limit; compute.py's deadline is the remaining time minus 2 s.
COMPUTE_TIMEOUT_SECONDS: Final = 28
# ImportFunction: Lambda's 900 s maximum; one file's copy into landing/.
IMPORT_TIMEOUT_SECONDS: Final = 900

COMMIT_TIMEOUT_SECONDS: Final = 120
EXTRACT_TIMEOUT_SECONDS: Final = 120
COMMIT_VISIBILITY_SECONDS: Final = VISIBILITY_FACTOR * COMMIT_TIMEOUT_SECONDS
EXTRACT_VISIBILITY_SECONDS: Final = VISIBILITY_FACTOR * EXTRACT_TIMEOUT_SECONDS

# The commit claim (finalize, import and every commit claim) and the extract claim.
COMMIT_LEASE_SECONDS: Final = COMMIT_TIMEOUT_SECONDS + LEASE_MARGIN_SECONDS
EXTRACT_LEASE_SECONDS: Final = EXTRACT_TIMEOUT_SECONDS + LEASE_MARGIN_SECONDS
# Written at COMMITTED with `extract_state = PENDING`: the queue backlog allowance.
EXTRACT_BACKLOG_SECONDS: Final = 600
# `make reextract` tracking item: the queue wait plus one extraction.
REEXTRACT_LEASE_SECONDS: Final = EXTRACT_BACKLOG_SECONDS

# DeriveFunction: the re-extract read/derive task a Step Functions Map
# invokes per asset. It runs the same extractor work the extract handler runs within
# EXTRACT_TIMEOUT_SECONDS (retries included), plus one gzip bundle and two PUTs (a
# bundle for a 96-electrode, 44-unit session is 678 KB), so 300 s is 2.5 times that
# budget and a third of Lambda's 900 s. No queue and no lease of its own: one asset's
# tracking lease must cover its derive and its serial commit
# (REEXTRACT_DERIVE_AND_COMMIT).
DERIVE_TIMEOUT_SECONDS: Final = 300
REEXTRACT_DERIVE_AND_COMMIT_SECONDS: Final = (
    DERIVE_TIMEOUT_SECONDS + EXTRACT_TIMEOUT_SECONDS + LEASE_MARGIN_SECONDS
)

# LFP index and overview builder: its own standard queue, Lambda's 900 s
# maximum. It writes derived objects only and claims no item: its lease is the claim
# budget one delivery holds before SQS redelivers, so the row pins the lease rule for
# its queue.
LFP_BUILD_TIMEOUT_SECONDS: Final = 900
LFP_BUILD_VISIBILITY_SECONDS: Final = VISIBILITY_FACTOR * LFP_BUILD_TIMEOUT_SECONDS
LFP_BUILD_LEASE_SECONDS: Final = LFP_BUILD_TIMEOUT_SECONDS + LEASE_MARGIN_SECONDS


# ImportPlanFunction: invoked asynchronously, no queue
# and no lease, so the lease rule has no row for it. One plan makes a handful of
# sequential archive calls (each bounded by `import_plan.HTTP_TIMEOUT_SECONDS`, 60 s),
# one catalog scan and at most 50 asynchronous invokes; the acceptance row budgets
# 60 s. Five HTTP timeouts, so one slow archive answer cannot end the plan.
IMPORT_PLAN_TIMEOUT_SECONDS: Final = 300

# The lease of a web import run, read by `imports_api`. A run has no writer
# after ImportPlanFunction, so a run still PLANNING or RUNNING this long after its last
# write reads as FAILED. PLANNING: Lambda's asynchronous invoke makes 3 attempts
# (1 + 2 retries) of at most `IMPORT_PLAN_TIMEOUT_SECONDS` each, waiting 60 s and then
# 120 s between them, plus the shared margin. RUNNING: the same lease, plus one commit
# lease for each file (the commit FIFO may hold the files one at a time).
IMPORT_PLAN_ATTEMPTS: Final = 3
IMPORT_PLAN_RETRY_WAIT_SECONDS: Final = 60 + 120
IMPORT_RUN_LEASE_SECONDS: Final = (
    IMPORT_PLAN_ATTEMPTS * IMPORT_PLAN_TIMEOUT_SECONDS
    + IMPORT_PLAN_RETRY_WAIT_SECONDS
    + LEASE_MARGIN_SECONDS
)
IMPORT_RUN_FILE_ALLOWANCE_SECONDS: Final = COMMIT_LEASE_SECONDS


def lease_is_valid(lease: int, timeout: int, visibility: int) -> bool:
    """Return whether ``timeout < lease < visibility``."""
    return timeout < lease < visibility


# (name, lease, consumer timeout, queue visibility) for every lease the platform writes.
LEASES: Final = (
    (
        "commit claim",
        COMMIT_LEASE_SECONDS,
        COMMIT_TIMEOUT_SECONDS,
        COMMIT_VISIBILITY_SECONDS,
    ),
    (
        "extract claim",
        EXTRACT_LEASE_SECONDS,
        EXTRACT_TIMEOUT_SECONDS,
        EXTRACT_VISIBILITY_SECONDS,
    ),
    (
        "extract at COMMITTED",
        EXTRACT_BACKLOG_SECONDS,
        EXTRACT_TIMEOUT_SECONDS,
        EXTRACT_VISIBILITY_SECONDS,
    ),
    (
        "reextract tracking item",
        REEXTRACT_LEASE_SECONDS,
        EXTRACT_TIMEOUT_SECONDS,
        EXTRACT_VISIBILITY_SECONDS,
    ),
    (
        "lfp build delivery",
        LFP_BUILD_LEASE_SECONDS,
        LFP_BUILD_TIMEOUT_SECONDS,
        LFP_BUILD_VISIBILITY_SECONDS,
    ),
)
