## Acceptance rows

A tester on the hosted app; each row starts from the previous row's end. Outcomes are estimates unless an offline
test covers them.

| Row | Control or op | Start state | Refusal rules | Outcome from that start | Budget |
|---|---|---|---|---|---|
| R0 | Open the hosted URL; managed login | signed out | nothing renders before a token | `/` in the new shell | sign-in at most 5 s |
| R1 | Nav "Catalog" | R0 | 401; 403; 502; 503 | datasets incl. `dandi-000623` | p50 2.33 s, max 3.16 s |
| R2 | `dandi-000623` | R1 | 400; 404 | subjects CS41, CS62, sessions, formats | at most 4 s |
| R3 | sub-CS62, the R2 NWB file | R2 | 404 | header, 96 electrodes, 44 units, 41 trials | at most 4 s a tab |
| R4 | Units tab: unit 0, `intervals:trials`, -2 s to +2 s, 50 ms, z-score | R3, after `reextract` | 400; 404; 413; 503 | raster of 41 trials, rate and z curves, `source: catalog` | warm p50 at most 4 s, max 8 s; body at most 5 MB |
| R5 | Same tab, align "scene cuts" (`frames:<scenecut_info.csv asset>:shot_start_fr:/stimulus/movieframe_time`), -2 s to +2 s, 50 ms, z-score | R4 | as R4; 404 CSV or series; 422 series without timestamps | 93 cuts; rates equal `make analysis`'s scene-cut step for the unit | warm max 10 s |
| R6 | Events tab | R5 | 404 | 41 trials on the timeline | at most 4 s |
| R7 | LFP tab: `LFP_micro`, channel 0, start 0, window = the longest the route allows; pan once | R6 | 400; 422; 413; 503 | heatmap and trace twice | warm at most 15 s each; cold at most 25 s |
| R8 | Copy R7's URL, open in a new tab | R7 | sign-in first | same tab, channel and window | R0 + R7 |
| R9 | The ds004473 sub-1 T1w (imported at the deploy), Image tab | R8 | 404; 64 MB, 512 MB, 4D | three planes, crosshair in world mm | presign at most 3 s; 10,740,856 B at most 5 s |
| R10 | Events tab of the NWB file: drag, name "baseline", save; reload | R9 | 400; 409; 403 | label persists, "yours" | POST at most 3 s |
| R11 | Second researcher (sandbox-test, labelled) opens it | R10 | as R10 | sees it; edit refused with "only its author can change this" | as R10 |
| R12 | Units tab aligned to `label_name:baseline` | R11 | as R4 | rates around each "baseline" start | as R4 |
| R13 | Notebook: `asteroidea.labels(<id>)`, `analysis.rates(...)` on `unit_spikes` from `sql()` | R12 | SDK errors as `artifacts()` | the label; rates equal R4's for the same inputs | at most 5 s |
| R14 | After `make curator`: sign out, sign in, Imports page, one small not-yet-imported file (about 11 MB, estimated) | signed in with a token issued after the group change | 403 (a pre-change token gets this); 400; run REFUSED (no title or licence, second version, over 50 files, nothing selected); ImportFunction refusals | run DONE, 1 PASS | plan at most 60 s; file EXTRACTED at most 10 min |
