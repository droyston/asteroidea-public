# Harvest fixtures

Recorded pairs for the harvester tests. Each `<key>.json` holds one request (`method`, `url`, and for
POST the `request.json` body) and its ordered `responses`. The replay transport serves the n-th call
for a request with the n-th response, then repeats the last one. `<key>` is the first 16 hex digits of
the SHA-256 of `METHOD url canonical-body` (`asteroidea_services.http.request_key`); a change to the
GraphQL query text in `harvest/openneuro.py` changes the OpenNeuro keys, and the replay miss names the
request. `expected-index.json` is the golden index (see the end).

Recorded with `python -m asteroidea_services.harvest --record DIR` against the live public
APIs (read-only GET and GraphQL POST), then trimmed with a one-off script. Every edit follows. Nothing
else was changed, apart from the two redacted contact addresses under DANDI: the kept DANDI
`info/` bodies and the kept OpenNeuro pages are verbatim.

## DANDI

- Listing (`GET .../dandisets/?page_size=1000&draft=false&empty=false`): `results` trimmed from 464
  entries to 5 (000003, 000004, 000016, 000029, 000576); `count` rewritten 464 to 5; `next` is null as
  recorded.
- `info/` for 000003, 000016, 000029, 000576: verbatim recorded 200 responses. Why these four: 000576
  is the human exemplar (Units, LFP, ElectricalSeries); 000003 is a mouse set whose species label is
  `House mouse` (not the long form); 000029 has three species (macaque, mouse, rat) and Units only;
  000016 has no `assetsSummary.species` and microscopy plus behavioral approaches.
- SYNTHESIZED, not recorded: the `info/` for 000004 returns HTTP 500 (body `Internal Server Error`)
  three times, so the harvester reports it in `failed[]` with 3 attempts. The real error seen in an
  earlier run was not captured, so its type is unknown.
- SYNTHESIZED, not recorded: the `info/` for 000016 returns HTTP 503 (body `Service Unavailable`)
  once, then the recorded 200, so the harvester retries and the record is ok.
- 000004's listing entry is the real one; only its `info/` is synthesized.
- REDACTED, not recorded: every personal contact address in the recorded bodies became a distinct
  `contact.<word>@example.invalid` address (organization role addresses are kept). The harvester reads no email field, so
  `expected-index.json` is unchanged.

## OpenNeuro

The full pagination was recorded (39 pages, 1927 edges). Kept verbatim, in this order:

1. the first page (offset 0, 50 edges; `metadata.species` is null on all 50);
2. the first page that contains a `null` edge (recorded request offset 1150, 50 edges: 49 datasets and
   one `null` edge; species values `Human`, null, `""`, `Mouse`, `baboon`, `Other`). This is the real
   production shape of a null node: the whole edge is `null`. The recording held 6 null edges in total;
3. the real terminal page (recorded request offset 1900, 27 edges, `hasNextPage` false).

Edits: on each kept page `pageInfo.count` rewritten to 127 (50 + 50 + 27); `pageInfo.endCursor`
rewritten to the base64 of `{"offset":50}`, `{"offset":100}` and `{"offset":127}` so the cursors chain
(page 2 is requested with `after` = page 1's `endCursor`, page 3 with page 2's). Edge `cursor` fields
inside `edges` are as recorded. No node was edited; the three species cases (null, `""`, `"Human"`) all
occur naturally in the kept pages, so no labelled edit to a node was needed, and the recording did show
real null nodes, so none was synthesized.

## Golden index

`expected-index.json` is the harvester's own output over these files:

    python -m asteroidea_services.harvest --replay services/tests/fixtures/harvest \
      --out services/tests/fixtures/harvest/expected-index.json --now 2026-10-23T12:00:00Z

`--now` freezes the wall clock and makes every `seconds` field 0.0. It was checked by hand
against the raw `info/` fixtures and the raw OpenNeuro pages; the golden test then catches
regressions against that output only.

## Schema /2

`expected-index.json` was regenerated with `--replay` after the schema moved to `asteroidea.dataset-index/2`.
The only differences from the /1 file: the `schema` string, a `version_modified` key on every record (the
DANDI listing's `modified` for the published version; null for OpenNeuro), and `"stale": false` on the one
failure entry. No recorded request changed.

