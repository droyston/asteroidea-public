// The results table and the "used by" list, shared by the results page, the result page and
// the file page. Each row links to its result page; the author is shown by email.
import { useCallback } from "react";
import type { AuthStore } from "../auth/store";
import { formatCreated } from "../auth/UploadsPanel";
import { Notice } from "../kit/Notice";
import { type Column, Table } from "../kit/Table";
import { useLoad } from "../useLoad";
import { type Artifact, formatBytes, loadUsedBy } from "./results";

export function ResultsTable({
  rows,
  author,
  label,
  empty,
  testId,
}: {
  rows: readonly Artifact[];
  author: boolean;
  label: string;
  empty: string;
  testId: string;
}) {
  const columns: Column<Artifact>[] = [
    {
      key: "name",
      header: "Name",
      what: true,
      render: (a) => <a href={`/results/${encodeURIComponent(a.artifact_id)}`}>{a.name}</a>,
    },
    ...(author
      ? [{ key: "author", header: "Author", render: (a: Artifact) => a.author_email ?? "—" }]
      : []),
    { key: "size", header: "Size", numeric: true, render: (a) => formatBytes(a.size) },
    { key: "created", header: "Created", render: (a) => formatCreated(a.created_at) },
    { key: "state", header: "State", render: (a) => a.state },
  ];
  return (
    <Table
      columns={columns}
      rows={rows}
      rowKey={(a) => a.artifact_id}
      label={label}
      empty={empty}
      testId={testId}
    />
  );
}

/** The results that used `inputId`, one level (`GET /artifacts?scope=all&input=`). */
export function UsedBy({
  auth,
  inputId,
  empty,
  testId,
}: {
  auth: AuthStore;
  inputId: string;
  empty: string;
  testId: string;
}) {
  const run = useCallback(
    () => loadUsedBy(auth.config, inputId, auth, (url, init) => fetch(url, init)),
    [auth, inputId],
  );
  const { state, reload } = useLoad(auth, run);
  if (state.kind === "loading") {
    return (
      <Notice kind="info" testId={`${testId}-loading`}>
        Loading…
      </Notice>
    );
  }
  if (state.kind === "error") {
    return (
      <Notice
        kind="error"
        testId={`${testId}-error`}
        action={{ label: "Try again", onClick: reload }}
      >
        {state.message}
      </Notice>
    );
  }
  if (state.kind === "ended") return null;
  return (
    <ResultsTable
      rows={state.artifacts}
      author
      label="Results that used this"
      empty={empty}
      testId={testId}
    />
  );
}
