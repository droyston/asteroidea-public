"""Makefile guard: aws-cdk AWS verb targets use the sandbox guard and profile."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
TARGET = re.compile(r"^([A-Za-z0-9_-]+):(?!=)")
CDK_AWS_VERB = re.compile(r"aws-cdk@\S+\s+(diff|deploy|destroy|bootstrap)\b")
GUARD = "$(SANDBOX_GUARD)"
PROFILE_FLAG = '--profile "$$ASTEROIDEA_PROFILE"'


def _recipes(text: str) -> dict[str, str]:
    recipes: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("\t") and current is not None:
            recipes[current] += line + "\n"
            continue
        match = TARGET.match(line)
        current = str(match.group(1)) if match else None
        if current is not None:
            recipes[current] = ""
    return recipes


def test_parser_sees_the_aws_targets() -> None:
    recipes = _recipes(MAKEFILE.read_text())
    cdk_targets = {name for name, body in recipes.items() if CDK_AWS_VERB.search(body)}
    # Non-vacuous: the parser must find the live targets, or the guard check is empty.
    assert {"deploy", "diff", "destroy-services"} <= cdk_targets


def test_every_cdk_aws_target_is_guarded_with_profile() -> None:
    recipes = _recipes(MAKEFILE.read_text())
    for name, body in sorted(recipes.items()):
        verb = CDK_AWS_VERB.search(body)
        if verb is None:
            continue
        assert GUARD in body, f"{name}: aws-cdk {verb.group(1)} without {GUARD}"
        assert body.index(GUARD) < verb.start(), (
            f"{name}: guard must run before the aws-cdk call"
        )
        assert PROFILE_FLAG in body, (
            f"{name}: aws-cdk {verb.group(1)} without {PROFILE_FLAG}"
        )
