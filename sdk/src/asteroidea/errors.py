"""SDK error types. Every message says what to do next and names no cloud service."""


class AsteroideaError(Exception):
    """Base class of every error the SDK raises on purpose."""

    default: str = (
        "Something went wrong; try again, and if it keeps failing "
        "ask your administrator."
    )

    def __init__(self, detail: str | None = None) -> None:
        """Use `detail` as the message when given, else the class default."""
        super().__init__(detail or self.default)


class ConfigMissingError(AsteroideaError):
    """The platform settings file is missing or incomplete."""

    default = (
        "Platform settings not found; ask your administrator for platform.json "
        "and save it as ~/.config/asteroidea/platform.json."
    )


class SignInRequiredError(AsteroideaError):
    """No usable sign-in: no cache, or the refresh token is gone."""

    default = (
        "You are not signed in, or your session has ended; "
        "run sign_in() to sign in again."
    )


class SignInFailedError(AsteroideaError):
    """Interactive sign-in did not finish."""

    default = "Sign-in did not finish; run sign_in() again."


class NotAResearcherError(AsteroideaError):
    """The account is not in the researchers group."""

    default = (
        "This account is not a researcher account; ask your administrator "
        "to add it to the researchers group, then run sign_in() again."
    )


class PlatformUnreachableError(AsteroideaError):
    """The platform could not be reached."""

    default = (
        "Could not reach the platform; check your network connection and try again."
    )


class CredentialsUnavailableError(AsteroideaError):
    """The platform refused to issue data access to a signed-in account."""

    default = (
        "The platform did not give this account access to data; run sign_in() "
        "again, and if it fails again ask your administrator."
    )


class UploadRefusedError(AsteroideaError):
    """The platform refused an upload request (a malformed or duplicate request)."""

    default = "The platform refused the upload; fix the request and try again."


class DatasetNotYoursError(UploadRefusedError):
    """The dataset name belongs to another researcher (the server's 403 code)."""

    default = (
        "That dataset belongs to another researcher; upload under another "
        "dataset name and try again."
    )


class UploadIncompleteError(AsteroideaError):
    """The file did not arrive intact; nothing is readable and no row was written."""

    default = "The file did not arrive intact; upload again."


class ArtifactRefusedError(AsteroideaError):
    """The platform refused a saved-result request (a malformed request)."""

    default = "The platform refused the result; fix the request and try again."


class ArtifactNotFoundError(AsteroideaError):
    """No saved result of yours has this id, or it was never finished."""

    default = (
        "No saved result has that id; run artifacts() and try again with an id from it."
    )


class FinalizeTimeoutError(AsteroideaError):
    """The upload was accepted but did not become visible in time."""

    default = (
        "The upload was accepted but is not visible yet; wait a minute, then "
        "check with query(), and upload again if it is still missing."
    )


class SnapshotGoneError(AsteroideaError):
    """The requested snapshot of the catalog no longer exists."""

    default = "That snapshot no longer exists; run query() without snapshot_id."


class UnknownFilterError(AsteroideaError):
    """A query filter names a column the catalog does not offer."""

    default = "Unknown filter; use one of the listed filter names."


class AssetNotFoundError(AsteroideaError):
    """No asset has this id."""

    default = "No asset has that id; take an asset_id from a query() result."


class ReadInterruptedError(AsteroideaError):
    """A remote read failed even after the credentials were renewed."""

    default = (
        "The read was interrupted; run sign_in() again and repeat the read, "
        "and if it fails again ask your administrator."
    )


class ExtraMissingError(AsteroideaError):
    """An optional package is not installed."""

    default = "A package is missing; install asteroidea[nifti] and try again."


class QueryError(AsteroideaError):
    """A SQL query or a catalog table read failed; the message is the engine's own."""

    default = "The query failed; check the query text and try again."


class NotNwbError(AsteroideaError):
    """The asset is not an NWB file."""

    default = (
        "That file is not an NWB file; "
        "run query() and take the asset_id of an NWB file."
    )


class NotTheAuthorError(AsteroideaError):
    """The result is another researcher's; only its author may change it (403)."""

    default = (
        "Only the author of this result can change, withdraw or delete it; "
        "run artifacts() to see your own results and try again with one of those."
    )


class IdTokenRequiredError(SignInRequiredError):
    """The platform needs the sign-in's ID token to save a run's result (401)."""

    default = (
        "The platform could not read your email from this sign-in, so it did not "
        "save the result; run sign_in() again, and if it fails again ask your "
        "administrator."
    )


class NestedRunError(AsteroideaError):
    """A `run()` was started while another `run()` was still open."""

    default = (
        "A run is already open; finish it (leave its with block) and try again, "
        "or save both results from the one run."
    )


class HasDependentsError(ArtifactRefusedError):
    """Other results were made from this one, so it cannot be deleted (409).

    The message names those results. Withdraw it instead: a withdrawn result stays
    readable, so the results made from it keep their inputs.
    """

    default = (
        "Other results were made from this one, so it cannot be deleted; "
        "withdraw it with withdraw_artifact() instead, or try again once "
        "those results are deleted."
    )
