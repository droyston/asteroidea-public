"""Identity-pool credentials from the id token, cached and refreshed before expiry."""

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Final, Protocol, TypedDict, cast

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from asteroidea._auth import REFRESH_MARGIN, Auth
from asteroidea.errors import (
    CredentialsUnavailableError,
    PlatformUnreachableError,
    SignInRequiredError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from asteroidea._config import PlatformConfig

_REFUSED_CODE: Final = "NotAuthorizedException"


class IdentityIdReply(TypedDict):
    """The part of the `get_id` reply this module reads."""

    IdentityId: str


class RawCredentials(TypedDict):
    """The credentials block of a `get_credentials_for_identity` reply."""

    AccessKeyId: str
    SecretKey: str
    SessionToken: str
    Expiration: datetime


class CredentialsReply(TypedDict):
    """The part of the `get_credentials_for_identity` reply this module reads."""

    IdentityId: str
    Credentials: RawCredentials


class IdentityClient(Protocol):
    """The two identity-pool calls this module makes."""

    def get_id(self, **kwargs: str | Mapping[str, str]) -> IdentityIdReply:
        """Return the identity id for the logins."""
        ...

    def get_credentials_for_identity(
        self, **kwargs: str | Mapping[str, str]
    ) -> CredentialsReply:
        """Return temporary credentials for the identity."""
        ...


@dataclass(frozen=True)
class Credentials:
    """Temporary data-access credentials; `expires_at` is epoch seconds."""

    access_key_id: str
    secret_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expires_at: float


def identity_client(config: PlatformConfig) -> IdentityClient:
    """Return an unsigned identity-pool client; the id token authorizes the calls."""
    return cast(
        "IdentityClient",
        boto3.client(
            "cognito-identity",
            region_name=config.region,
            config=Config(signature_version=UNSIGNED),
        ),
    )


def _cached(auth: Auth) -> Credentials | None:
    """Return the credentials saved in the sign-in cache, or `None` if unusable."""
    saved = auth.cache.read().get("credentials")
    try:
        return Credentials(**saved) if isinstance(saved, dict) else None
    except TypeError:
        return None


def credentials(
    auth: Auth, client: IdentityClient | None = None, *, force: bool = False
) -> Credentials:
    """Return credentials with over 5 minutes left, exchanging the id token if needed.

    `force` exchanges again even when the saved credentials look fresh.
    Raises `SignInRequiredError` when there is no sign-in to exchange.
    """
    tokens = auth.tokens()
    cached = _cached(auth)
    if (
        not force
        and cached is not None
        and cached.expires_at - auth.clock() > REFRESH_MARGIN
    ):
        return cached
    config = auth.config
    provider = f"cognito-idp.{config.region}.amazonaws.com/{config.user_pool_id}"
    logins = {provider: tokens.id_token}
    identity = client or identity_client(config)
    try:
        identity_id = identity.get_id(
            IdentityPoolId=config.identity_pool_id, Logins=logins
        )["IdentityId"]
        raw = identity.get_credentials_for_identity(
            IdentityId=identity_id, Logins=logins
        )["Credentials"]
        fresh = Credentials(
            access_key_id=raw["AccessKeyId"],
            secret_key=raw["SecretKey"],
            session_token=raw["SessionToken"],
            expires_at=raw["Expiration"].timestamp(),
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == _REFUSED_CODE:
            raise SignInRequiredError from err
        raise CredentialsUnavailableError from err
    except BotoCoreError as err:
        raise PlatformUnreachableError from err
    auth.cache.write({**auth.cache.read(), "credentials": asdict(fresh)})
    return fresh


def session(auth: Auth, client: IdentityClient | None = None) -> boto3.Session:
    """Return a boto3 session holding the current credentials.

    The session does not renew itself; call this again for each unit of work.
    """
    held = credentials(auth, client)
    return boto3.Session(
        aws_access_key_id=held.access_key_id,
        aws_secret_access_key=held.secret_key,
        aws_session_token=held.session_token,
        region_name=auth.config.region,
    )
