"""Each zip-built API Lambda imports its handler with only what the zip carries.

`functions.build_function` zips `ingest/src` and nothing else: the Python 3.14 runtime
adds boto3 and botocore. A handler whose import chain reaches pyiceberg or
`asteroidea_infra` fails at init, and API Gateway answers 500 "Internal Server Error"
before any handler code runs (seen live with ImportsFunction).
"""

import sys

import pytest

from asteroidea_infra.functions import FUNCTIONS
from asteroidea_infra.runner import execute

# The module of every zip function's handler, from the registry rows.
HANDLERS = sorted({s.handler.rsplit(".", 1)[0] for s in FUNCTIONS if s.image is None})

# Runs in a clean interpreter: any top-level import outside the runtime's own
# libraries and the zipped package is refused, as it would fail in the Lambda.
PROBE = """
import importlib
import importlib.abc
import sys

RUNTIME = {"boto3", "botocore", "s3transfer", "jmespath", "dateutil", "urllib3", "six"}
ALLOWED = set(sys.stdlib_module_names) | RUNTIME | {"asteroidea_ingest"}

class Bundle(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top not in ALLOWED and not top.startswith("_"):
            raise ModuleNotFoundError(f"not in the zip or the runtime: {name}")

sys.meta_path.insert(0, Bundle())
importlib.import_module(sys.argv[1])
"""


def test_the_api_handlers_were_found() -> None:
    zipped = sum(s.image is None for s in FUNCTIONS)
    # Five zip functions today: every API function but ComputeFunction.
    assert len(HANDLERS) == zipped >= 5, HANDLERS


@pytest.mark.parametrize("module", HANDLERS)
def test_handler_imports_with_only_the_zip_and_the_runtime(module: str) -> None:
    result = execute([sys.executable, "-I", "-c", PROBE, module], timeout=60)
    assert result.code == 0, result.out + result.err
