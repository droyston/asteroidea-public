# Introduction and goals

## What the platform is

asteroidea is a cloud data platform on AWS for multimodal neuroscience research data. A small
research organisation collects two kinds of data: MRI images (DICOM series and NIfTI volumes) and
electrophysiology recordings (NWB files and vendor raw formats). The researchers write code. They
want simple interfaces, sensible defaults and error messages that say what to do next.

Before the platform there was no shared place that did five things. The platform now does them:

- It stores each raw file once and never lets it change silently.
- It catalogues each file with the metadata needed to find it again.
- It answers a question across the whole corpus ("all T1-weighted scans of subjects aged 20 to 30")
  with exact file references.
- It lets a researcher open one large file and read only the part they need.
- It stores the results of an analysis with a record of the inputs that produced them.

Researchers use a web app and a Python SDK. Administrators operate the platform through AWS IAM
Identity Center, the AWS CLI and the console. Analysis results go to a separate artifacts bucket,
not to the main data. The [context chapter](03-context-and-scope.md) defines that boundary.

## What runs today

The platform runs in one sandbox AWS account and one region (us-east-1). A researcher can sign in to
the hosted web app or from the SDK, browse datasets, subjects, sessions and files, query the catalog
with SQL at a fixed snapshot, read NWB and NIfTI files lazily, upload files, share labels on a file
and save results that every researcher can read. A curator can import a public dataset from DANDI or OpenNeuro.

Two things are not built: a private network (VPC) and the checks that hold a suspect upload apart
(validation and quarantine). Multipart upload and continuous integration are also missing.

This book describes the system as it is. Each decision has its own record (an ADR, an Architecture
Decision Record); the [decisions chapter](09-decisions.md) lists them. Some records are
**Provisional**: the direction is chosen, but a measurement may still change it. One record is
**Proposed**: it is written but not yet confirmed.


## User stories

Researchers:

1. **Upload.** I upload an MRI session (a DICOM series, or NIfTI files with BIDS sidecars) or an
   electrophysiology recording (an NWB file or a vendor raw folder). The platform stores it once,
   checks its checksum and catalogues it.
2. **Browse.** I browse the catalog in the web app to see which datasets, subjects, sessions and
   files exist.
3. **Query the corpus.** I run a query across the whole catalog and get back exact file references
   (asset, file version, checksum), not copies of the bytes.
4. **Read lazily.** I open one large file from a laptop or notebook and read only the slices I need.
5. **Save analysis results.** I save the output of an analysis. The SDK records which source files,
   file versions and catalog snapshot it came from. I never write into the main data.

Administrators:

6. **Deploy.** I sign in through IAM Identity Center and deploy or update the platform with the AWS CDK.
7. **Operate.** I manage researcher accounts, watch cost and health, and (once quarantine exists)
   review held uploads.

## Quality goals

The goals are ranked. When two conflict, the higher one wins.

| Rank | Goal | What it means here |
|---|---|---|
| 1 | Raw-data integrity and immutability | A finalized raw file is never overwritten or deleted by a researcher or a service. Its checksum is recorded at upload and can be checked later. |
| 2 | Reproducible queries | A query can be re-run later against the same catalog state and return the same rows. |
| 3 | Low idle cost | A platform with no traffic costs close to nothing per month. |
| 4 | De-identified data only | The platform accepts only de-identified data. |
| 5 | Operable by one developer | One person can deploy, upgrade and debug the whole platform. |
| 6 | Productive in the first hour | A new researcher installs the SDK, signs in, uploads a sample, queries and reads within an hour. |

The [quality chapter](10-quality.md) gives a measurable scenario for each goal.

## Stakeholders

| Stakeholder | Role | What they expect of the platform | What they expect of this book |
|---|---|---|---|
| Researcher | Uploads, browses, queries and reads data; saves results | A simple SDK and web app, sensible defaults, plain error messages | What the platform promises and what it refuses |
| Administrator | Deploys and operates the platform | Small, explainable infrastructure; low idle cost | Each stack, its resources and its failure modes |
| Project lead | Decides architecture, scope and meaning | A design record that later work starts from | Every decision traceable to a record; open questions named |
| Future contributor | Builds on the platform | A repository that installs and checks in one command each | Which decisions are settled and which wait for a measurement |
| Data steward | Answers for the de-identification rule | Only de-identified data stored | A plain statement of what is and is not checked |
| Neighbour: the artifacts bucket | Receives analysis results | A stable record of each result's inputs | The boundary, in the [context chapter](03-context-and-scope.md) |

## How to read this book

The chapters follow the arc42 template. Words used across the book are defined in the
[glossary](12-glossary.md).

