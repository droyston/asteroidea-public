import re

import pytest

from asteroidea import errors

DENYLIST = re.compile(
    r"\b(AWS|Cognito|STS|ARN|IAM|boto3?)\b|arn:aws|AccessDenied|ExpiredToken"
    r"|NotAuthorized",
    re.IGNORECASE,
)
NEXT_STEP = re.compile(
    r"ask your administrator|run sign_in|try again|check your|upload again"
    r"|run query\(\)|use one of|take an asset_id|wait a minute|install asteroidea",
    re.IGNORECASE,
)


def _classes() -> list[type[errors.AsteroideaError]]:
    return [
        c
        for c in vars(errors).values()
        if isinstance(c, type) and issubclass(c, errors.AsteroideaError)
    ]


def test_every_error_derives_from_the_base() -> None:
    found = [
        c
        for c in vars(errors).values()
        if isinstance(c, type) and issubclass(c, Exception)
    ]
    assert found
    assert all(issubclass(c, errors.AsteroideaError) for c in found)
    assert len(_classes()) >= 6


@pytest.mark.parametrize("cls", _classes())
def test_message_names_a_next_step_and_no_cloud_terms(
    cls: type[errors.AsteroideaError],
) -> None:
    message = str(cls())
    assert NEXT_STEP.search(message), message
    assert not DENYLIST.search(message), message
