# asteroidea

asteroidea is an AWS data platform for multimodal neuroscience research data, such as MRI images and
electrophysiology recordings. It is built for small research and development teams that analyse neural
data and want simple tools with sensible defaults.

The platform keeps primary data in S3, and raw files are read-only. An Apache Iceberg catalog holds the
metadata, so a researcher can query the whole corpus with SQL and also read single files lazily.
Researchers use a Python SDK or a web app. Administrators run the deployment through AWS IAM Identity
Center. The infrastructure is Python code written with the AWS CDK.

## What runs today

The platform runs in one sandbox AWS account. A researcher can:

- sign in to the hosted web app, or from the SDK;
- browse datasets, subjects, sessions and files in the catalog;
- query the catalog with SQL, at a fixed snapshot;
- read NWB and NIfTI files lazily, plot spike rates and LFP windows, and view MRI planes;
- upload files and folders (the first uploader owns a dataset name);
- share labels on a file, and save results, readable by every researcher, with a record of the files they came from.

A curator can also import a public dataset from DANDI or OpenNeuro.

The deployment has two CloudFormation stacks. DataStack holds the state: buckets
(including the site bucket that serves the web app), tables, the catalog and the user pool.
ServicesStack holds the code: 12 Lambda functions, an HTTP API with 36 routes, the write queues and
the re-extract state machine. The catalog has 14 tables.

Not built yet: the VPC, validation and quarantine of uploads, multipart upload and continuous
integration.

## Where to start

| You are | Read |
|---|---|
| A researcher | [For researchers](researchers.md): sign in, set up the SDK, query, read, upload |
| A developer | [Developer guide](developers.md): commands by task, layout, gates, and how to add a route, a function or a table |
| Reading the design | The architecture chapters, starting with [Introduction and goals](01-introduction.md) and the [Building block view](05-building-blocks.md) |
| Asking why | The decision records, listed under Architecture decisions in the sidebar, and the [Decisions](09-decisions.md) chapter |
| Looking up a word | The [Glossary](12-glossary.md) |

## How this book is organised

The chapters follow the arc42 template for architecture documents. Each decision record (ADR) states one
decision, the options, and the consequences. Records are kept as they were written; where the system has
since changed, the chapters describe the system as it is.
