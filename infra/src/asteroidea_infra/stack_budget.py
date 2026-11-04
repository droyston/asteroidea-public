"""Per-template size budget, so a stack is split before the limit.

Pure Python: no `aws_cdk` import, so the gate stays fast. The numbers keep
headroom under the CloudFormation hard limits of 500 resources, 200 outputs, 200
parameters and 1,000,000 bytes, which leaves room to split in a separate change.
"""

import json
from typing import Any, Final

MAX_RESOURCES: Final = 300
MAX_OUTPUTS: Final = 150
MAX_PARAMETERS: Final = 150
MAX_BYTES: Final = 800_000


def template_bytes(template: dict[str, Any]) -> int:
    """Return the size of the template as CDK writes it (JSON, one-space indent)."""
    return len(json.dumps(template, indent=1).encode())


def over_budget(name: str, template: dict[str, Any]) -> list[str]:
    """Return one refusal per measure of `template` that passes its budget."""
    measures = (
        ("resources", len(template.get("Resources", {})), MAX_RESOURCES),
        ("outputs", len(template.get("Outputs", {})), MAX_OUTPUTS),
        ("parameters", len(template.get("Parameters", {})), MAX_PARAMETERS),
        ("bytes", template_bytes(template), MAX_BYTES),
    )
    return [
        f"{name}: {size} {unit}, budget {limit}"
        for unit, size, limit in measures
        if size > limit
    ]
