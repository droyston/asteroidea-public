"""The HTTP API route table: every route, the function that serves it, its group.

Standard library only, so the zip Lambdas can import it. `infra/.../api.py` builds one
API Gateway route per row.

Ordering rule: each function's first row stays first. CDK creates a function's
integration under the first route bound to it, and that route's path is part of the
integration's logical id. Add a new row at the end of its function's block or at the
end of the table, and add its key to `infra/tests/fixtures/routes.txt`.
"""

from dataclasses import dataclass
from typing import Final

RESEARCHERS: Final = "researchers"
CURATORS: Final = "curators"


@dataclass(frozen=True)
class Route:
    """One HTTP API route."""

    # "GET", "POST", "PATCH" or "DELETE" (the CORS set in api.py).
    method: str
    # API Gateway path; parameters as {name}.
    path: str
    # The FunctionSpec construct id that serves it, e.g. "CatalogFunction".
    function: str
    # The Cognito group the caller must be in: "researchers" or "curators".
    group: str
    # One line for the API reference page.
    summary: str
    # Timing-line name; "" means derived from method and path.
    timing: str = ""

    @property
    def key(self) -> str:
        """Return the HTTP API routeKey, e.g. "GET /catalog/datasets"."""
        return f"{self.method} {self.path}"


ROUTES: Final[tuple[Route, ...]] = (
    Route(
        "POST", "/sessions", "SessionFunction", RESEARCHERS, "Open an upload session"
    ),
    Route(
        "POST",
        "/sessions/{id}/finalize",
        "SessionFunction",
        RESEARCHERS,
        "Finalize an upload session and queue its commit",
    ),
    Route("GET", "/sessions/{id}", "SessionFunction", RESEARCHERS, "Read one session"),
    Route(
        "GET", "/uploads", "SessionFunction", RESEARCHERS, "List the caller's uploads"
    ),
    Route(
        "GET",
        "/get-started",
        "PlatformFunction",
        RESEARCHERS,
        "Return the setup command and handout links",
    ),
    Route("POST", "/artifacts", "PlatformFunction", RESEARCHERS, "Start an artifact"),
    Route(
        "POST",
        "/artifacts/{id}/finalize",
        "PlatformFunction",
        RESEARCHERS,
        "Finalize an artifact upload",
    ),
    Route(
        "GET",
        "/artifacts",
        "PlatformFunction",
        RESEARCHERS,
        "List the caller's artifacts, every author's, or those that used one input",
    ),
    Route(
        "GET", "/artifacts/{id}", "PlatformFunction", RESEARCHERS, "Read one artifact"
    ),
    Route(
        "PATCH",
        "/artifacts/{id}",
        "PlatformFunction",
        RESEARCHERS,
        "Edit the caller's artifact note and name",
    ),
    Route(
        "POST",
        "/artifacts/{id}/withdraw",
        "PlatformFunction",
        RESEARCHERS,
        "Withdraw the caller's artifact",
    ),
    Route(
        "DELETE",
        "/artifacts/{id}",
        "PlatformFunction",
        RESEARCHERS,
        "Delete the caller's artifact that no other artifact used",
    ),
    Route(
        "POST",
        "/notes",
        "PlatformFunction",
        RESEARCHERS,
        "Leave an app-dev feedback note with the page address and view",
    ),
    Route(
        "GET", "/catalog/datasets", "CatalogFunction", RESEARCHERS, "List the datasets"
    ),
    Route(
        "GET",
        "/catalog/datasets/{dataset}",
        "CatalogFunction",
        RESEARCHERS,
        "Read one dataset",
    ),
    Route(
        "GET",
        "/catalog/datasets/{dataset}/assets",
        "CatalogFunction",
        RESEARCHERS,
        "List a dataset's files",
    ),
    Route(
        "GET",
        "/catalog/assets/{asset_id}",
        "CatalogFunction",
        RESEARCHERS,
        "Read one file's catalog row",
    ),
    Route(
        "GET",
        "/catalog/assets/{asset_id}/tables/{table}",
        "CatalogFunction",
        RESEARCHERS,
        "Read one file's rows of a catalog table",
    ),
    Route(
        "GET",
        "/catalog/assets/{asset_id}/events",
        "CatalogFunction",
        RESEARCHERS,
        "Read one file's events and intervals",
    ),
    Route(
        "GET",
        "/catalog/assets/{asset_id}/download",
        "CatalogFunction",
        RESEARCHERS,
        "Presign a download of one file",
        timing="catalog_download",
    ),
    Route(
        "GET",
        "/catalog/studies",
        "CatalogFunction",
        RESEARCHERS,
        "List the studies and every dataset's subjects, sessions and regions",
    ),
    Route(
        "GET",
        "/catalog/links",
        "CatalogFunction",
        RESEARCHERS,
        "List subject links by study, subject key, or dataset and subject",
    ),
    Route(
        "GET",
        "/catalog/assets",
        "CatalogFunction",
        RESEARCHERS,
        "Name up to 100 files by id, each with its version and whether it is current",
    ),
    Route(
        "GET",
        "/compute/rates",
        "ComputeFunction",
        RESEARCHERS,
        "Compute unit firing rates",
    ),
    Route(
        "GET",
        "/compute/overview",
        "ComputeFunction",
        RESEARCHERS,
        "Read an LFP overview",
    ),
    Route(
        "GET",
        "/compute/spectrogram",
        "ComputeFunction",
        RESEARCHERS,
        "Compute an LFP window and its spectrogram",
    ),
    Route(
        "GET",
        "/compute/events",
        "ComputeFunction",
        RESEARCHERS,
        "List a file's alignment events from its dataset's recipe",
    ),
    Route(
        "GET",
        "/compute/tuning",
        "ComputeFunction",
        RESEARCHERS,
        "Screen unit tuning to one event attribute (exploratory)",
    ),
    Route(
        "GET",
        "/catalog/session-summary",
        "ComputeFunction",
        RESEARCHERS,
        "Read a session's stored summary, or say none is built yet",
    ),
    Route(
        "GET",
        "/files/{asset_id}/labels",
        "LabelsFunction",
        RESEARCHERS,
        "List a file's shared labels",
    ),
    Route(
        "POST",
        "/files/{asset_id}/labels",
        "LabelsFunction",
        RESEARCHERS,
        "Create a shared label",
        timing="labels_create",
    ),
    Route(
        "PATCH",
        "/files/{asset_id}/labels/{label_id}",
        "LabelsFunction",
        RESEARCHERS,
        "Edit the caller's label",
    ),
    Route(
        "DELETE",
        "/files/{asset_id}/labels/{label_id}",
        "LabelsFunction",
        RESEARCHERS,
        "Delete the caller's label",
    ),
    Route(
        "GET", "/imports", "ImportsFunction", CURATORS, "List the caller's import runs"
    ),
    Route("POST", "/imports", "ImportsFunction", CURATORS, "Start an import run"),
    Route(
        "GET", "/imports/{run_id}", "ImportsFunction", CURATORS, "Read one import run"
    ),
)
