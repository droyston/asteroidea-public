// Signal inspection: the real `SignalsView` over a SYNTHETIC file,
// parsed by the view's own parsers. The live catalog fixture holds no LFP overview, so this surface
// builds one deterministically, shaped like CS41 R1: `LFP_macro` 96 channels and `LFP_micro` 80, both
// at 1000 Hz, 64 log bands 1-250 Hz at 1 s steps (60 s here, to keep the inline body small).
//   signals            the 96-channel series with an electrodes table of 96 rows, 6 regions x 16:
//                      channels are grouped and coloured by region.
//   signals-unmapped   the 80-channel series with the file's 176 electrodes rows: the catalog does not
//                      say which electrodes the series' channels are, so one "region not known" group.
// Expectations are derived here from the synthetic tables, not from the view's model.
import { createElement, Fragment, type ReactElement } from "react";
import type { Row } from "../../catalog/catalog";
import { parseLabels } from "../../events/labels";
import { axisLabels, computeLanes, parseLaneEvents } from "../../events/lanes";
import { lfpSeries, parseSpectrogram } from "../../lfp/lfp";
import { parseOverviewJson } from "../../lfp/overview";
import { volumeHeader } from "../../mri/mri";
import { type AnatomyState, AnatomyView } from "../../signals/Anatomy";
import { LabelsList } from "../../signals/LabelsList";
import { channelLayout, parseBand } from "../../signals/model";
import { SignalsView } from "../../signals/SignalsView";
import { ab, type Expectation, type Surface } from "../harness";

const ASSET = "asset-cs41-r1";
const N_TIMES = 60;
const N_BANDS = 64;
const WIN = { start: 20, dur: 20 };
const REGIONS = [
  "amygdala left",
  "amygdala right",
  "hippocampus left",
  "hippocampus right",
  "ACC",
  "vmPFC",
] as const;
const MACRO = { path: "/acquisition/LFP_macro", channels: 96 };
const MICRO = { path: "/acquisition/LFP_micro", channels: 80 };

const ALLOW = [
  "aside",
  "figure",
  "figcaption",
  "strong",
  "title",
  "section",
  "div",
  "label",
  "select",
  "option",
  "p",
  "h3",
  "ul",
  "li",
  "span",
  "button",
  "canvas",
  "svg",
  "g",
  "rect",
  "line",
  "text",
];

function round2(v: number): number {
  return Math.round(v * 100) / 100;
}

const EDGES = Array.from({ length: N_BANDS + 1 }, (_v, i) =>
  Number((250 ** (i / N_BANDS)).toPrecision(6)),
);

const SERIES_BODY = {
  rows: [MACRO, MICRO].map((s) => ({
    asset_id: ASSET,
    path: s.path,
    neurodata_type: "ElectricalSeries",
    shape: [763964, s.channels],
    dtype: "int16",
    rate_hz: 1000,
    starting_time_s: 0,
    has_timestamps: false,
    unit: "volts",
  })),
  page: 0,
  more: false,
};

/** 96 electrodes, 6 regions x 16, in region order. */
const ELECTRODES_96: Row[] = Array.from({ length: 96 }, (_v, i) => ({
  asset_id: ASSET,
  electrode_index: i,
  electrode_id: i,
  location: REGIONS[Math.floor(i / 16)] ?? null,
  group_name: "macro",
}));

/** 176 electrodes (80 + 96): more rows than the micro series has channels. */
const ELECTRODES_176: Row[] = Array.from({ length: 176 }, (_v, i) => ({
  asset_id: ASSET,
  electrode_index: i,
  electrode_id: i,
  location: REGIONS[Math.floor(i / 30) % REGIONS.length] ?? null,
  group_name: i < 80 ? "micro" : "macro",
}));

/** The inline overview body: a 1/f slope, a slow wave per channel and a theta burst at 24-30 s. */
function overviewBody(channels: number): Record<string, unknown> {
  const centres = EDGES.slice(0, -1).map((lo, b) => Math.sqrt(lo * (EDGES[b + 1] ?? lo)));
  const power = Array.from({ length: channels }, (_c, c) =>
    Array.from({ length: N_TIMES }, (_t, t) =>
      centres.map((f) => {
        const burst = t >= 24 && t < 30 && f >= 4 && f <= 8 ? 12 : 0;
        return round2(-40 - 20 * Math.log10(f) + 6 * Math.sin(t / 7 + c / 5) + burst);
      }),
    ),
  );
  return {
    times_s: Array.from({ length: N_TIMES }, (_v, t) => t + 0.5),
    band_edges_hz: EDGES,
    channels: Array.from({ length: channels }, (_v, i) => i),
    power_db: power,
    rate_hz: 1000,
    starting_time_s: 0,
  };
}

function spectrogramBody(): Record<string, unknown> {
  const freqs = Array.from({ length: 251 }, (_v, f) => f);
  const times = Array.from({ length: 39 }, (_v, i) => WIN.start + 0.5 + i * 0.5);
  const traceTimes = Array.from({ length: 400 }, (_v, i) => WIN.start + (i * WIN.dur) / 400);
  return {
    freqs_hz: freqs,
    times_s: times,
    power_db: freqs.map((f) =>
      times.map((t) => {
        const burst = t >= 24 && t < 30 && f >= 4 && f <= 8 ? 12 : 0;
        return round2(-40 - 20 * Math.log10(Math.max(1, f)) + 3 * Math.sin(t) + burst);
      }),
    ),
    trace: {
      times_s: traceTimes,
      values: traceTimes.map((t) => round2(50 * Math.sin(t * 6) + 20 * Math.sin(t * 41))),
    },
    rate_hz: 1000,
    starting_time_s: 0,
    bytes_read: 2400000,
    requests: 12,
    compute_ms: 140,
  };
}

// --- event lanes, labels, anatomy ---------------------------------------------------------

/** Scene cuts every 7 s, trials of 3 s every 10 s, and 3,000 frames (closer than a pixel: binned). */
const N_FRAMES = 3000;
const EVENTS_BODY = {
  asset_id: ASSET,
  dataset: "dandi-000623",
  recipe: { version: "synthetic", default: false, lfp_clock_offset_s: 0 },
  event_types: [
    {
      name: "scene_cut",
      averaging: "average",
      n_events: 8,
      events: Array.from({ length: 8 }, (_v, i) => ({
        onset_s: 3 + i * 7,
        stop_s: null,
        attributes: {},
        source: { kind: "synthetic", ref: "scene_cut", row: i },
      })),
    },
    {
      name: "trial",
      averaging: "single",
      n_events: 6,
      events: Array.from({ length: 6 }, (_v, i) => ({
        onset_s: 2 + i * 10,
        stop_s: 5 + i * 10,
        attributes: {},
        source: { kind: "synthetic", ref: "trials", row: i },
      })),
    },
    {
      name: "frame",
      averaging: "single",
      n_events: N_FRAMES,
      events: Array.from({ length: N_FRAMES }, (_v, i) => ({
        onset_s: (i * 60) / N_FRAMES,
        stop_s: null,
        attributes: {},
        source: { kind: "synthetic", ref: "frames", row: i },
      })),
    },
  ],
};
const N_LANES = EVENTS_BODY.event_types.length;

/** 14 labels, 3 of them yours, spread over the 60 s. */
const N_LABELS = 14;
const LABELS_BODY = {
  labels: Array.from({ length: N_LABELS }, (_v, i) => ({
    label_id: `label-${String(i).padStart(2, "0")}`,
    name: i === 6 ? "theta burst" : i % 2 === 0 ? "burst" : "artifact",
    note: "",
    start_s: 1 + i * 4,
    stop_s: 3 + i * 4,
    mine: i % 5 === 1,
    version: 1,
  })),
};
const N_MINE = LABELS_BODY.labels.filter((l) => l.mine).length;

/**
 * This person's T1w. The live fixture's sub-p41cs has no MRI session, so the `signals` variant files the
 * synthetic recording under sub-CS62, whose pending link reaches openneuro-ds004798 sub-p62cs and its one
 * NIfTI file, asset-p62cs-t1w. `signals-unmapped` stays under sub-CS41: no T1w.
 */
const T1W_ID = "asset-p62cs-t1w";
const T1W_ASSET = {
  asset: {
    asset_id: T1W_ID,
    dataset: "openneuro-ds004798",
    subject: "sub-p62cs",
    bids_session: "ses-mri",
    s3_key: "raw/openneuro-ds004798/sub-p62cs/ses-mri/anat/sub-p62cs_ses-mri_T1w.nii.gz",
    datatype: "anat",
    format: "nifti",
    size_bytes: 10_400_000,
    s3_version_id: "synthetic",
  },
  headers: [{ asset_id: T1W_ID, nifti_shape: [176, 256, 256], nifti_datatype: "int16" }],
};

interface Variant {
  id: string;
  series: { path: string; channels: number };
  electrodes: Row[];
  subject: string;
  t1w: boolean;
}

const VARIANTS: readonly Variant[] = [
  { id: "signals", series: MACRO, electrodes: ELECTRODES_96, subject: "sub-CS62", t1w: true },
  {
    id: "signals-unmapped",
    series: MICRO,
    electrodes: ELECTRODES_176,
    subject: "sub-CS41",
    t1w: false,
  },
];

const PLOT_W = 740;

function render(v: Variant): ReactElement {
  const overview = parseOverviewJson(overviewBody(v.series.channels));
  const noop = () => undefined;
  const clock = { offset: EVENTS_BODY.recipe.lfp_clock_offset_s, startingTime: 0 };
  const labels = axisLabels(parseLabels(LABELS_BODY), clock);
  const anatomy: AnatomyState = v.t1w
    ? {
        kind: "found",
        file: {
          assetId: T1W_ID,
          dataset: T1W_ASSET.asset.dataset,
          s3Key: T1W_ASSET.asset.s3_key,
          sizeBytes: T1W_ASSET.asset.size_bytes,
          header: volumeHeader(T1W_ASSET.headers[0]),
          pending: true,
        },
      }
    : { kind: "none" };
  const aside = createElement(
    Fragment,
    null,
    createElement(AnatomyView, { state: anatomy, onOpen: noop, viewer: null }),
    createElement(LabelsList, {
      labels,
      around: [WIN.start, WIN.start + WIN.dur],
      selectedId: null,
      loading: false,
      error: null,
      onRetry: noop,
      onPick: noop,
    }),
  );
  return createElement(SignalsView, {
    list: lfpSeries(SERIES_BODY.rows),
    chosen: v.series.path,
    band: parseBand(""),
    overview: { kind: "loaded", overview },
    layout: channelLayout(overview.nChannels, v.electrodes),
    channel: 0,
    win: WIN,
    spectrogram: { kind: "loaded", body: parseSpectrogram(spectrogramBody()) },
    width: PLOT_W,
    onSeries: noop,
    onBand: noop,
    onChannel: noop,
    onWindow: noop,
    lanes: { kind: "loaded", lanes: computeLanes(parseLaneEvents(EVENTS_BODY), clock) },
    labels: {
      labels,
      selectedId: null,
      draft: null,
      blocked: null,
      busy: false,
      refusal: null,
      onDraft: noop,
      onSelect: noop,
      onCancel: noop,
      onCreate: noop,
      onChange: noop,
      onRemove: noop,
      onReload: noop,
    },
    aside,
  });
}

function expectation(v: Variant): Expectation {
  const matched = v.electrodes.length === v.series.channels;
  const regions = matched
    ? [...new Set(v.electrodes.map((r) => String(r.location)))]
    : ["region not known"];
  return {
    allow: ALLOW,
    counts: [
      { noun: "region groups", n: regions.length, match: ab("region-group") },
      {
        noun: "region not known groups",
        n: matched ? 0 : 1,
        match: ab("region-group", { "data-ab-known": "false" }),
      },
      { noun: "series options", n: SERIES_BODY.rows.length, match: ab("series-option") },
      { noun: "channel options", n: v.series.channels, match: ab("channel-option") },
      { noun: "window brushes", n: 1, match: ab("brush") },
      { noun: "event lanes", n: N_LANES, match: ab("event-lane") },
      { noun: "label ticks under the overview", n: N_LABELS, match: ab("label-tick") },
      {
        noun: "labels drawn in the window",
        n: LABELS_BODY.labels.filter(
          (l) => l.stop_s >= WIN.start && l.start_s <= WIN.start + WIN.dur,
        ).length,
        match: ab("label-bar"),
      },
      { noun: "labels listed", n: 3, match: ab("label-row") },
      { noun: "anatomy planes", n: v.t1w ? 3 : 0, match: ab("anatomy-plane") },
    ],
    texts: [
      ...regions,
      `all ${v.series.channels} channels`,
      v.series.path,
      `Window ${WIN.start}–${WIN.start + WIN.dur} s`,
      ...(matched ? [] : ["does not say which electrodes"]),
      "Scene cut",
      "Trial",
      "Frame",
      "Labels on this file",
      `${N_LABELS} · ${N_MINE} yours`,
      `${N_LABELS - 3} more across the recording`,
      ...(v.t1w ? ["T1w from openneuro-ds004798", "link pending"] : ["holds no T1w"]),
    ],
    // One row per channel, one per log band of the window, one trace.
    canvasRows: v.series.channels + N_BANDS + 1,
  };
}

export const signalsSurfaces: readonly Surface[] = VARIANTS.map((v) => ({
  id: v.id,
  board: "07-signal-inspection",
  title: "Signal inspection",
  pending: null,
  route: `/files/${ASSET}?tab=lfp&series=${encodeURIComponent(v.series.path)}&start=${WIN.start}&dur=${WIN.dur}`,
  ready: "file",
  viewports: ["1440x900"],
  noScroll: false,
  render: () => render(v),
  expect: () => expectation(v),
  api: {
    [`/catalog/assets/${ASSET}`]: {
      asset: {
        asset_id: ASSET,
        dataset: "dandi-000623",
        subject: v.subject,
        bids_session: "ses-1",
        s3_key: "raw/dandi-000623/sub-CS41/sub-CS41_ses-1_ecephys.nwb",
        datatype: "ecephys",
        format: "nwb",
        size_bytes: 2000000000,
        s3_version_id: "synthetic",
      },
      headers: [{ asset_id: ASSET, format: "nwb", nwb_identifier: "CS41-ses-1 (synthetic)" }],
    },
    [`/catalog/assets/${ASSET}/tables/series`]: SERIES_BODY,
    [`/catalog/assets/${ASSET}/tables/electrodes`]: { rows: v.electrodes, page: 0, more: false },
    "/compute/overview": overviewBody(v.series.channels),
    "/compute/spectrogram": spectrogramBody(),
    "/compute/events": EVENTS_BODY,
    [`/files/${ASSET}/labels`]: LABELS_BODY,
    ...(v.t1w ? { [`/catalog/assets/${T1W_ID}`]: T1W_ASSET } : {}),
  },
}));
