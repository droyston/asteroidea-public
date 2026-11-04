# For researchers

This page is for a researcher who has been invited to the platform. It covers getting in, installing the
tools, and the calls you use day to day. You need a terminal on macOS or Linux and an email address your
administrator has invited.

## Getting an account

Your administrator invites you by email address. You receive a message titled "Your asteroidea account"
with the address of the hosted app, your username and a temporary password. The temporary password is
valid for 7 days. If it has expired, ask your administrator to send a new invitation.

## Signing in to the web app

Open the address in the invitation and sign in. You are asked to choose a new password the first time.
Nothing is shown before you sign in. After sign-in the app has four pages:

- **Data**: this platform's own datasets, then one dataset's subjects, sessions and files, then one file's header rows. It is read-only.
- **Discover**: the public DANDI and OpenNeuro datasets, to survey what exists.
- **My results**: the results saved with the SDK (see below), under **Mine** and **All**, with a download for each.
- **Get started**: the one setup command for the SDK and the notebook.

## Installing the SDK and notebook

Open **Get started**. It shows one command; press Copy and run it in a terminal:

```text
uvx --from 'https://<hosted app>/sdk/<wheel file>' asteroidea-setup https://<hosted app>
```

The wheel link in the command is public and does not expire; it carries no sign-in or credential.
You need `uv`; if it is missing, the page links to its install instructions. The command is safe to run
again. It:

1. fetches your connection settings from the hosted app and saves them to
   `~/.config/asteroidea/platform.json` (readable by you only); if a different file is already there, it
   stops, and running it again with `--force` replaces it;
2. signs you in: it prints a login address, you open it in any browser, sign in and paste the address the
   browser ends on (or its code) back; a sign-in already saved on this computer is reused;
3. creates a private Python 3.14 environment in `~/asteroidea-env`; `uv` fetches its own Python, so you need
   no other Python;
4. downloads the SDK, checks its checksum and installs it with the NWB and NIfTI readers and the notebook
   tools;
5. registers a notebook kernel in that environment;
6. downloads the exemplar notebook to `~/asteroidea-get-started/`, keeping any older copy as `.bak`.

It prints PASS or FAIL for each step, stops at the first FAIL, and appends the same lines to
`~/asteroidea-get-started/report.txt`. At the end it prints the command that opens the notebook. Run the same
command with `--pass2` added (and no address needed) for the second check, which asks you a few yes or no
questions about the web app and runs the SDK checks itself.

## Working with the SDK

Use the Python in `~/asteroidea-env`. The first call signs you in:

```python
import asteroidea

asteroidea.sign_in()
```

It prints a login address. Open it in any browser, sign in, and paste the address the browser ends on (or its
code) back into the terminal. Later runs reuse the saved sign-in.

| Call | What it does |
|---|---|
| `sign_in()` | Sign in once; later calls reuse it. |
| `datasets()` | One row per dataset, as an Arrow table. |
| `subjects(dataset=...)` | Subjects, optionally of one dataset. |
| `sessions(dataset=..., subject=...)` | Sessions, optionally of one dataset or subject. |
| `studies(tier=...)` | One row per study and dataset, with the dataset's tier; `tier` is `curated`, `upload` or `sandbox`. |
| `participants(study=..., has=[...])` | One row per participant (`subject_key`) with the datasets, subjects and modalities behind it; `has=["anat", "units"]` keeps participants with all listed. |
| `links(study=..., subject_key=...)` | The links that join subjects across datasets, with status, who linked, when and why. |
| `session_summary(dataset, subject, session)` | The session's stored summary, as the web app's session page draws it: `units` (with `location`), `events`, and per averaged event type `rates[type]` (every unit's rate and z per bin, the bin `edges`, the responsive screen). A read, not a computation; `None` when no summary is built yet. |
| `assets(dataset=..., subject=..., bids_session=..., file_format=...)` | Files joined to their headers. |
| `query(**filters)` | Catalog rows that match the filters, with the snapshot id; `.files` lists the files. |
| `sql("SELECT ...")` | SQL over the catalog views (DuckDB syntax); returns an Arrow table. |
| `open_nwb(asset_id)` | An NWB file, read lazily as you touch it. |
| `open_image(asset_id)` | A NIfTI image (`.nii` or `.nii.gz`), read lazily. |
| `open_file(asset_id)` | A read-only seekable file for any format; each read fetches only the bytes it needs. |
| `upload(path, dataset=..., sandbox=False)` | Upload one file and wait until it is in the catalog. |
| `upload_dataset(root, dataset=..., sandbox=False)` | Upload a folder, each file under its path relative to `root`. |
| `save_artifact(path, sources=..., snapshot_id=...)` | Save a file as a result with a record of its inputs. Every researcher can read it. |
| `artifacts(scope="mine", input_id=None)` | Saved results, newest first; see "Your results". |
| `download_artifact(artifact_id, dest)` | Save a result's file to `dest`; any researcher's finished result. |
| `labels(asset_id)` | The labels on a file (yours and others'; `mine` marks yours), as an Arrow table; read only. |

`participants()` names modalities as the web app does: `units`, `lfp`, `ieeg`, `anat`, `func` and `task`, and
`other:<name>` for any other kind of file. An NWB file counts as `units` when its header lists units, `task` when it
lists trials and `lfp` when it holds an LFP series. A link counts whatever its status, as `subjects()` does. Each
entry of `subjects` is `<dataset>/<subject>`, so the same label in two datasets stays distinct. It reads the same
two answers the web app's home page reads (the studies list and the subject links), so it takes seconds, and a
dataset's modalities are those of its last stored summary. It refuses with `QueryError` when more than 5000 links
match; pass `study=` to narrow it.

A short example: find files, read one, save a figure.

```python
found = asteroidea.query(dataset="dandi-000623")
nwb = asteroidea.open_nwb(
    found.files[0].asset_id
)  # lazy; only what you touch is fetched
asteroidea.save_artifact(
    "psth.png", sources=found.files[:1], snapshot_id=found.snapshot_id
)
```

The setup command already added the NWB and NIfTI readers, so there is nothing more to install.

## The exemplar notebook

The setup command puts `exemplar.ipynb` in `~/asteroidea-get-started/`. Open it with the command it printed,
which starts JupyterLab from `~/asteroidea-env`. It follows the web app's views: sign in, the studies, the
participants with both MRI and electrophysiology (and the links that join their labels), one session's
units by region, and that session's response heatmap. Each reads the catalog or a stored summary, so the
main path runs in about a minute or less. An optional last section, off by default, opens the raw NWB file.

## What you can upload

| File | What the platform does with it |
|---|---|
| NWB (`.nwb`) | Stored; its units, time series and header are extracted into the catalog. |
| NIfTI (`.nii`, `.nii.gz`) | Stored; its header is extracted. |
| BIDS JSON sidecars and `participants.tsv` | Stored and read into the catalog. |
| Anything else | Stored only. You can open it with `open_file`, but it adds no catalog rows. |

Upload de-identified data only: the platform stores what you send and does not scrub it for you.

Pass `sandbox=True` to `upload` or `upload_dataset` for a test dataset: the dataset is written with tier
`sandbox`, so it is kept apart from curated datasets and real uploads. The tier is set by the first file
that creates the dataset's row.

## Who owns a dataset

The first researcher to upload into a dataset name owns that name. Only the owner can add files to it;
another researcher who tries gets an error saying the dataset belongs to another researcher, and should upload under a different name. Datasets
imported by the administrator belong to the administrator. Everyone who is signed in can read every
dataset.

## Your results

A result saved with `save_artifact` or `Run.save_artifact` is readable by every signed-in researcher: anyone
can list it, open its record and download it, and each result shows its author's email address. Only the author
changes the name or note, withdraws it, or deletes it. Each result carries a provenance record: the catalog
files it was computed from, the snapshot of the catalog you queried, the time and your note. A result saved
inside a `run()` (see below) also records the run itself. Records are kept and do not expire.

| Call | What it does |
|---|---|
| `artifacts(scope="mine")` | Your results in every state, newest first, at most 100. |
| `artifacts(scope="all")` | The newest 100 finished results of every author, each with `author_email`. |
| `artifacts(input_id=<asset id or result id>)` | Only the results made from that file or result. |
| `open_artifact(artifact_id)` | The record of a finished result of any author. |
| `withdraw_artifact(artifact_id)` | Mark your result withdrawn. Its record and file stay readable. |

A result that another result used cannot be deleted; withdraw it instead (`HasDependentsError` names the
dependents). Another researcher's edit or withdraw raises `NotTheAuthorError`.

## Working with one session

A session is one recording of one subject: a dataset, a subject and a session label (`_` when the files carry
none). Every result of this part is an **exploratory screen**: it shows which units look responsive or tuned,
with raw p values and Benjamini-Hochberg q values, and it is not a finished analysis. Each result names the
analysis defaults and event recipe it used.

```python
import asteroidea

sess = asteroidea.load_session("dandi-000623", "sub-CS62", "ses-P62CSR2")
sess.units  # one row per unit, with its location
sess.events  # the resolved events (scene cuts, recognition onsets, movie onset)

psth = sess.rates("scene_cut")  # every unit, aligned to the type, with the screen
tune = sess.tuning("scene_cut", "cut_kind")  # one attribute of that type
power = sess.band_power("scene_cut")  # event-aligned band power from the LFP
```

| Call | What it does |
|---|---|
| `sessions_index()` | One row per session of the catalog. |
| `load_session(dataset, subject, session)` | A `Session`: `files`, `units`, `spikes`, `events`, `series`. Needs the `nwb` extra for `.lfp`. |
| `Session.lfp(series, channels=None, *, start_s, duration_s)` | A window of an LFP series on the trials clock. |
| `Session.rates(event_type, *, pre_s=None, post_s=None, bin_s=None)` | The PSTH of every unit (polars), plus the responsive screen. Omitted windows come from the dataset's analysis defaults. |
| `Session.tuning(event_type, attribute)` | The tuning screen of one attribute, one row per unit. Raises `ValueError` when fewer than 2 levels hold enough events. |
| `Session.band_power(event_type, series=None, channels=None, bands=None)` | Band power around each event, one row per (series, channel, band, step). Reads only each event's window. |
| `Session.to_pynapple(lfp=None)` | The session as pynapple objects (`units`, `events`, `lfp`). Needs `asteroidea[pynapple]`. |

Install the extras you use: `analysis` for the statistics, `nwb` for LFP, `pynapple` for `to_pynapple`.

To save a result with the record of everything it read, wrap the work in `run()`:

```python
with asteroidea.run("scene-cut rates", {"event": "scene_cut"}) as r:
    sess = asteroidea.load_session("dandi-000623", "sub-CS62", "ses-P62CSR2")
    psth = sess.rates("scene_cut")
    r.save_artifact("rates.csv", note="exploratory screen")
```

While the run is open, the SDK records every `sql()` table snapshot, every file opened with `open_file` or
`open_nwb`, every `query()`, every result opened or downloaded, and the defaults and recipe versions that
`rates`, `tuning` and `band_power` used. A `run()` inside a `run()` raises `NestedRunError`. The result's
page in the web app shows these as **Run** and **Inputs**, and the files it read list it under **Used by**.
The result is saved with your ID token, so the author email comes from your sign-in; if the platform refuses
it, `IdTokenRequiredError` says to sign in again.

## The session page in the web app

Under **Data**, a dataset lists its subjects, a subject lists its sessions, and a session opens its own page.
The page is labelled an exploratory screen and states the analysis defaults and baseline it used.

- **Units**: for every unit, a small firing-rate curve (PSTH) for each averaged event type, with the unit's
  location, p and q from the screen and whether it passed. A long unit list is paged by range.
- **Events**: the session's resolved events, listed by type.
- **Tuning**: choose an event type and an attribute; you get the selectivity index, the level means with the
  event count per level, p, q, whether the unit passed, and a line per brain area. A refusal (for example, too
  few events per level) shows the server's reason.
- **Files**: the session's files.
- **Results**: the results made from any of the session's files.

The server computes all of it with `asteroidea.analysis`, the same code the SDK calls. The movie onset is a
single trial, so it is drawn as one labelled trace and not averaged.

## Results pages

**My results** has two tabs, **Mine** and **All**. A result's page shows its name, the author's email, the
creation time, the size, the state, the note, and the panels **Run**, **Inputs** and **Used by**. Anyone can
download it. The author can edit the name and note and withdraw it. A file's page has a **Show results that
used this file** button. Withdrawn results stay listed, marked withdrawn. Because every researcher's email is
shown on a result, do not put anything in a result's name or note that you would not show every researcher.

## Exploring a file in the web app

Open **Data**, pick a dataset, then a subject, then a file (or a session, see above). Every page has an address you can bookmark or send; opening it signs you in first. A file page has tabs.

- **Units** (an NWB file with units): choose a unit and what to line up on: the recording's trials, the movie's scene cuts, a label, or all labels with one name. You get a spike raster and a firing-rate curve, optionally z-scored. The server computes it with the same function the SDK exposes as `asteroidea.analysis`.
- **LFP**: a whole-recording spectrogram first, then a window you choose from a channel; panning loads the next window.
- **Events**: the experiment's trials and events on a timeline, and a Labels lane. Drag in the lane to mark a period, name it and save. Everyone signed in sees your label; only you can change or delete it.
- **Image** (a NIfTI file): three orthogonal slices in a standard viewer that respects the scan's position and orientation. Files over 64 MiB compressed, 4D files and volumes over 512 MiB decompressed are refused with the reason.

In a notebook, `asteroidea.labels("<asset id>")` returns the same labels as an Arrow table, and `asteroidea.analysis` (install the `analysis` extra) holds the rate, z-score and spectrogram functions.

A curator also sees **Imports**: start an import of up to 50 files from DANDI or OpenNeuro and watch each file land.

## If something goes wrong

- The temporary password has expired, or the invitation email never arrived: ask your administrator to
  resend the invitation.
- `FAIL` in the setup command: read the line, fix the cause it names and run the command again.
- The SDK says you are not signed in: call `asteroidea.sign_in()` again.
