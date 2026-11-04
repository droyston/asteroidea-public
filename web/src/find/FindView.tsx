// The Find page's pure view: the species by modality matrix over the whole
// index, the facets with their distribution bars, the best four fits with a count inviting narrowing
// (never a scrolled table), "already in catalog" marked, and the picks rail as a slot. It takes
// the index, the filters and callbacks, so the artboard harness renders the same component over the
// golden index. `data-ab` marks are what the harness counts.
import type { ReactNode } from "react";
import type { Dataset, Index } from "../index/schema";
import { Button } from "../kit/Button";
import { formatBytes } from "../table/facets";
import {
  applyFind,
  type Bin,
  bestFit,
  COLUMNS,
  type Column,
  containsCounts,
  emptyFind,
  FACETS,
  type Facet,
  type FindFilters,
  facetCounts,
  filtersToSearch,
  histogram,
  joinKey,
  matrix,
  SIZE_BINS,
  SPECIES,
  type SpeciesRow,
  SUBJECT_BINS,
  speciesCounts,
} from "./model";

export const BEST = 4;

export interface FindViewProps {
  index: Index;
  filters: FindFilters;
  onFilters: (f: FindFilters) => void;
  /** `"<archive>:<id>"` of every dataset with a pick. */
  picked: ReadonlySet<string>;
  onPick: (d: Dataset) => void;
  /** Index key to catalog dataset id; null when the catalog was not read (no sign-in, or it failed). */
  catalog: ReadonlyMap<string, string> | null;
  /** The picks rail. */
  rail: ReactNode;
  /** The index status line, drawn above the page. */
  status?: ReactNode;
  /** A curator also sees the Imports tab. */
  curator?: boolean;
  /** Which tab is current: the archive index, or the curator's Imports (`/discover/imports*`). */
  tab?: "index" | "imports";
}

const fmt = (n: number) => n.toLocaleString("en-US");

function toggled<T>(set: ReadonlySet<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

function level(n: number, max: number): number {
  if (n <= 0 || max <= 0) return 0;
  return Math.max(1, Math.ceil((4 * Math.log(1 + n)) / Math.log(1 + max)));
}

function Matrix({
  rows,
  filters,
  onFilters,
}: {
  rows: readonly Dataset[];
  filters: FindFilters;
  onFilters: (f: FindFilters) => void;
}) {
  const counts = matrix(rows);
  const max = Math.max(0, ...counts.values());
  const only = (set: ReadonlySet<string>, key: string) => set.size === 1 && set.has(key);
  return (
    <section aria-label="The whole index, species by modality" className="find-matrix">
      <p className="find-label">The whole index · species × modality · click a cell to filter</p>
      <table>
        <thead>
          <tr>
            <td />
            {COLUMNS.map((c) => (
              <th key={c.key} scope="col" data-ab="matrix-column">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {SPECIES.map((s) => (
            <tr key={s.key}>
              <th scope="row">{s.label}</th>
              {COLUMNS.map((c) => {
                const n = counts.get(`${s.key}|${c.key}`) ?? 0;
                if (n === 0) {
                  return (
                    <td key={c.key}>
                      <span className="find-cell find-cell-empty" data-ab="matrix-empty">
                        –
                      </span>
                    </td>
                  );
                }
                const on = only(filters.species, s.key) && only(filters.has, c.key);
                return (
                  <td key={c.key}>
                    <button
                      type="button"
                      className="find-cell"
                      data-level={level(n, max)}
                      data-ab="matrix-cell"
                      aria-pressed={on}
                      aria-label={`${s.label}, ${c.label}: ${n} datasets`}
                      onClick={() =>
                        onFilters(
                          on
                            ? { ...filters, species: new Set(), has: new Set() }
                            : {
                                ...filters,
                                species: new Set<SpeciesRow>([s.key]),
                                has: new Set<Column>([c.key]),
                              },
                        )
                      }
                    >
                      {fmt(n)}
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Option({
  label,
  count,
  max,
  checked,
  onChange,
}: {
  label: string;
  count: number;
  max: number;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <label className="find-opt" data-ab="facet-option">
      <input type="checkbox" checked={checked} onChange={onChange} />
      <span className="find-opt-label">{label}</span>
      <span className="find-n">{fmt(count)}</span>
      <span className="find-bar" aria-hidden="true">
        <i style={{ width: `${max === 0 ? 0 : Math.round((100 * count) / max)}%` }} />
      </span>
    </label>
  );
}

function Histogram({
  title,
  bins,
  counts,
  range,
  onRange,
}: {
  title: string;
  bins: readonly Bin[];
  counts: readonly number[];
  range: { min: number | null; max: number | null };
  onRange: (r: { min: number | null; max: number | null }) => void;
}) {
  const max = Math.max(0, ...counts);
  return (
    <fieldset className="find-facet" data-ab="histogram">
      <legend>{title}</legend>
      <div className="find-hist">
        {bins.map((b, i) => {
          const on = range.min === b.min && range.max === b.max;
          const n = counts[i] ?? 0;
          return (
            <button
              key={b.label}
              type="button"
              className="find-hist-bin"
              aria-pressed={on}
              aria-label={`${title} ${b.label}: ${n} datasets`}
              onClick={() => onRange(on ? { min: null, max: null } : { min: b.min, max: b.max })}
            >
              <span
                className="find-hist-bar"
                style={{ height: `${max === 0 ? 0 : (100 * n) / max}%` }}
              />
              <span className="find-hist-label">{b.label}</span>
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

function Facets({
  rows,
  filters,
  onFilters,
}: {
  rows: readonly Dataset[];
  filters: FindFilters;
  onFilters: (f: FindFilters) => void;
}) {
  const contains = containsCounts(rows, filters);
  const species = speciesCounts(rows, filters);
  const containsMax = Math.max(0, ...contains.values());
  const speciesMax = Math.max(0, ...species.values());
  return (
    <aside className="find-facets" aria-label="Filters">
      <label className="find-search">
        Search title or id
        <input
          type="search"
          value={filters.text}
          onChange={(e) => onFilters({ ...filters, text: e.target.value })}
        />
      </label>
      <fieldset className="find-facet" aria-label="Contains">
        <legend>Contains</legend>
        {COLUMNS.map((c) => (
          <Option
            key={c.key}
            label={c.label}
            count={contains.get(c.key) ?? 0}
            max={containsMax}
            checked={filters.has.has(c.key)}
            onChange={() => onFilters({ ...filters, has: toggled(filters.has, c.key) })}
          />
        ))}
      </fieldset>
      <fieldset className="find-facet" aria-label="Species">
        <legend>Species</legend>
        {SPECIES.map((s) => (
          <Option
            key={s.key}
            label={s.label}
            count={species.get(s.key) ?? 0}
            max={speciesMax}
            checked={filters.species.has(s.key)}
            onChange={() => onFilters({ ...filters, species: toggled(filters.species, s.key) })}
          />
        ))}
      </fieldset>
      <Histogram
        title="Participants"
        bins={SUBJECT_BINS}
        counts={histogram(rows, filters, "subjects", SUBJECT_BINS)}
        range={filters.subjects}
        onRange={(subjects) => onFilters({ ...filters, subjects })}
      />
      <Histogram
        title="Size"
        bins={SIZE_BINS}
        counts={histogram(rows, filters, "bytes", SIZE_BINS)}
        range={filters.bytes}
        onRange={(bytes) => onFilters({ ...filters, bytes })}
      />
      {FACETS.map((f) => (
        <MultiFacet
          key={f.key}
          facet={f.key}
          title={f.label}
          rows={rows}
          {...{ filters, onFilters }}
        />
      ))}
      <Button onClick={() => onFilters(emptyFind())}>Clear filters</Button>
      <p className="find-url">
        Filters live in the URL: <code>/discover{filtersToSearch(filters)}</code>
      </p>
    </aside>
  );
}

function MultiFacet({
  facet,
  title,
  rows,
  filters,
  onFilters,
}: {
  facet: Facet;
  title: string;
  rows: readonly Dataset[];
  filters: FindFilters;
  onFilters: (f: FindFilters) => void;
}) {
  const values = facetCounts(rows, filters, facet).slice(0, 6);
  const max = Math.max(0, ...values.map((v) => v.count));
  return (
    <fieldset className="find-facet" aria-label={title}>
      <legend>{title}</legend>
      {values.map((v) => (
        <Option
          key={v.key}
          label={v.key}
          count={v.count}
          max={max}
          checked={filters[facet].has(v.key)}
          onChange={() => onFilters({ ...filters, [facet]: toggled(filters[facet], v.key) })}
        />
      ))}
    </fieldset>
  );
}

function describeFit(f: FindFilters): string {
  const parts: string[] = [];
  const sp = SPECIES.filter((s) => f.species.has(s.key)).map((s) => s.label.toLowerCase());
  if (sp.length > 0) parts.push(sp.join(" or "));
  const has = COLUMNS.filter((c) => f.has.has(c.key)).map((c) => c.label);
  if (has.length > 0) parts.push(has.join(" and "));
  if (f.text.trim() !== "") parts.push(`“${f.text.trim()}”`);
  return parts.length === 0 ? "in the archive index" : `fit ${parts.join(", ")}`;
}

function FitRow({
  d,
  picked,
  onPick,
  catalog,
}: {
  d: Dataset;
  picked: boolean;
  onPick: () => void;
  catalog: string | null;
}) {
  return (
    <li className="find-fit" data-ab="fit-row" data-testid={`row-${d.source}-${d.id}`}>
      <div className="find-fit-main">
        <a className="find-fit-title" href={d.urls.landing} target="_blank" rel="noreferrer">
          {d.title}
        </a>
        <span className="find-fit-meta">
          {d.source} {d.id} · {d.version} ·{" "}
          {d.license.spdx ?? d.license.raw ?? "licence not stated"}
        </span>
      </div>
      <span className="find-fit-num" title="Participants">
        {d.subjects ?? "—"}
      </span>
      <span className="find-fit-num" title="Size">
        {formatBytes(d.bytes)}
      </span>
      <span className="find-fit-catalog" data-ab={catalog === null ? undefined : "in-catalog"}>
        {catalog === null ? null : <a href={`/d/${encodeURIComponent(catalog)}`}>In catalog</a>}
      </span>
      <span className="find-fit-action">
        {picked ? (
          <strong>Picked</strong>
        ) : (
          <Button variant="quiet" aria-label={`Pick dataset ${d.id}`} onClick={onPick}>
            Pick
          </Button>
        )}
      </span>
    </li>
  );
}

export function FindView({
  index,
  filters,
  onFilters,
  picked,
  onPick,
  catalog,
  rail,
  status,
  curator = false,
  tab = "index",
}: FindViewProps) {
  const rows = index.datasets;
  const shown = applyFind(rows, filters);
  const best = bestFit(rows, filters, BEST);
  const more = shown.length - best.length;
  return (
    <div className="find" data-testid="find">
      <nav className="find-tabs" aria-label="Find sections">
        {tab === "index" ? (
          <span aria-current="page">Archive index</span>
        ) : (
          <a href={`/discover${typeof window === "undefined" ? "" : window.location.search}`}>
            Archive index
          </a>
        )}
        {curator && tab === "index" ? (
          <a href="/discover/imports">
            Imports <small>curator</small>
          </a>
        ) : null}
        {curator && tab === "imports" ? (
          <span aria-current="page">
            Imports <small>curator</small>
          </span>
        ) : null}
      </nav>
      {status}
      <div className="find-body">
        <Facets rows={rows} filters={filters} onFilters={onFilters} />
        <section className="find-main" aria-label="Datasets">
          <h2 className="find-count" data-testid="match-count">
            {fmt(shown.length)} of {fmt(rows.length)} datasets
          </h2>
          <p className="find-fits">archive datasets {describeFit(filters)}</p>
          <Matrix rows={rows} filters={filters} onFilters={onFilters} />
          <p className="find-label">Best fit first · {fmt(best.length)}</p>
          {best.length === 0 ? (
            <p className="find-none">
              No dataset fits. Clear a filter, or{" "}
              <Button onClick={() => onFilters(emptyFind())}>clear all filters</Button>
            </p>
          ) : (
            <ol className="find-fit-list" data-testid="best-fit">
              {best.map((d) => (
                <FitRow
                  key={`${d.source}-${d.id}-${d.version}`}
                  d={d}
                  picked={picked.has(joinKey(d))}
                  onPick={() => onPick(d)}
                  catalog={catalog?.get(joinKey(d)) ?? null}
                />
              ))}
            </ol>
          )}
          <p className="find-more" data-testid="find-more">
            {more > 0
              ? `${fmt(more)} more fit · narrow by species, participants or size`
              : shown.length > 0
                ? "That is every dataset that fits."
                : ""}
          </p>
        </section>
        {rail}
      </div>
    </div>
  );
}
