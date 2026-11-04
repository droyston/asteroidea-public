// The data home: a pure view of the studies and links bodies. It has
// no state of its own: the tier and the facet choices are a `HomeQuery` the page keeps in the URL
// (see `home/facets.ts`), passed in with `onQuery` for the controls. Elements marked `data-ab` are
// what the artboard harness counts. The Latest results card is a slot the page fills from its own lazy chunk.
import type { ReactNode } from "react";
import { datasetViewPath } from "../router/dataPaths";
import { CopyCode } from "../snippets/CopyCode";
import type { HomeBody } from "./data";
import {
  clearFacets,
  EMPTY_QUERY,
  type FacetId,
  type HomeQuery,
  isNarrowed,
  type Tier,
  toggleFacet,
} from "./facets";
import {
  type FacetGroup,
  type FacetOption,
  type HomeModel,
  homeModel,
  type RailEntry,
  type RegionSegment,
  type StudyRow,
} from "./model";

type OnQuery = (query: HomeQuery) => void;

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function TierSwitch({
  model,
  tier,
  onSelect,
}: {
  model: HomeModel;
  tier: Tier;
  onSelect: (t: Tier) => void;
}) {
  const items: { id: Tier; label: string; count: string }[] = [
    { id: "curated", label: "Curated studies", count: String(model.tiers.curated) },
    { id: "mine", label: "Mine", count: plural(model.tiers.mine, "upload", "uploads") },
    { id: "sandbox", label: "Sandbox", count: `${model.tiers.sandbox} · hidden` },
  ];
  return (
    <div className="home-tiers">
      <small className="home-label">Tier</small>
      {items.map((i) => (
        <button
          key={i.id}
          type="button"
          className={`home-tier home-tier-${i.id}`}
          aria-pressed={tier === i.id}
          onClick={() => onSelect(i.id)}
        >
          <span>{i.label}</span>
          <span className="home-count">{i.count}</span>
        </button>
      ))}
    </div>
  );
}

/** Options shown before the rest fold under "n more"; a chosen option is always shown. */
const FACET_SHOWN = 6;

function FacetList({
  group,
  query,
  onQuery,
}: {
  group: FacetGroup;
  query: HomeQuery;
  onQuery: OnQuery;
}) {
  const chosen = query[group.id];
  const shown = group.options.filter((o, i) => i < FACET_SHOWN || chosen.includes(o.value));
  const rest = group.options.filter((o) => !shown.includes(o));
  const chip = (o: FacetOption) => (
    <li key={o.value}>
      <button
        type="button"
        className="home-chip-facet"
        data-ab="facet-chip"
        aria-pressed={chosen.includes(o.value)}
        data-facet={group.id}
        data-value={o.value}
        title={o.label}
        onClick={() => onQuery(toggleFacet(query, group.id, o.value))}
      >
        {o.cssVar === undefined ? null : (
          <span className="home-swatch" style={{ background: o.cssVar }} />
        )}
        <span className="home-facet-name">{o.label}</span>
        <span className="home-count">{o.count}</span>
      </button>
    </li>
  );
  return (
    <>
      <ul className="home-facet-opts">{shown.map(chip)}</ul>
      {rest.length === 0 ? null : (
        <details className="home-facet-more">
          <summary>{`${rest.length} more`}</summary>
          <ul className="home-facet-opts">{rest.map(chip)}</ul>
        </details>
      )}
    </>
  );
}

/**
 * The facets as chips: a list in the left column at desktop width, wrapped chips on a phone. Each chip
 * is a toggle with the number of curated studies that have it; values of one facet widen, facets narrow.
 */
function Facets({
  groups,
  query,
  onQuery,
}: {
  groups: readonly FacetGroup[];
  query: HomeQuery;
  onQuery: OnQuery;
}) {
  const sections: { title: string; ids: readonly FacetId[] }[] = [
    { title: "Modality", ids: ["modality"] },
    { title: "Region", ids: ["region"] },
    { title: "Species · Archive", ids: ["species", "archive"] },
  ];
  return (
    <div className="home-facets" data-testid="home-facets">
      {sections.map((s) => (
        <section key={s.title} className="home-facet" aria-label={s.title}>
          <small className="home-label">{s.title}</small>
          {groups
            .filter((g) => s.ids.includes(g.id))
            .map((g) => (
              <FacetList key={g.id} group={g} query={query} onQuery={onQuery} />
            ))}
        </section>
      ))}
    </div>
  );
}

function RegionBar({ regions }: { regions: readonly RegionSegment[] }) {
  if (regions.length === 0) return null;
  return (
    <div className="home-bar" role="img" aria-label="Regions recorded, by units">
      {regions.map((r) => (
        <span
          key={r.label}
          className="home-seg"
          title={r.label}
          data-ab="region-seg"
          style={{ flexGrow: r.share, background: r.cssVar }}
        />
      ))}
    </div>
  );
}

/** Every region's label beside its colour, on one wrapped line under the row. */
function RegionLegend({ regions }: { regions: readonly RegionSegment[] }) {
  return (
    <ul className="home-regions">
      {regions.map((r) => (
        <li key={r.label}>
          <span className="home-swatch" style={{ background: r.cssVar }} />
          {r.label}
          {r.units > 0 ? <small> {r.units}</small> : null}
        </li>
      ))}
    </ul>
  );
}

function Row({ row, model }: { row: StudyRow; model: HomeModel }) {
  return (
    <>
      <tr data-ab="study-row" data-testid="study-row" className="home-row">
        <td className="home-study">
          <h3 className="home-name">
            <a href={row.href} title={row.title}>
              {row.title}
            </a>
          </h3>
          <p className="home-liner">
            <span className="home-meta">{row.meta}</span> · {row.oneLiner}
          </p>
          <ul className="home-releases">
            {row.releases.map((r) => (
              <li key={r.dataset}>
                <a className="home-release" href={r.href}>
                  {r.text}
                </a>
                {r.license === null ? null : <small> {r.license}</small>}
              </li>
            ))}
          </ul>
        </td>
        {model.columns.map((c) => {
          const n = row.cells[c.id];
          return (
            <td key={c.id} className="home-cell" data-label={c.label}>
              {n === undefined ? (
                <span className="home-chip home-chip-empty" title={`No ${c.label}`}>
                  –
                </span>
              ) : (
                <span className="home-chip" title={`${n} with ${c.label}`}>
                  {n}
                </span>
              )}
            </td>
          );
        })}
        <td className="home-regions-cell">
          <RegionBar regions={row.regions} />
          <small className="home-meta">{row.regionSummary}</small>
        </td>
      </tr>
      <tr className="home-legend-row">
        <td colSpan={model.columns.length + 2}>
          <RegionLegend regions={row.regions} />
        </td>
      </tr>
    </>
  );
}

function Matrix({
  model,
  query,
  onQuery,
}: {
  model: HomeModel;
  query: HomeQuery;
  onQuery: OnQuery;
}) {
  if (model.rows.length === 0 && isNarrowed(query)) {
    return (
      <p className="home-empty" data-testid="home-no-match">
        No study matches these facets.{" "}
        <button type="button" className="home-clear" onClick={() => onQuery(clearFacets(query))}>
          Clear facets
        </button>
      </p>
    );
  }
  if (model.rows.length === 0) {
    return <p className="home-empty">No curated studies yet.</p>;
  }
  return (
    <div className="home-scroll">
      <table className="home-matrix" aria-label="Studies by modality">
        <thead>
          <tr>
            <th scope="col">Study · archives</th>
            {model.columns.map((c) => (
              <th key={c.id} scope="col" className="home-col" data-ab="matrix-col">
                {c.label}
              </th>
            ))}
            <th scope="col">Regions recorded</th>
          </tr>
        </thead>
        <tbody>
          {model.rows.map((r) => (
            <Row key={r.id} row={r} model={model} />
          ))}
        </tbody>
      </table>
      <small className="home-note">
        Cells count participants with that modality. Filled = present.
      </small>
    </div>
  );
}

function MineSection({ model }: { model: HomeModel }) {
  return (
    <div className="home-mine" data-ab="mine">
      <small className="home-label">Mine · your uploads</small>
      {model.mine.length === 0 ? (
        <p className="home-empty" data-ab="mine-empty">
          No uploads of yours yet
        </p>
      ) : (
        <ul className="home-uploads">
          {model.mine.map((d) => (
            <li key={d.dataset} data-ab="mine-row">
              <a href={datasetViewPath(d.dataset)}>{d.dataset}</a>
              <span>{d.title ?? ""}</span>
              <small>{d.created_at ?? ""}</small>
            </li>
          ))}
        </ul>
      )}
      {model.others.length === 0 ? null : (
        <p className="home-others" data-ab="others-uploads">
          {plural(model.others.length, "upload", "uploads")} from other researchers:{" "}
          {model.others.map((d) => d.title ?? d.dataset).join(", ")}
        </p>
      )}
    </div>
  );
}

function SandboxList({ datasets }: { datasets: readonly string[] }) {
  return (
    <ul className="home-sandbox-list">
      {datasets.map((d) => (
        <li key={d}>
          <a href={datasetViewPath(d)}>{d}</a>
        </li>
      ))}
    </ul>
  );
}

function SandboxFold({ model }: { model: HomeModel }) {
  const s = model.sandbox;
  if (s.count === 0) return null;
  return (
    <details className="home-sandbox" data-ab="sandbox-fold">
      <summary>
        <strong>{plural(s.count, "sandbox dataset", "sandbox datasets")} hidden</strong>
        <span className="home-prefixes">
          {" "}
          · test and SDK leftovers: {s.prefixes.map((p) => `${p.prefix}-… ${p.count}`).join(", ")}
        </span>
        <span className="home-toggle"> Show</span>
      </summary>
      <SandboxList datasets={s.datasets} />
    </details>
  );
}

function Mark({ line }: { line: RailEntry["line"] }) {
  if (line === "dashed") return <span className="home-link home-link-pending">link pending</span>;
  if (line === "solid")
    return <span className="home-link home-link-confirmed">link confirmed</span>;
  return <span className="home-link-none">same dataset</span>;
}

function Rail({ rail }: { rail: readonly RailEntry[] }) {
  return (
    <section className="home-rail" aria-label="Participants with MRI and ephys">
      <small className="home-label">Participants with MRI and ephys</small>
      <h2>{plural(rail.length, "participant", "participants")}</h2>
      {rail.length === 0 ? (
        <p className="home-empty">No participant has both MRI and ephys yet.</p>
      ) : (
        <ul className="home-rail-list">
          {rail.map((e) => (
            <li key={e.key} data-ab="rail-entry" data-ab-line={e.line}>
              <div className="home-rail-head">
                <strong>{e.name}</strong>
                <Mark line={e.line} />
              </div>
              <p>{e.detail}</p>
            </li>
          ))}
        </ul>
      )}
      <p className="home-note">
        A link between two archives is a curator&apos;s claim that two subjects are one person.
        Until it is confirmed it is drawn dashed.
      </p>
    </section>
  );
}

export function HomeView({
  body,
  query = EMPTY_QUERY,
  onQuery = () => undefined,
  latest = null,
}: {
  body: HomeBody;
  query?: HomeQuery;
  onQuery?: OnQuery;
  /** The Latest results card (its own lazy chunk, `latest.tsx`); null draws none. */
  latest?: ReactNode;
}) {
  const tier = query.tier;
  const model = homeModel(body, query);
  const narrowed = isNarrowed(query);
  return (
    <section className="home" data-testid="home-page">
      <div className="home-side">
        {latest}
        <TierSwitch model={model} tier={tier} onSelect={(t) => onQuery({ ...query, tier: t })} />
        {tier === "curated" ? (
          <Facets groups={model.facets} query={query} onQuery={onQuery} />
        ) : null}
      </div>
      <div className="home-main">
        <CopyCode page="home" values={{}} />
        {tier === "curated" ? (
          <>
            <h2 className="home-title">
              {narrowed
                ? `${model.rows.length} of ${plural(model.tiers.curated, "curated study", "curated studies")}`
                : plural(model.tiers.curated, "curated study", "curated studies")}
            </h2>
            {model.rows.length === 0 ? null : <p className="home-summary">{model.summary}</p>}
            <Matrix model={model} query={query} onQuery={onQuery} />
            <MineSection model={model} />
            <SandboxFold model={model} />
          </>
        ) : null}
        {tier === "mine" ? (
          <>
            <h2 className="home-title">Your uploads</h2>
            <MineSection model={model} />
          </>
        ) : null}
        {tier === "sandbox" ? (
          <>
            <h2 className="home-title">
              {plural(model.sandbox.count, "sandbox dataset", "sandbox datasets")}
            </h2>
            <p className="home-summary">Test and SDK leftovers, hidden from the studies.</p>
            <SandboxList datasets={model.sandbox.datasets} />
          </>
        ) : null}
      </div>
      <Rail rail={model.rail} />
    </section>
  );
}
