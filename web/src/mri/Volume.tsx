// One NIfTI volume in NiiVue: the Image tab of a NIfTI file, and (compact) the
// anatomy panel of the signals view, three planes and a readout cell. The volume
// comes from the presign route and is fetched once, whole, by a plain `fetch` (no credentials, progress
// from Content-Length); the bytes go to NiiVue, which is loaded with `import()` only when this mounts. Refusals (4D, over 64 MiB compressed,
// over 512 MiB decompressed) are decided from the catalog header before anything is downloaded. The
// crosshair is in the URL (`mm=x,y,z`), so a link restores the view.
import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import type { AuthStore } from "../auth/store";
import { loadDownload } from "../catalog/file";
import { doFetch } from "../catalog/loadable";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import { parseHex } from "../plots/heatmap";
import { useSearch } from "../research/shared";
import { loadChunk, StaleChunkNotice } from "../router/lazy";
import { appRouter } from "../router/router";
import { themeStore } from "../shell/theme";
import {
  fetchVolume,
  formatIntensity,
  fromWindowLevel,
  mmSearch,
  type Progress,
  parseMm,
  refusal,
  toWindowLevel,
  type VolumeHeader,
} from "./mri";
import type { Layout, Readout, Viewer } from "./viewer";

/** What the e2e probe reads: the NiiVue instance, hung on the canvas like `astData` elsewhere. */
export interface MriCanvas extends HTMLCanvasElement {
  astMri?: unknown;
}

const LAYOUTS: readonly { id: Layout; label: string }[] = [
  { id: "grid", label: "Grid" },
  { id: "row", label: "Row" },
  { id: "column", label: "Column" },
];

const KEYS: Record<string, [number, number, number]> = {
  ArrowRight: [1, 0, 0],
  ArrowLeft: [-1, 0, 0],
  ArrowUp: [0, 1, 0],
  ArrowDown: [0, -1, 0],
  PageUp: [0, 0, 1],
  PageDown: [0, 0, -1],
};

function plainFetch(url: string, init: RequestInit): Promise<Response> {
  return fetch(url, init);
}

function token(name: string, fallback: string): number[] {
  const rgb = parseHex(getComputedStyle(document.documentElement).getPropertyValue(name)) ??
    parseHex(fallback) ?? [0, 0, 0];
  return [rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1];
}

function fileName(assetKey: string): string {
  return assetKey.endsWith(".nii") || assetKey.endsWith(".nii.gz")
    ? assetKey
    : `${assetKey}.nii.gz`;
}

function num(n: number): string {
  return n.toFixed(2);
}

export function Volume({
  auth,
  assetId,
  header,
  sizeBytes,
  s3Key,
  compact = false,
}: {
  auth: AuthStore;
  assetId: string;
  header: VolumeHeader;
  sizeBytes: number | null;
  s3Key: string;
  /** Three planes and a readout cell, window and level only (the signals view's anatomy panel). */
  compact?: boolean;
}) {
  const refused = refusal(header, sizeBytes);
  const search = useSearch();
  const searchRef = useRef(search);
  searchRef.current = search;
  const canvas = useRef<MriCanvas>(null);
  const viewer = useRef<Viewer | null>(null);
  const [note, setNote] = useState<"stale" | string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [ready, setReady] = useState(false);
  const [readout, setReadout] = useState<Readout | null>(null);
  const [layout, setLayout] = useState<Layout>("grid");
  const [range, setRange] = useState<{ min: number; max: number } | null>(null);

  // The position is written to the URL on every move; the first read (deep link) is taken once at load.
  const onMove = useCallback((r: Readout) => {
    setReadout(r);
    appRouter().navigate(`${window.location.pathname}${mmSearch(searchRef.current, r.mm)}`, {
      replace: true,
    });
  }, []);

  useEffect(() => {
    if (refused !== null) return;
    const ctl = new AbortController();
    let live = true;
    void (async () => {
      const dl = await loadDownload(auth.config, assetId, auth, doFetch);
      if (!live) return;
      if (dl.kind === "ended") return auth.expire();
      if (dl.kind !== "loaded") return setNote(dl.kind === "error" ? dl.message : "not found");
      setProgress({ loaded: 0, total: dl.value.size_bytes });
      const got = await fetchVolume(
        dl.value.url,
        plainFetch,
        (p) => live && setProgress(p),
        ctl.signal,
      );
      if (!live) return;
      if (got.kind !== "loaded") return setNote(got.message);
      const chunk = await loadChunk(() => import("./viewer"));
      if (!live || canvas.current === null) return;
      if (chunk.kind === "stale") return setNote("stale");
      try {
        const link = parseMm(searchRef.current);
        const v = await chunk.module.openViewer(
          canvas.current,
          got.buffer,
          fileName(s3Key),
          onMove,
          {
            crosshair: token("--color-accent", "#0969da"),
            back: token("--color-surface", "#ffffff"),
          },
        );
        if (!live) return v.dispose();
        viewer.current = v;
        canvas.current.astMri = v.nv;
        setRange({ ...v.initial });
        setReady(true);
        setProgress(null);
        if (link !== null) v.setMm(link);
        else setReadout(v.readout());
      } catch (err) {
        setNote(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      live = false;
      ctl.abort();
      viewer.current?.dispose();
      viewer.current = null;
    };
  }, [auth, assetId, refused, s3Key, onMove]);

  // The viewer took its colours from the tokens when it opened; a theme change recolours it in place.
  useEffect(() => {
    if (!ready) return;
    return themeStore().subscribe(() => {
      const v = viewer.current;
      if (v === null) return;
      v.nv.setCrosshairColor(token("--color-accent", "#0969da"));
      v.nv.opts.backColor = token("--color-surface", "#ffffff");
      v.nv.drawScene();
    });
  }, [ready]);

  const onKey = (e: KeyboardEvent) => {
    const step = KEYS[e.key];
    if (step === undefined || viewer.current === null) return;
    e.preventDefault();
    e.stopPropagation();
    viewer.current.nudge(...step);
  };
  const full = viewer.current?.full ?? { min: 0, max: 1 };
  const wl = range === null ? null : toWindowLevel(range.min, range.max);
  const span = Math.max(full.max - full.min, 1e-6);
  const setWl = (level: number, window: number) => {
    const [min, max] = fromWindowLevel(level, window, span / 1000);
    setRange({ min, max });
    viewer.current?.setRange(min, max);
  };
  const pct = progress?.total
    ? Math.min(100, Math.round((progress.loaded / progress.total) * 100))
    : null;

  const readoutView = (
    <p className="mri-readout" data-testid="mri-readout" aria-live="polite" hidden={!ready}>
      {readout === null ? null : (
        <>
          <span>
            World (mm) <b data-testid="mri-mm">{readout.mm.map(num).join(", ")}</b>
          </span>
          <span>
            Voxel <b data-testid="mri-vox">{readout.vox.join(", ")}</b>
          </span>
          <span>
            Intensity{" "}
            <b data-testid="mri-value">
              {readout.value === null ? "—" : formatIntensity(readout.value)}
            </b>
          </span>
        </>
      )}
    </p>
  );
  if (refused !== null) {
    return (
      <Notice kind="info" testId="file-image-refused">
        {refused}
      </Notice>
    );
  }
  return (
    <div className={compact ? "mri mri-compact" : "mri"} data-testid="mri">
      {note === "stale" ? <StaleChunkNotice /> : null}
      {note !== null && note !== "stale" ? (
        <Notice kind="error" testId="file-image-note">
          {note}
        </Notice>
      ) : null}
      {progress !== null && note === null ? (
        <div className="mri-progress" data-testid="mri-progress">
          <progress
            value={progress.total ? progress.loaded : undefined}
            max={progress.total ?? undefined}
            aria-label="Downloading the volume"
          />
          <span>{pct === null ? "Downloading…" : `Downloading… ${pct}%`}</span>
        </div>
      ) : null}
      <div className="mri-controls" hidden={!ready}>
        {compact ? null : (
          <fieldset className="mri-layout">
            <legend>Layout</legend>
            {LAYOUTS.map((l) => (
              <Button
                key={l.id}
                variant={layout === l.id ? "primary" : "secondary"}
                aria-pressed={layout === l.id}
                onClick={() => {
                  setLayout(l.id);
                  viewer.current?.setLayout(l.id);
                }}
                data-testid={`mri-layout-${l.id}`}
              >
                {l.label}
              </Button>
            ))}
          </fieldset>
        )}
        <label>
          Level
          <input
            type="range"
            min={full.min}
            max={full.max}
            step={span / 500}
            value={wl?.level ?? 0}
            onChange={(e) => wl !== null && setWl(Number(e.target.value), wl.window)}
            data-testid="mri-level"
          />
        </label>
        <label>
          Window
          <input
            type="range"
            min={span / 1000}
            max={span * 2}
            step={span / 500}
            value={wl?.window ?? 0}
            onChange={(e) => wl !== null && setWl(wl.level, Number(e.target.value))}
            data-testid="mri-window"
          />
        </label>
        <Button
          onClick={() => {
            viewer.current?.reset();
            setLayout("grid");
            setRange({ ...(viewer.current?.initial ?? { min: 0, max: 1 }) });
          }}
          data-testid="mri-reset"
        >
          Reset view
        </Button>
      </div>
      {compact ? null : readoutView}
      <div className="mri-stage" onKeyDownCapture={onKey}>
        <canvas
          ref={canvas}
          className="mri-canvas"
          tabIndex={0}
          aria-label="MRI slices. Arrow keys move the crosshair in the slice, Page Up and Page Down move it between slices."
          data-testid="mri-canvas"
        />
        {compact ? readoutView : null}
      </div>
    </div>
  );
}
