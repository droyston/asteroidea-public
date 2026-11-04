# ADR-0016: File bytes never transit the API; presigned PUT and multipart

Status: Proposed
Date: 2026-10-22

## Context and Problem Statement

Research files are large. A NIfTI image is about 10 MB to 1 GB, a DICOM series is thousands of
0.1–0.5 MB files, and an NWB recording is one object of 0.1 to more than 100 GB. API Gateway caps a request payload at 10 MB on both
REST and HTTP APIs; a synchronous Lambda takes at most 6 MB per request; and the integration timeout
is 29 s on REST and 30 s on HTTP APIs. The question
is how file bytes travel from a researcher's machine or browser into S3.

This decision follows from the researcher journeys rather than from an explicit requirement, and it is
therefore Proposed. Nothing contests it, and every upload decision (sessions, quarantine) assumes it.

## Considered Options

- The API issues presigned S3 URLs; clients upload bytes directly to S3 with a single PUT or a
  multipart upload.
- Bytes go through API Gateway and Lambda.
- The SDK writes to S3 directly with its own temporary AWS credentials.
- Presigned URLs through S3 Transfer Acceleration by default.

## Decision Outcome

Chosen option: "Presigned single PUT or multipart, direct to S3", because it is the only option that
handles files of every size without a server in the byte path, and it keeps the ingest service as
the only identity that can write raw data.

The client sends the API a manifest (file list, sizes, client checksums) inside an upload session
([ADR-0010](0010-dynamodb-upload-sessions.md)); the API returns presigned URLs. A single presigned PUT
handles objects up to 5 GB. Larger objects, and any upload that must survive a dropped connection,
use multipart upload: parts of 5 MiB to 5 GiB, up to 10,000 parts, objects up to 48.8 TiB. The URLs are signed by the ingest service's role, which is
the only role the raw bucket's policy lets write.

### Consequences

- Good: no size ceiling below S3's own, and no API timeout on the byte path.
- Good: a failed part is retried alone, which is what makes a large upload resumable. The resumable
  5 GB upload in [Quality requirements](../10-quality.md) is the target this serves.
- Good: researchers never hold write credentials for raw data.
- Bad: presigned URLs signed with role credentials last only as long as the signing session, so a
  Lambda's role session can cut a URL short; long uploads need the SDK to ask for fresh part URLs.
- Bad: S3's full-object checksum for multipart uploads exists only for CRC64NVME, CRC32 and CRC32C. A
  multipart SHA256 is a checksum of part checksums, not the file's SHA256. The platform verifies CRC64NVME, the S3 default, and
  records any separate SHA256 as its own column if cross-system identity is needed.
- Bad: abandoned multipart uploads keep their parts until aborted, so the upload bucket needs a
  lifecycle rule that aborts incomplete uploads.
- Unverified: presigned multipart `UploadPart` was not covered by the documentation page consulted. It is not yet exercised (see the update below).

## Pros and Cons of the Options

### Presigned PUT and multipart, direct to S3 (chosen)

- Good: any size, resumable, no server in the byte path, write access stays with one role.
- Bad: URL lifetime and checksum semantics need care, as above.

### Bytes through API Gateway and Lambda

- Rejected: the 10 MB payload cap, the 6 MB synchronous Lambda limit and the 29–30 s timeout rule out
  every real file.

### SDK writes to S3 with its own temporary credentials

- Bad: the raw bucket denies `Put` and `Delete` to every principal except the ingest role, so
  researcher credentials would need write access to raw data, which the immutability goal forbids. The identity-pool credentials
  of [ADR-0017](0017-identity-pool-data-access.md) are for reads.

### Transfer Acceleration by default

- Bad: it needs a bucket name without dots, works only in listed Regions, and costs extra. Its use with presigned URLs was not verified. It stays an
  option to measure if upload speed becomes a problem.

## More Information

- Related: [ADR-0010](0010-dynamodb-upload-sessions.md) (sessions and finalize),
  [ADR-0005](0005-deidentified-only-quarantine.md) (what happens when validation fails).
- Runtime: the upload scenario in the [runtime view](../06-runtime.md).

## Update (2026-10): what was verified

The Status stays Proposed: only the single-PUT half of the decision was exercised.

- **Verified.** A single presigned PUT carrying a client CRC64NVME checksum: the SDK uploaded a 3 MiB
  sample, the commit Lambda read the stored checksum from the landing object and compared it with the
  client's value, and a deliberately wrong checksum was refused with the session INCOMPLETE and nothing
  readable in `raw/`.
- **Landing and raw split.** The PUT targets `landing/<session_id>/<relpath>`, which researchers cannot
  read. Only a verified object is copied to `raw/<dataset>/<relpath>`. Two ingest roles exist:
  SessionApiRole signs the URLs and CommitRole copies.
- **Unverified, deferred.** Presigned multipart `UploadPart` and Complete, the full-object checksum
  on a multipart upload, URL refresh during a long upload, and the 5 GB case. They are not yet built.
- **Added later.** The raw-bucket policy that lets only the ingest roles write was not part of the initial upload path.
  Researchers cannot write because ResearcherRole carries an explicit deny
  on `PutObject*` and `DeleteObject*` for the raw bucket. The bucket policy and Object Lock came later.

## Update (2026-10): dataset uploads

A multi-file dataset upload (`upload_dataset`) is one upload session per file, grouped by an `upload_id`; there is still no multipart upload and no multi-file session item. Files over 5 GiB remain out of scope. Status stays Proposed.

## Current state

Clients upload file bytes straight to S3 with presigned URLs, and the API carries only manifests and URLs. The reason is that no server sits in the byte path and only the ingest code can write raw data. It holds for the single presigned PUT: `uploads_api.py` generates the URL. Multipart upload is not built; no code creates multipart uploads, so large files are not supported. The status stays Proposed for that reason.
