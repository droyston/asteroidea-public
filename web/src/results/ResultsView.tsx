// The Results place's pure view. It takes the models and callbacks and
// nothing else, so the artboard harness renders the same components over a fixture. `data-ab` marks are
// what the harness counts. No scrolled table: the timeline draws all 100, the tree at most 10 branches,
// the open branch its newest 4 and a count. The preview draws a PNG or JPEG as an image and an
// HTML page in a frame sandboxed to scripts only (never same-origin), both from the presigned link; any
// other type keeps the labelled box with a sentence. "From the same run" links the run's other files.
// The Runs box tops the side column: the newest runs, each one link to its first figure.
import { type FormEvent, type ReactNode, useState } from "react";
import { Button } from "../kit/Button";
import { EmptyResults } from "../states/EmptyResults";
import {
  type ChainModel,
  type InputCard,
  type ListModel,
  type RunRow,
  runDetail,
  type StripItem,
} from "./model";

export interface ListActions {
  onScope: (scope: "mine" | "all") => void;
  onOpen: (branch: string, session: string | null) => void;
}

function Toggle({ list, on }: { list: ListModel; on: ListActions }) {
  const tab = (scope: "mine" | "all", label: string) => (
    <button
      type="button"
      role="tab"
      aria-selected={list.scope === scope}
      className={`results-toggle-btn${list.scope === scope ? " is-on" : ""}`}
      onClick={() => on.onScope(scope)}
    >
      {label}
    </button>
  );
  return (
    <div className="results-toggle" role="tablist" aria-label="Whose results">
      {tab("mine", `Mine ${list.mineCount}`)}
      {tab("all", "Everyone's")}
    </div>
  );
}

const W = 340;
const H = 56;

function Dots({ list }: { list: ListModel }) {
  const t = list.timeline;
  return (
    <figure className="results-timeline" data-testid="results-timeline" data-ab="timeline">
      <svg
        viewBox={`0 0 ${W} ${H + 16}`}
        role="img"
        aria-label={`The newest ${t.dots.length} results`}
      >
        <line x1={0} x2={W} y1={H} y2={H} className="results-axis" />
        {t.dots.map((d) => (
          <a key={d.artifact_id} href={d.href} data-ab="dot" data-ab-mine={d.mine ? "1" : "0"}>
            <circle
              cx={4 + d.x * (W - 8)}
              cy={4 + d.y * (H - 10)}
              r={2.6}
              className={d.mine ? "results-dot is-mine" : "results-dot"}
            >
              <title>{d.name}</title>
            </circle>
          </a>
        ))}
        {t.ticks.map((k) => (
          <text key={`${k.label}-${k.x}`} x={k.x * W} y={H + 13} className="results-tick">
            {k.label}
          </text>
        ))}
      </svg>
      <figcaption className="results-legend">
        <span className="results-key is-mine" /> yours · <span className="results-key" /> others ·
        newest {t.dots.length} finalised
      </figcaption>
    </figure>
  );
}

function Tree({ list, on }: { list: ListModel; on: ListActions }) {
  return (
    <nav className="results-tree" aria-label="By the data they used" data-testid="results-tree">
      <p className="results-eyebrow">By the data they used</p>
      <ul>
        {list.branches.map((b) => (
          <li key={b.key} data-ab="branch">
            <button
              type="button"
              className={`results-branch${b.open ? " is-open" : ""}`}
              aria-expanded={b.open}
              onClick={() => on.onOpen(b.key, null)}
            >
              <span>{b.label}</span>
              <span className="results-count">{b.count}</span>
            </button>
            {b.open && b.sessions.length > 0 ? (
              <ul>
                {b.sessions.map((s) => (
                  <li key={s.key}>
                    <button
                      type="button"
                      className={`results-session${s.open ? " is-open" : ""}`}
                      onClick={() => on.onOpen(b.key, s.key)}
                    >
                      <span>{s.label}</span>
                      <span className="results-count">{s.count}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>
    </nav>
  );
}

function Branch({ list }: { list: ListModel }) {
  if (list.openLabel === null) return null;
  const count = list.rows.length + list.more;
  return (
    <section className="results-branch-list" aria-label={list.openLabel} data-testid="results-list">
      <p className="results-eyebrow">
        {list.openLabel} · {count}
      </p>
      <ul>
        {list.rows.map((r) => (
          <li key={r.artifact_id} data-ab="result-row">
            <a
              href={r.href}
              className={`results-row${r.selected ? " is-selected" : ""}${r.pending ? " is-pending" : ""}`}
              aria-current={r.selected ? "page" : undefined}
            >
              <span className="results-row-name">{r.name}</span>
              <span>{r.author}</span>
              <span>{r.date}</span>
            </a>
          </li>
        ))}
      </ul>
      {list.more > 0 ? <p className="results-more">{list.more} more · sorted newest</p> : null}
    </section>
  );
}

/** The newest runs, one link each to the run's first figure; nothing when there is no run. */
export function RunsBox({ runs }: { runs: readonly RunRow[] }) {
  if (runs.length === 0) return null;
  return (
    <section className="results-runs" aria-label="Runs" data-testid="results-runs">
      <h3 className="results-eyebrow">Runs</h3>
      <ul>
        {runs.map((r) => (
          <li key={r.run_id} data-ab="run-row">
            <a
              href={r.href}
              className={`results-run${r.mine ? " is-mine" : ""}`}
              data-testid="results-run"
            >
              <strong>{r.analysis}</strong>
              <small>{runDetail(r)}</small>
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ResultsList({
  list,
  on,
  runs = [],
}: {
  list: ListModel;
  on: ListActions;
  /** The Runs box above the timeline, from the All listing whatever the scope. */
  runs?: readonly RunRow[];
}) {
  return (
    <aside className="results-side" data-testid="results-side">
      <RunsBox runs={runs} />
      <div className="results-side-head">
        <h2>
          {list.total} result{list.total === 1 ? "" : "s"}
        </h2>
        <Toggle list={list} on={on} />
      </div>
      {list.total === 0 ? (
        <EmptyResults scope={list.scope} />
      ) : (
        <>
          <Dots list={list} />
          <Tree list={list} on={on} />
          <Branch list={list} />
        </>
      )}
    </aside>
  );
}

// --- the chain ---

export interface ChainActions {
  onDownload: () => void;
  onRename: (name: string) => void;
  onNote: (note: string | null) => void;
  onWithdraw: () => void;
  onDelete: () => void;
  busy: boolean;
}

function Card({ card }: { card: InputCard }) {
  const head = (
    <span className="chain-kind">
      {card.kind}
      {card.role === null ? null : ` · role: ${card.role}`}
    </span>
  );
  return (
    <li className="chain-card" data-ab="input" data-ab-current={String(card.current)}>
      {head}
      {card.href === null ? (
        <strong>{card.title}</strong>
      ) : (
        <a href={card.href}>
          <strong>{card.title}</strong>
        </a>
      )}
      {card.detail === null && card.current === null ? null : (
        <small className="chain-detail">
          {card.detail}
          {card.current === true ? " · current ✓" : null}
          {card.current === false ? " · a newer version exists" : null}
        </small>
      )}
    </li>
  );
}

function Header({ chain, on }: { chain: ChainModel; on: ChainActions }) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(chain.name);
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState(chain.note ?? "");
  const rename = (e: FormEvent) => {
    e.preventDefault();
    setRenaming(false);
    on.onRename(name);
  };
  const saveNote = (e: FormEvent) => {
    e.preventDefault();
    setNoting(false);
    on.onNote(note === "" ? null : note);
  };
  const badge = chain.state === "WITHDRAWN" ? "Withdrawn" : "Saved";
  return (
    <header className="chain-head">
      <div className="chain-title">
        {renaming ? (
          <form onSubmit={rename} className="chain-rename" data-testid="result-rename-form">
            <label>
              Name
              <input value={name} onChange={(e) => setName(e.target.value)} required />
            </label>
            <Button type="submit" variant="primary" disabled={on.busy}>
              Save name
            </Button>
            <Button onClick={() => setRenaming(false)}>Cancel</Button>
          </form>
        ) : (
          <h2>
            {chain.name} <span className="chain-badge">{badge}</span>
          </h2>
        )}
        <div className="chain-actions">
          <Button variant="primary" onClick={on.onDownload} data-testid="result-download">
            Download
          </Button>
          {chain.mine ? (
            <>
              <Button onClick={() => setRenaming(true)} disabled={on.busy}>
                Rename
              </Button>
              {chain.state === "COMMITTED" ? (
                <Button onClick={on.onWithdraw} disabled={on.busy} data-testid="result-withdraw">
                  Withdraw…
                </Button>
              ) : null}
              <Button
                onClick={on.onDelete}
                disabled={on.busy || chain.deleteRefusal !== null}
                title={chain.deleteRefusal ?? undefined}
                data-testid="result-delete"
              >
                Delete
              </Button>
            </>
          ) : null}
        </div>
      </div>
      <p className="chain-by" data-testid="result-by">
        By <strong>{chain.author}</strong>
        {chain.mine ? " (you)" : ""} · {chain.when} · {chain.size}
      </p>
      {noting ? (
        <form onSubmit={saveNote} className="chain-note-form" data-testid="result-note-form">
          <label>
            Note
            <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
          </label>
          <Button type="submit" variant="primary" disabled={on.busy}>
            Save note
          </Button>
          <Button onClick={() => setNoting(false)}>Cancel</Button>
        </form>
      ) : (
        <p className="chain-note">
          {chain.note === null ? null : <q>{chain.note}</q>}{" "}
          {chain.mine ? (
            <button type="button" className="chain-link" onClick={() => setNoting(true)}>
              {chain.note === null ? "Add a note" : "Edit note"}
            </button>
          ) : null}
        </p>
      )}
      {chain.mine && chain.deleteRefusal !== null ? (
        <p className="chain-refusal" data-testid="result-delete-reason">
          {chain.deleteRefusal}
        </p>
      ) : null}
    </header>
  );
}

export const PREVIEW_LOADING = "Loading the preview…";
export const PREVIEW_FAILED = "The preview could not be loaded; download it";

function Preview({ chain, url }: { chain: ChainModel; url: string | null }) {
  const [failed, setFailed] = useState(false);
  const p = chain.preview;
  const label = `Preview of ${chain.name}`;
  if (p.kind === "none" || url === null || failed) {
    const text = p.kind === "none" ? p.text : failed ? PREVIEW_FAILED : PREVIEW_LOADING;
    return (
      <div className="chain-preview" data-testid="result-preview" data-ab="preview">
        {text}
      </div>
    );
  }
  return (
    <div
      className={`chain-preview is-${p.kind}`}
      data-testid="result-preview"
      data-ab="preview"
      data-kind={p.kind}
    >
      {p.kind === "image" ? (
        <img
          src={url}
          alt={label}
          className="chain-preview-image"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <iframe
          src={url}
          title={label}
          className="chain-preview-frame"
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
        />
      )}
    </div>
  );
}

/** A strip thumbnail; one that fails to load leaves the name chip alone. */
function Thumb({ src }: { src: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    <img
      src={src}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  );
}

export function RunStrip({ items }: { items: readonly StripItem[] }) {
  return (
    <section className="chain-strip" aria-label="From the same run" data-testid="result-run-strip">
      <p className="results-eyebrow">From the same run · {items.length}</p>
      <ul>
        {items.map((i) => (
          <li key={i.artifact_id} data-ab="run-item">
            <a
              href={i.href}
              className={`chain-strip-item${i.current ? " is-current" : ""}`}
              aria-current={i.current ? "page" : undefined}
              title={i.name}
            >
              {i.thumb === null ? null : <Thumb src={i.thumb} />}
              <span>{i.name}</span>
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ResultChain({
  chain,
  on,
  status,
  previewUrl = null,
  strip = null,
}: {
  chain: ChainModel;
  on: ChainActions;
  status?: ReactNode;
  /** The presigned inline link, fetched when the page mounts; null until then. */
  previewUrl?: string | null;
  /** The run strip, or a status line while it loads or when it failed. */
  strip?: ReactNode;
}) {
  return (
    <article className="chain" data-testid="result-page" aria-label={chain.name}>
      <Header key={`${chain.artifact_id}-${chain.name}-${chain.note}`} chain={chain} on={on} />
      {status}
      <p className="results-eyebrow">Provenance · what it used, what used it</p>
      <div className="chain-row">
        <section className="chain-col" aria-label="Inputs">
          <ul className="chain-inputs" data-testid="result-inputs">
            {chain.inputs.length === 0 ? (
              <li className="chain-card">This result records no inputs.</li>
            ) : (
              chain.inputs.map((c) => <Card key={c.key} card={c} />)
            )}
          </ul>
        </section>
        <section className="chain-col chain-run" aria-label="Analysis" data-testid="result-run">
          <span className="chain-kind">Analysis</span>
          {chain.run === null ? (
            <p>This result has no run record; it was saved before runs were recorded.</p>
          ) : (
            <>
              <strong>{chain.run.title}</strong>
              <dl>
                {chain.run.lines.map((l) => (
                  <div key={`${l.label}-${l.value}`}>
                    <dt>{l.label}</dt>
                    <dd>{l.value}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
        </section>
        <section className="chain-col chain-this" aria-label="This result">
          <span className="chain-kind">This result</span>
          <strong>{chain.name}</strong>
          <small>{chain.size}</small>
        </section>
        <section className="chain-col" aria-label="Used by">
          <h3 className="chain-kind">Used by · {chain.usedBy.length}</h3>
          <ul className="chain-inputs" data-testid="result-used-by">
            {chain.usedBy.length === 0 ? (
              <li className="chain-card chain-none">No result used this one.</li>
            ) : (
              chain.usedBy.map((u) => (
                <li key={u.artifact_id} className="chain-card" data-ab="used-by">
                  <a href={u.href}>
                    <strong>{u.name}</strong>
                  </a>
                  <small>{u.detail}</small>
                </li>
              ))
            )}
          </ul>
        </section>
      </div>
      <p className="results-eyebrow">Preview</p>
      <Preview key={previewUrl ?? ""} chain={chain} url={previewUrl} />
      {strip}
      <p className="chain-rules">
        Everyone can read this result. Only its author can rename, edit the note, withdraw or delete
        it. Delete is refused while another result uses it.
      </p>
    </article>
  );
}

export function ResultsLayout({ side, main }: { side: ReactNode; main: ReactNode }) {
  return (
    <section className="results-place" aria-label="Results" data-testid="results-page">
      {side}
      <div className="results-main">{main}</div>
    </section>
  );
}
