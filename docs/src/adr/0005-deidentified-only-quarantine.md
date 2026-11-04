# ADR-0005: De-identified data only; SDK pre-check; admin-only quarantine with 7-day expiry

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

Medical imaging files can carry protected health information (PHI): names, birth dates, scan dates and
other identifiers. DICOM, the format MRI scanners produce, stores these in its headers, and some
images have identifying text burned into the pixels. The DICOM standard defines a de-identification
profile (PS3.15 Annex E). Burned-in text needs its separate "Clean Pixel Data" option, and a
de-identifier must set the PatientIdentityRemoved attribute and record the method it used.

The platform keeps raw data immutable. The raw bucket is versioned, a bucket policy denies writes and
deletes to everyone except the ingest role, and S3 Object Lock in governance mode protects each object
version.
Object Lock cannot be turned off once it is enabled on a bucket.

Two questions follow. Does the platform accept identified data at all? And what happens to an upload
that fails the de-identification check?

## Considered Options

- De-identified data only; the SDK checks headers before upload; server-side failures go to an
  admin-only quarantine with a 7-day expiry
- De-identified data only, rejecting any failing upload with no quarantine
- Accept identified data under a PHI compliance regime

## Decision Outcome

Chosen option: "De-identified data only, with an SDK pre-check and an admin-only quarantine with a
7-day expiry", because the platform holds no PHI by design, researchers get early
feedback on their own machine, and a file that fails on the server is held where admins can deal with
it rather than lost.

The flow:

1. Before upload, the SDK checks file headers on the researcher's machine and stops before uploading a
   file that looks identified, with a message that says what to fix.
2. After upload, the server validates each file again.
3. A file that fails server-side validation goes to the quarantine bucket, not to the raw bucket. Only
   admins can read the quarantine bucket, and every object in it expires after 7 days.
4. A file that passes goes to the raw bucket and is catalogued.

The quarantine bucket lives in DataStack.

### Consequences

**Transient PHI exposure, stated plainly.** Any file that fails server-side validation may contain PHI,
and it is stored in AWS, in the quarantine bucket, for up to 7 days before expiry deletes it. During
that window it is readable by admins. Admin-only access, the 7-day expiry and the SDK pre-check reduce
the exposure; they do not remove it. The window exists because the platform keeps failed uploads instead of discarding
them. A file can also reach S3 before any check runs, for example a raw DICOM upload that was not
de-identified on the client.

**PHI in the raw bucket: the only remediation path.** If a file that contains PHI passes validation by
mistake, it lands in the Object-Locked raw bucket. A plain delete there only adds a delete marker; the
locked version stays. In governance mode, a principal holding the `s3:BypassGovernanceRetention` permission can
override the lock. That
governance-mode bypass, used by an admin to delete the specific locked version, is the only remediation
path for PHI in the raw bucket. This is
also why the raw bucket must never use compliance mode: compliance mode blocks everyone, including the
account root user, and removing its lock requires deleting the account.

Other consequences:

- Good: no PHI is meant to be in the platform, so the ordinary data path needs no PHI handling.
- Good: researchers without their own de-identification tools are not simply blocked; the SDK tells
  them what failed, and an admin can see what the server rejected.
- Bad: a header check cannot see identifying text burned into pixels, which needs the separate Clean
  Pixel Data treatment.
- Bad: the governance bypass permission is itself powerful. Granting it to anyone but admins would
  weaken the raw-data immutability that Object Lock exists to provide.
- Neutral: who converts vendor raw data, and where de-identification tools run in the ingest pipeline,
  are decided in the ingest design.

## Pros and Cons of the Options

### De-identified only, SDK pre-check, admin-only quarantine with 7-day expiry

- Good: no PHI by design; early feedback; failed uploads kept for review.
- Bad: a bounded window of transient PHI in quarantine, and one remediation path (the governance
  bypass) for PHI that slips into the raw bucket.

### De-identified only, rejecting failing uploads with no quarantine

- Good: no quarantine window at all on the server side.
- Bad: rejected because it blocks researchers who have no de-identification tools of their own.

### Accept identified data under a PHI compliance regime

- Good: researchers could upload scanner output as it is.
- Bad: not chosen, because the platform takes de-identified data only. Every part of the platform that stores or
  moves data would then need PHI handling.

## More Information

- Related: [ADR-0002](0002-stateful-stateless-stack-split.md) (DataStack),
  [ADR-0006](0006-bids-nwb-native.md) (formats),
  [ADR-0010](0010-dynamodb-upload-sessions.md) (upload sessions and finalize).

## Update (2026-10): public archives only

In practice, imports come only from public de-identified archives (DANDI dandiset 000623 and OpenNeuro ds004798). No PHI handling was added and the quarantine bucket was not built. ds004798 faces are voxelated. Status stays Accepted.

## Update (2026-10): Object Lock per object

Object Lock is applied per object, after the verified copy (CRC check) into `raw/`, by the commit function; it is not a bucket default. Retention is a deployment setting (`RAW_RETENTION_DAYS`); the sandbox uses 1 day, because longer retention would leave every sandbox upload undeletable on a RETAIN bucket. A rollback delete and a landing delete stay allowed. PHI removal runbook: a temporary role holding the governance-bypass permission is created for the removal and deleted after it. Files committed before this change was deployed are not locked retroactively. Quarantine and automatic PHI checks are still not built. Status stays Accepted.

## Current state

The decision was de-identified data only, an SDK pre-check of headers, and an admin-only quarantine bucket with a 7-day expiry. The reason is that the platform holds no personal health information by design. It holds only in part. The code contains no SDK de-identification pre-check and no quarantine bucket or mover, so a file that fails verification is not moved to quarantine. The rule that only de-identified data is accepted remains a policy, not a built control. Nothing replaced the missing parts.
