// The file page: one asset's header, a Download button and format tabs. NWB files get
// Overview, Units, Signals (the LFP, event lanes, labels and anatomy of the signals view), Electrodes and Series;
// NIfTI files Overview and Image; a BIDS events.tsv Overview and Events (its lanes and labels). The tab lives in the URL (`?tab=`). Download asks `/download` for a presigned
// URL and navigates the page to it; the app never fetches the bytes.
import { useCallback, useState } from "react";
import type { AuthStore } from "../auth/store";
import { type AssetBody, loadAsset, type Row } from "../catalog/catalog";
import { subjectPath } from "../catalog/depth";
import { loadDownload, loadTable, type TableName } from "../catalog/file";
import { doFetch } from "../catalog/loadable";
import { Button } from "../kit/Button";
import { KeyValue } from "../kit/KeyValue";
import { Notice } from "../kit/Notice";
import { type Column, Table } from "../kit/Table";
import { type TabItem, Tabs } from "../kit/Tabs";
import { volumeHeader } from "../mri/mri";
import { Volume } from "../mri/Volume";
import { UsedBy } from "../results/UsedBy";
import { dataCrumbs, datasetViewPath } from "../router/dataPaths";
import { Ready, Spine } from "../shell/marks";
import { SignalsLoader } from "../signals/SignalsLoader";
import { LoadState } from "../states/LoadState";
import { formatBytes } from "../table/facets";
import { UnitsTab } from "../units/UnitsTab";
import { useLoad } from "../useLoad";
import { none, toTrail, useTab } from "./shared";

type Kind = "nwb" | "nifti" | "events" | "other";

const TABS: Record<Kind, readonly TabItem[]> = {
  nwb: [
    { id: "overview", label: "Overview" },
    { id: "units", label: "Units" },
    { id: "lfp", label: "Signals" },
    { id: "electrodes", label: "Electrodes" },
    { id: "series", label: "Series" },
  ],
  nifti: [
    { id: "overview", label: "Overview" },
    { id: "image", label: "Image" },
  ],
  events: [
    { id: "overview", label: "Overview" },
    { id: "events", label: "Events" },
  ],
  other: [{ id: "overview", label: "Overview" }],
};

function text(v: unknown): string | null {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

/** The tab set for an asset: by `format`, with a BIDS events file recognised by its name. */
export function kindOf(asset: Row): Kind {
  const format = text(asset.format);
  const key = text(asset.s3_key) ?? "";
  if (format === "nwb") return "nwb";
  if (format === "nifti") return "nifti";
  if (format === "events" || key.endsWith("_events.tsv")) return "events";
  return "other";
}

function relativePath(asset: Row): string {
  const key = text(asset.s3_key) ?? "";
  const dataset = text(asset.dataset) ?? "";
  return key.startsWith(`raw/${dataset}/`) ? key.slice(`raw/${dataset}/`.length) : key;
}

const HIDDEN = new Set(["asset_id", "dataset", "subject", "bids_session", "events_for"]);

function cellView(v: unknown): string {
  return text(v) ?? "—";
}

function columnsFor(rows: readonly Row[]): Column<Row>[] {
  const keys: string[] = [];
  for (const row of rows) {
    for (const k of Object.keys(row)) if (!HIDDEN.has(k) && !keys.includes(k)) keys.push(k);
  }
  return keys.map((k) => ({
    key: k,
    header: k,
    numeric: rows.some((r) => typeof r[k] === "number"),
    render: (r: Row) => cellView(r[k]),
  }));
}

function Overview({ body }: { body: AssetBody }) {
  const header = body.headers[0] ?? {};
  const items = Object.entries(header)
    .filter(([k, v]) => k !== "asset_id" && text(v) !== null)
    .map(([k, v]) => ({ label: k, value: text(v) }));
  if (items.length === 0) {
    return <Notice kind="info">No header was extracted from this file.</Notice>;
  }
  return <KeyValue testId="file-overview" items={items} />;
}

function TableTab({
  auth,
  assetId,
  table,
}: {
  auth: AuthStore;
  assetId: string;
  table: TableName;
}) {
  const [page, setPage] = useState(0);
  const run = useCallback(
    () => loadTable(auth.config, assetId, table, page, auth, doFetch),
    [auth, assetId, table, page],
  );
  const { state, reload } = useLoad(auth, run);
  return (
    <LoadState
      state={state}
      noun={table}
      path={`/catalog/assets/${assetId}/tables/${table}`}
      onRetry={reload}
    >
      {(body) => (
        <>
          <Table
            label={table}
            testId={`file-${table}`}
            rows={body.rows}
            rowKey={(_r, i) => i}
            empty={`This file has no ${table} rows.`}
            paging={{ page: body.page, more: body.more, onPage: setPage }}
            columns={columnsFor(body.rows)}
          />
        </>
      )}
    </LoadState>
  );
}

function DownloadButton({
  auth,
  assetId,
  size,
}: {
  auth: AuthStore;
  assetId: string;
  size: number | null;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const go = () => {
    setBusy(true);
    setError(null);
    void loadDownload(auth.config, assetId, auth, doFetch).then((r) => {
      setBusy(false);
      if (r.kind === "loaded") window.location.assign(r.value.url);
      else if (r.kind === "error") setError(r.message);
      else auth.expire();
    });
  };
  return (
    <>
      <Button variant="primary" onClick={go} disabled={busy} data-testid="file-download">
        {busy ? "Preparing…" : `Download (${formatBytes(size)})`}
      </Button>
      {error === null ? null : (
        <Notice kind="error" testId="file-download-error">
          {error}
        </Notice>
      )}
    </>
  );
}

// Results that used this file: fetched when asked, so opening a file page makes no extra call.
function UsedByFile({ auth, assetId }: { auth: AuthStore; assetId: string }) {
  const [shown, setShown] = useState(false);
  return (
    <section aria-label="Results that used this file" data-testid="file-used-by">
      <h3>Results that used this file</h3>
      {shown ? (
        <UsedBy
          auth={auth}
          inputId={assetId}
          empty="No result used this file."
          testId="file-used-by-table"
        />
      ) : (
        <Button onClick={() => setShown(true)} data-testid="file-used-by-show">
          Show results that used this file
        </Button>
      )}
    </section>
  );
}

function FileBody({ auth, assetId, body }: { auth: AuthStore; assetId: string; body: AssetBody }) {
  const asset = body.asset;
  const kind = kindOf(asset);
  const tabs = TABS[kind];
  const [tab, select] = useTab(
    `/files/${encodeURIComponent(assetId)}`,
    tabs.map((t) => t.id),
  );
  const dataset = text(asset.dataset);
  const subject = text(asset.subject);
  const size = typeof asset.size_bytes === "number" ? asset.size_bytes : null;
  return (
    <>
      <Ready />
      <Spine
        trail={toTrail(
          dataCrumbs({
            ...(dataset === null ? {} : { dataset }),
            file: relativePath(asset),
          }),
        )}
      />
      <h2>{relativePath(asset)}</h2>
      <div className="file-head">
        <DownloadButton auth={auth} assetId={assetId} size={size} />
      </div>
      <KeyValue
        testId="file-facts"
        items={[
          {
            label: "Dataset",
            value: dataset === null ? null : <a href={datasetViewPath(dataset)}>{dataset}</a>,
          },
          {
            label: "Subject",
            value:
              dataset === null || subject === null ? (
                none(subject)
              ) : (
                <a href={subjectPath(dataset, subject)}>{subject}</a>
              ),
          },
          { label: "Session", value: text(asset.bids_session) },
          { label: "Format", value: text(asset.format) },
          { label: "Size", value: formatBytes(size) },
          { label: "Version", value: text(asset.s3_version_id) },
        ]}
      />
      <Tabs tabs={tabs} selected={tab} onSelect={select} label="File sections">
        {tab === "overview" ? <Overview body={body} /> : null}
        {tab === "units" ? (
          <UnitsTab
            auth={auth}
            file={{
              assetId,
              dataset,
              subject,
              session: text(asset.bids_session),
            }}
          />
        ) : null}
        {tab === "lfp" ? (
          <SignalsLoader auth={auth} assetId={assetId} dataset={dataset} subject={subject} />
        ) : null}
        {tab === "electrodes" ? (
          <TableTab auth={auth} assetId={assetId} table="electrodes" />
        ) : null}
        {tab === "series" ? <TableTab auth={auth} assetId={assetId} table="series" /> : null}
        {tab === "events" ? (
          <SignalsLoader
            auth={auth}
            assetId={assetId}
            dataset={dataset}
            subject={subject}
            view="events"
          />
        ) : null}
        {tab === "image" ? (
          <Volume
            auth={auth}
            assetId={assetId}
            header={volumeHeader(body.headers[0])}
            sizeBytes={size}
            s3Key={text(asset.s3_key) ?? ""}
          />
        ) : null}
      </Tabs>
      <UsedByFile auth={auth} assetId={assetId} />
    </>
  );
}

export function FilePage({ auth, assetId }: { auth: AuthStore; assetId: string }) {
  const run = useCallback(() => loadAsset(auth.config, assetId, auth, doFetch), [auth, assetId]);
  const { state, reload } = useLoad(auth, run);
  return (
    <section className="start research" data-testid="file-page">
      <LoadState state={state} noun="the file" path={`/catalog/assets/${assetId}`} onRetry={reload}>
        {(body) => <FileBody auth={auth} assetId={assetId} body={body} />}
      </LoadState>
    </section>
  );
}
