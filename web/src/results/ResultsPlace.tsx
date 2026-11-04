// The Results place: `/results` and `/results/:artifact_id` as one view, the
// lazy `results` chunk. It loads Mine and Everyone's (`GET /artifacts`), the study titles
// (`GET /catalog/studies`) and names every asset the listed results used in batches of 100
// (`GET /catalog/assets?ids=`); an open result adds its record and what used it. The author controls
// show when the record's `author_sub` is the access token's `sub` (display only: the API refuses anyone
// else with 403 `not_author`, shown as a Notice). Authors are shown by email. The record's
// inline link, fetched as the page mounts, feeds the preview; Download asks for a fresh attachment
// link. A result with a run lists that run's files in one request (`?run=`); each row carries its own
// inline link, so image thumbnails need no further API call. A failed listing shows its HTTP status.
// The Runs box groups the All listing by run; its subjects come from the asset names already loaded
// for the tree, so it adds no request.
import { useCallback, useMemo, useState } from "react";
import type { AuthStore } from "../auth/store";
import { Button } from "../kit/Button";
import { Dialog } from "../kit/Dialog";
import { Notice } from "../kit/Notice";
import { appRouter } from "../router/router";
import { Ready } from "../shell/marks";
import { tokenEmail } from "../shell/user";
import { useLoad } from "../useLoad";
import {
  chainModel,
  isImage,
  listModel,
  parseStudyNames,
  runsModel,
  type StripItem,
  stripModel,
} from "./model";
import { ResultChain, ResultsLayout, ResultsList, RunStrip } from "./ResultsView";
import {
  type Artifact,
  type ArtifactRecord,
  type AssetName,
  deleteArtifact,
  editArtifact,
  loadArtifact,
  loadArtifacts,
  loadAssetNames,
  loadDownload,
  loadRun,
  loadStudiesBody,
  loadUsedBy,
  tokenSub,
  withdrawArtifact,
} from "./results";
import "./results.css";

export type ResultsRoute = { name: "results" } | { name: "result"; artifact_id: string };

const fetchFn = (url: string, init: RequestInit) => fetch(url, init);

type ListsResult =
  | {
      kind: "loaded";
      mine: readonly Artifact[];
      all: readonly Artifact[];
      names: ReadonlyMap<string, AssetName>;
      studies: unknown;
    }
  | { kind: "error"; message: string }
  | { kind: "ended" };

async function loadLists(auth: AuthStore): Promise<ListsResult> {
  const [mine, all, studies] = await Promise.all([
    loadArtifacts(auth.config, "mine", auth, fetchFn),
    loadArtifacts(auth.config, "all", auth, fetchFn),
    loadStudiesBody(auth.config, auth, fetchFn),
  ]);
  if (mine.kind !== "loaded") return mine;
  if (all.kind !== "loaded") return all;
  const ids = [...mine.artifacts, ...all.artifacts].flatMap((a) => a.source_asset_ids);
  const names = await loadAssetNames(auth.config, ids, auth, fetchFn);
  if (names.kind !== "loaded") return names;
  return {
    kind: "loaded",
    mine: mine.artifacts,
    all: all.artifacts,
    names: names.names,
    // The tree falls back to dataset names when the studies cannot be read.
    studies: studies.kind === "loaded" ? studies.value : null,
  };
}

type OneResult =
  | {
      kind: "loaded";
      record: ArtifactRecord;
      /** The inline link fetched with the record: the preview's source. */
      url: string;
      usedBy: readonly Artifact[];
      names: ReadonlyMap<string, AssetName>;
    }
  | { kind: "error"; message: string }
  | { kind: "ended" };

async function loadOne(auth: AuthStore, id: string): Promise<OneResult> {
  const [got, used] = await Promise.all([
    loadArtifact(auth.config, id, auth, fetchFn),
    loadUsedBy(auth.config, id, auth, fetchFn),
  ]);
  if (got.kind !== "loaded") return got;
  if (used.kind !== "loaded") return used;
  const r = got.record;
  const ids = [
    ...r.sources.map((s) => s.asset_id),
    ...r.inputs.filter((i) => i.kind === "asset").map((i) => i.id),
  ];
  const names = await loadAssetNames(auth.config, ids, auth, fetchFn);
  if (names.kind !== "loaded") return names;
  return { kind: "loaded", record: r, url: got.url, usedBy: used.artifacts, names: names.names };
}

type StripResult =
  | { kind: "loaded"; items: readonly StripItem[] | null }
  | { kind: "error"; message: string }
  | { kind: "ended" };

/** One run's files in one request; each image row's own inline link is its thumbnail. */
async function loadStrip(auth: AuthStore, runId: string, currentId: string): Promise<StripResult> {
  const listed = await loadRun(auth.config, runId, auth, fetchFn);
  if (listed.kind !== "loaded") return listed;
  const thumbs = new Map<string, string>();
  for (const f of listed.files) {
    if (f.url !== null && isImage(f.content_type)) thumbs.set(f.artifact_id, f.url);
  }
  return { kind: "loaded", items: stripModel(listed.files, currentId, thumbs) };
}

function StripLoader({
  auth,
  runId,
  currentId,
}: {
  auth: AuthStore;
  runId: string;
  currentId: string;
}) {
  const run = useCallback(() => loadStrip(auth, runId, currentId), [auth, runId, currentId]);
  const { state } = useLoad(auth, run);
  if (state.kind === "loading") {
    return (
      <p className="chain-strip-status" data-testid="result-run-strip-loading">
        Listing the other files of this run…
      </p>
    );
  }
  if (state.kind === "error") {
    return (
      <p className="chain-strip-status" data-testid="result-run-strip-error">
        The other files of this run could not be listed: {state.message}
      </p>
    );
  }
  if (state.kind === "ended" || state.items === null) return null;
  return <RunStrip items={state.items} />;
}

function Chain({
  auth,
  id,
  known,
  onChanged,
}: {
  auth: AuthStore;
  id: string;
  known: ReadonlyMap<string, Artifact>;
  onChanged: () => void;
}) {
  const run = useCallback(() => loadOne(auth, id), [auth, id]);
  const { state, reload } = useLoad(auth, run);
  const [patch, setPatch] = useState<Partial<ArtifactRecord>>({});
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"withdraw" | "delete" | null>(null);

  if (state.kind === "loading") {
    return (
      <Notice kind="info" testId="result-loading">
        Loading the result…
      </Notice>
    );
  }
  if (state.kind === "error") {
    return (
      <Notice kind="error" testId="result-error" action={{ label: "Try again", onClick: reload }}>
        {state.message}
      </Notice>
    );
  }
  if (state.kind === "ended") return null;
  const record = { ...state.record, ...patch };
  const chain = chainModel({
    record,
    names: state.names,
    known,
    usedBy: state.usedBy,
    sub: tokenSub(auth.getAccessToken()),
  });

  const act = async (what: "name" | "note" | "withdraw" | "delete", value?: string | null) => {
    setBusy(true);
    setProblem(null);
    setDone(null);
    const result =
      what === "withdraw"
        ? await withdrawArtifact(auth.config, id, auth, fetchFn)
        : what === "delete"
          ? await deleteArtifact(auth.config, id, auth, fetchFn)
          : await editArtifact(
              auth.config,
              id,
              {
                name: what === "name" ? String(value) : record.name,
                note: what === "note" ? (value ?? null) : record.note,
              },
              auth,
              fetchFn,
            );
    setBusy(false);
    setConfirm(null);
    if (result.kind === "ended") {
      auth.expire();
      return;
    }
    if (result.kind === "error") {
      setProblem(result.message);
      return;
    }
    if (what === "delete") {
      onChanged();
      appRouter().navigate("/results");
      return;
    }
    if (what === "withdraw") {
      setPatch((p) => ({ ...p, state: "WITHDRAWN" }));
      setDone("Withdrawn. The file stays available to download.");
    } else if (what === "name") {
      setPatch((p) => ({ ...p, name: String(value) }));
      setDone("Renamed.");
    } else {
      setPatch((p) => ({ ...p, note: value ?? null }));
      setDone("Note saved.");
    }
    onChanged();
  };

  const download = async () => {
    setProblem(null);
    // A fresh attachment link: the page's inline link shows the file in place and expires after
    // 15 minutes.
    const r = await loadDownload(auth.config, id, auth, fetchFn);
    if (r.kind === "ended") auth.expire();
    else if (r.kind === "error") setProblem(r.message);
    else window.location.assign(r.url);
  };

  const status = (
    <>
      {problem === null ? null : (
        <Notice kind="error" testId="result-problem">
          {problem}
        </Notice>
      )}
      {done === null ? null : (
        <Notice kind="success" testId="result-done">
          {done}
        </Notice>
      )}
    </>
  );
  return (
    <>
      <Ready />
      <ResultChain
        chain={chain}
        status={status}
        previewUrl={state.url}
        strip={
          record.run_id === null ? null : (
            <StripLoader auth={auth} runId={record.run_id} currentId={id} />
          )
        }
        on={{
          busy,
          onDownload: () => void download(),
          onRename: (name) => void act("name", name),
          onNote: (note) => void act("note", note),
          onWithdraw: () => setConfirm("withdraw"),
          onDelete: () => setConfirm("delete"),
        }}
      />
      <Dialog
        open={confirm !== null}
        title={confirm === "delete" ? "Delete this result?" : "Withdraw this result?"}
        onClose={() => setConfirm(null)}
        testId="result-confirm"
      >
        <p>
          {confirm === "delete"
            ? "The file and its record are removed for everyone. This cannot be undone."
            : "Everyone can still read and download it; it is marked withdrawn."}
        </p>
        <Button
          variant="primary"
          disabled={busy}
          data-autofocus
          onClick={() => void act(confirm ?? "withdraw")}
        >
          {confirm === "delete" ? "Delete" : "Withdraw"}
        </Button>
        <Button onClick={() => setConfirm(null)}>Cancel</Button>
      </Dialog>
    </>
  );
}

export function ResultsPlace({ auth, route }: { auth: AuthStore; route: ResultsRoute }) {
  const run = useCallback(() => loadLists(auth), [auth]);
  const { state, reload } = useLoad(auth, run);
  const [scope, setScope] = useState<"mine" | "all">("all");
  const [open, setOpen] = useState<{ branch: string; session: string | null } | null>(null);
  const selected = route.name === "result" ? route.artifact_id : null;
  const me = tokenEmail(auth.getIdToken());

  const known = useMemo(() => {
    const m = new Map<string, Artifact>();
    if (state.kind === "loaded")
      for (const a of [...state.mine, ...state.all]) m.set(a.artifact_id, a);
    return m;
  }, [state]);

  let side = null;
  if (state.kind === "loading") {
    side = (
      <Notice kind="info" testId="results-loading">
        Loading results…
      </Notice>
    );
  } else if (state.kind === "error") {
    side = (
      <Notice kind="error" testId="results-error" action={{ label: "Try again", onClick: reload }}>
        {state.message}
      </Notice>
    );
  } else if (state.kind === "loaded") {
    const list = listModel({
      scope,
      rows: scope === "mine" ? state.mine : state.all,
      mineCount: state.mine.length,
      me,
      names: state.names,
      studies: parseStudyNames(state.studies),
      open,
      selected,
    });
    side = (
      <ResultsList
        list={list}
        runs={runsModel(state.all, state.names, me)}
        on={{
          onScope: (s) => {
            setScope(s);
            setOpen(null);
          },
          onOpen: (branch, session) => setOpen({ branch, session }),
        }}
      />
    );
  }
  const main =
    selected === null ? (
      <p className="results-pick" data-testid="results-pick">
        Pick a result in the timeline or the tree to see what it used and what used it.
        {state.kind === "loaded" ? <Ready /> : null}
      </p>
    ) : (
      <Chain key={selected} auth={auth} id={selected} known={known} onChanged={reload} />
    );
  return <ResultsLayout side={side} main={main} />;
}
