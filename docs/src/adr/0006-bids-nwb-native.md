# ADR-0006: BIDS and NWB native; vendor raw ephys ingest-only

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

The platform stores two families of neuroscience data, and each has a community standard:

- **MRI: BIDS** (Brain Imaging Data Structure). BIDS defines file names and folders from a set of 35
  entities (subject, session and so on) plus JSON "sidecar" files for acquisition parameters. A
  T1-weighted scan, for example, lives at `sub-*/ses-*/anat/*_T1w.nii.gz` with a `.json` sidecar.
- **Electrophysiology (ephys): NWB** (Neurodata Without Borders), an HDF5-based container. One NWB
  file is one object of 0.1 to more than 100 GB.

Ephys acquisition systems also write their own vendor formats, each a folder of several files: Open
Ephys, SpikeGLX, Intan and Blackrock among them. The question is which formats the platform treats as its native, catalogued formats.

## Considered Options

- BIDS and NWB as the native formats; vendor raw ephys formats accepted at ingest only
- Vendor raw formats as first-class native formats alongside BIDS and NWB
- Convert everything to a cloud-native array format (Zarr, OME-Zarr) at ingest

## Decision Outcome

Chosen option: "BIDS and NWB as the native formats; vendor raw ephys formats accepted at ingest only",
because the two standards are widely used and tool-supported, and they give the catalog a common
vocabulary.

- The catalog stores BIDS entities, DICOM-derived fields and platform fields as typed columns.
- Vendor raw folders can be uploaded, but they are inputs to conversion, not native catalogued data.
- Files are stored as they arrive.
- The data model follows DANDI, the public NWB archive: an immutable blob (bytes, checksum, size), an
  asset (path plus metadata) and a versioned collection.

### Consequences

- Good: researchers query by familiar BIDS entities and NWB fields, and existing BIDS and NWB tools
  work on the stored files.
- Bad: neither standard defines a subject's age at scan or a quality-control (QC) state. A NIfTI header
  carries no subject, age or date, and BIDS keeps age in `participants.tsv`. Age and QC are therefore
  columns the platform owns.
- Bad: QC changes after ingest, while catalog records are immutable. The planned resolution
  is QC as a separate append-only table.
- Bad: who converts vendor raw data to BIDS or NWB is open and is decided in the ingest design.
- Bad: reading NWB lazily over S3 works, but the tooling is immature. The pynwb documentation calls
  remfile "very new" and says fsspec is slow for HDF5.
- Bad: Neo, the library that reads vendor headers without loading the data, documents local files
  only, so an extractor copies the small header files to local disk first.
- Neutral: extractor size is a measured question. The unzipped size and import time of pynwb, neo, h5py,
  pydicom and nibabel for Lambda still need measuring; only the compressed wheel
  sizes were measured.
- Neutral: ephys-in-BIDS proposals are deferred until their status is confirmed.

## Pros and Cons of the Options

### BIDS and NWB native; vendor raw ingest-only

- Good: one vocabulary for the catalog; community tools work on the stored data.
- Bad: conversion from vendor formats is still to be designed.

### Vendor raw formats as first-class native formats

- Good: no conversion step; researchers upload what the rig produced.
- Bad: not chosen; vendor formats are ingest-only. Each vendor folder has its own layout, so the catalog would need per-vendor columns and
  extractors, and queries would not share one vocabulary.

### Convert everything to Zarr or OME-Zarr at ingest

- Good: Zarr v3 and OME-Zarr are the cloud-native direction, and chunked Zarr allows partial reads
  from S3, which HDF5 and NIfTI do not.
- Bad: not chosen at MVP. The adoption evidence found for OME-Zarr is early, and whether DANDI or BIDS
  is adopting NWB-Zarr was not verified. Files are stored as they arrive at MVP.

## More Information

- Related: [ADR-0005](0005-deidentified-only-quarantine.md) (de-identification at ingest),
  [ADR-0010](0010-dynamodb-upload-sessions.md) (multi-file upload sessions).

## Update (2026-10): extraction

Extraction reads BIDS and NWB natively. ExtractFunction runs after each commit and fills `asset_headers`, `series`, `electrodes`, `units` and `intervals` from NWB (pynwb) and the NIfTI header, plus BIDS sidecars. Scene-cut annotations (`scenecut_info.csv`) are imported as an ancillary file under `stimuli/`. First real import: CS62 R2 gave units 44, electrodes 96, intervals 41. Status stays Accepted.

## Current state

BIDS is the native layout for MRI and NWB for electrophysiology, and vendor raw formats are accepted at ingest only. The reason is that both standards are widely used and give the catalog a common vocabulary. It holds. The extractors in `ingest/src/asteroidea_ingest/extract/` read BIDS, NWB and NIfTI files into typed catalog tables. Nothing replaced it.
