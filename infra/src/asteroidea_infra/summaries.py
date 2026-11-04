"""`make summaries` (admin, live): queue a summary rebuild for every dataset.

`make summaries [DATASET=<d>]` sends one `{"kind": "summary", "dataset": <d>}`
message per dataset to the LFP build queue (the `LfpBuildQueueUrl` output of
ServicesStack). Without `DATASET` the datasets are the distinct `dataset` values of
the catalog `assets` table (PyIceberg Glue). LfpBuildFunction builds each dataset
summary and fans out its session summaries; this module never invokes a function
and never writes the derived store. It is the one-time backfill; import and
re-extract keep the summaries fresh after it. It prints one SENT or FAIL line per
dataset.

Exit codes: 0 every message sent; 1 a send failed or no dataset found; 2 usage;
4 no sandbox.
"""

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from asteroidea_infra import schema
from asteroidea_infra.admin import session_for, stack_outputs
from asteroidea_infra.catalog_init import glue_catalog
from asteroidea_infra.sandbox_guard import require_sandbox

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyiceberg.catalog import Catalog

QUEUE_OUTPUT: Final = "LfpBuildQueueUrl"
# The LFP builder's message kind for one dataset summary (`dataset_summary.SUMMARY`).
KIND: Final = "summary"
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2


class Sqs(Protocol):
    """The one SQS call the CLI makes."""

    def send_message(self, **kwargs: str) -> dict[str, Any]:
        """Send one message to a queue."""
        ...


def list_datasets(catalog: Catalog) -> list[str]:
    """Return the distinct datasets of the catalog `assets` table, sorted."""
    table = catalog.load_table(f"{schema.DATABASE}.{schema.TABLE}")
    rows = table.scan(selected_fields=("dataset",)).to_arrow().to_pylist()
    return sorted({str(r["dataset"]) for r in rows if r["dataset"]})


def message(dataset: str) -> str:
    """Return the message body that rebuilds one dataset's summary."""
    return json.dumps({"kind": KIND, "dataset": dataset})


def send_all(sqs: Sqs, queue_url: str, datasets: Iterable[str]) -> int:
    """Send one summary message per dataset, print SENT or FAIL, return the code."""
    sent = failed = 0
    for dataset in datasets:
        try:
            sqs.send_message(QueueUrl=queue_url, MessageBody=message(dataset))
        except (BotoCoreError, ClientError) as err:
            failed += 1
            sys.stdout.write(f"FAIL {dataset}: {err}\n")
            continue
        sent += 1
        sys.stdout.write(f"SENT {dataset}\n")
    if sent + failed == 0:
        sys.stdout.write("FAIL no dataset in the catalog assets table\n")
        return EXIT_FAIL
    sys.stdout.write(f"summaries: {sent} sent, {failed} failed\n")
    return EXIT_FAIL if failed else 0


def main() -> None:
    """Parse the flags, require the sandbox env, and send."""
    parser = argparse.ArgumentParser(prog="summaries")
    parser.add_argument("--dataset", default=None)
    args = parser.parse_args()
    if args.dataset == "":
        sys.stderr.write("--dataset needs a name\n")
        raise SystemExit(EXIT_USAGE)
    profile, account = require_sandbox()
    session = session_for(profile, account)
    queue_url = stack_outputs(session)[QUEUE_OUTPUT]
    datasets = [args.dataset] if args.dataset else list_datasets(glue_catalog(profile))
    raise SystemExit(send_all(session.client("sqs"), queue_url, datasets))


if __name__ == "__main__":
    main()
