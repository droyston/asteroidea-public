"""The architecture diagram is current, covers the registry and reads cleanly.

The overview SVG must equal what `architecture` renders now; every registry function
and every routed function is a node; every curated edge names nodes and ports that
exist; the queue map covers every event source; no two boxes overlap. The overview
groups the model: every function and every storage resource shows once, and every
model edge is drawn or listed in its caller's reads line. Each use-case SVG is current
and reads cleanly, and its steps are model edges, in order, or marked free text.
"""

import dataclasses
import html
import re
from collections import Counter

import pytest

from asteroidea_infra import architecture, functions
from asteroidea_ingest import routes


def test_the_overview_is_current() -> None:
    committed = architecture.OVERVIEW.read_text(encoding="utf-8")
    assert architecture.render_overview() == committed, architecture.STALE


def test_every_function_spec_is_a_node() -> None:
    keys = set(architecture.nodes())
    missing = [
        s.construct_id for s in functions.FUNCTIONS if s.construct_id not in keys
    ]
    assert missing == []


def test_every_routed_function_is_a_node() -> None:
    keys = set(architecture.nodes())
    missing = sorted({r.function for r in routes.ROUTES} - keys)
    assert missing == []


def test_every_edge_names_existing_nodes_and_ports() -> None:
    all_nodes = architecture.nodes()
    bad = [
        f"{e.src} -> {e.dst} {e.port}"
        for e in architecture.EDGES
        if e.src not in all_nodes
        or e.dst not in all_nodes
        or (e.port and e.port not in {p.key for p in all_nodes[e.dst].ports})
    ]
    assert bad == []


def test_every_edge_is_a_read_or_a_write_with_a_basis() -> None:
    bad = [
        f"{e.src} -> {e.dst}"
        for e in architecture.EDGES
        if e.verb not in {architecture.READ, architecture.WRITE} or not e.basis
    ]
    assert bad == []


def test_the_queue_map_covers_every_event_source() -> None:
    all_nodes = architecture.nodes()
    fed = {
        s.construct_id: architecture.queue_of(s)
        for s in functions.FUNCTIONS
        if s.queue is not None
    }
    assert fed == {
        "CommitFunction": "CommitQueue",
        "ExtractFunction": "ExtractQueue",
        "LfpBuildFunction": "LfpBuildQueue",
    }
    assert all(all_nodes[q].kind == architecture.QUEUE for q in fed.values())
    # Each consumer reads its queue on the diagram.
    consumes = {
        (e.src, e.dst) for e in architecture.EDGES if e.verb == architecture.READ
    }
    assert all((fn, q) in consumes for fn, q in fed.items())


def test_no_two_node_boxes_overlap() -> None:
    boxes = architecture.layout(architecture.blocks())
    keys = sorted(boxes)
    overlaps = [
        (a, b)
        for i, a in enumerate(keys)
        for b in keys[i + 1 :]
        if boxes[a].overlaps(boxes[b])
    ]
    assert overlaps == []


def test_every_box_is_placed_once() -> None:
    a = architecture
    placed = [k for column in a.ORDER for k in column]
    grouped = {m for members in a.group_members().values() for m in members}
    expected = {g.key for g in a.GROUPS} | (set(a.nodes()) - grouped)
    assert sorted(placed) == sorted(expected)


def _shown() -> Counter[str]:
    """Count each model node the overview shows, as a box or as a bullet."""
    shown: Counter[str] = Counter()
    for key, block in architecture.blocks().items():
        if block.members:
            shown.update(m.key for m in block.members)
        else:
            shown[key] += 1
    return shown


def test_every_function_spec_appears_once() -> None:
    shown = _shown()
    counts = {s.construct_id: shown[s.construct_id] for s in functions.FUNCTIONS}
    assert counts == {s.construct_id: 1 for s in functions.FUNCTIONS}


def test_every_storage_resource_appears_once() -> None:
    shown = _shown()
    storage = [
        k
        for k, n in architecture.nodes().items()
        if n.kind in architecture.STORAGE_KINDS
    ]
    assert {k: shown[k] for k in storage} == dict.fromkeys(storage, 1)
    # Nothing else is lost or doubled either: every model node shows exactly once.
    assert shown == Counter(list(architecture.nodes()))


def test_the_api_bullets_follow_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = dataclasses.replace(functions.API_FUNCTIONS[0], construct_id="FakeFunction")
    monkeypatch.setattr(functions, "API_FUNCTIONS", (*functions.API_FUNCTIONS, fake))
    monkeypatch.setattr(functions, "FUNCTIONS", (*functions.FUNCTIONS, fake))
    group = architecture.blocks()[architecture.API_GROUP]
    bullets = [m.key for m in group.members]
    assert bullets == [s.construct_id for s in functions.API_FUNCTIONS]
    assert _shown()["FakeFunction"] == 1


def test_every_edge_reaches_both_endpoints() -> None:
    boxes, routed = architecture.overview_routes()
    rows = architecture.spans(architecture.blocks(), boxes, architecture.links())

    def on_border(point: tuple[float, float], key: str, member: str) -> bool:
        x, y = point
        b = boxes[key]
        inside_x, inside_y = b.x - 1 <= x <= b.right + 1, b.y - 1 <= y <= b.bottom + 1
        on_x = min(abs(x - b.x), abs(x - b.right)) < 1
        on_y = min(abs(y - b.y), abs(y - b.bottom)) < 1
        if member:
            # An end on a bullet lands on a side, within that bullet's row.
            top, bottom = rows[key, member]
            return on_x and top < y < bottom
        return (on_x and inside_y) or (on_y and inside_x)

    assert [r.link for r in routed] == list(architecture.links())
    missed = [
        f"{r.link.src} -> {r.link.dst}"
        for r in routed
        if not on_border(r.points[0], r.link.src, r.link.src_member)
        or not on_border(r.points[-1], r.link.dst, r.link.dst_member)
    ]
    assert missed == []


def _carried(svg: str, verb: str = r"\w+") -> set[str]:
    """Return the model edges the drawn polylines of `verb` carry, as their titles."""
    titles = re.findall(rf'<polyline class="e {verb}"[^>]*><title>([^<]*)</title>', svg)
    return {part for t in titles for part in html.unescape(t).split("; ")}


def _title(e: architecture.Edge) -> str:
    return f"{e.src} {e.verb}s {e.dst}{' ' + e.port + '/' if e.port else ''}: {e.basis}"


def _notes(svg: str) -> dict[str, str]:
    """Return the muted text of each box and bullet, keyed by its model node."""
    out: dict[str, str] = {}
    for key, body in re.findall(r"<g><title>(\w+)</title>(.*?)</g>", svg, re.DOTALL):
        lines = re.findall(r'<text[^>]*class="muted"[^>]*>([^<]*)</text>', body)
        out[key] = " ".join(html.unescape(t) for t in lines)
    return out


def test_every_model_edge_is_drawn_or_listed() -> None:
    a = architecture
    svg = a.render_overview()
    carried, notes = _carried(svg), _notes(svg)
    labels = {
        html.unescape(t)
        for t in re.findall(r'<text[^>]*class="elabel"[^>]*>([^<]*)</text>', svg)
    }
    all_nodes, places = a.nodes(), a.where()
    link_of = {e: link for link in a.links() for e in link.edges}
    lost: list[str] = []
    for e in a.EDGES:
        if _title(e) in carried:
            link = link_of[e]
            # A group end names its member: by ending on its bullet, or in the label.
            ends = (
                (places[e.src][1], link.src_member, a.SHORT.get(e.src, e.src)),
                (places[e.dst][1], link.dst_member, a.token(e, all_nodes)),
            )
            lost += [
                f"{e.src} -> {e.dst}: {member} is not named"
                for member, anchor, name in ends
                if member
                and anchor != member
                and (name not in link.label.split(", ") or link.label not in labels)
            ]
            continue
        text = notes.get(e.src, "")
        prefix = f"{a.note_prefix(e)}:"
        listed = text[text.find(prefix) :] if prefix in text else ""
        if a.token(e, all_nodes) not in listed:
            lost.append(f"{e.src} -> {e.dst}: neither drawn nor listed")
    assert lost == []


def test_the_overview_draws_no_read_but_the_exceptions() -> None:
    a = architecture
    all_nodes, places = a.nodes(), a.where()
    by_title = {_title(e): e for e in a.EDGES}
    reads = {by_title[t] for t in _carried(a.render_overview(), a.READ)}
    assert all(e.verb == a.READ for e in reads)

    def by_rule(e: architecture.Edge) -> bool:
        src, dst = all_nodes[e.src].kind, all_nodes[e.dst].kind
        return (
            (src == a.ACTOR and dst == a.EDGE_SERVICE)
            or e.label == "assume"
            or dst == a.QUEUE
        )

    excepted = {(e.src, e.dst) for e in reads if not by_rule(e)}
    assert excepted == set(a.READ_EXCEPTIONS)
    # Each exception joins two boxes that no other kind of model edge joins.
    for src, dst in excepted:
        pair = {places[src][0], places[dst][0]}
        between = {
            (e.src, e.dst)
            for e in a.EDGES
            if {places[e.src][0], places[e.dst][0]} == pair
        }
        assert between <= set(a.READ_EXCEPTIONS)


def test_the_overview_reads_cleanly() -> None:
    assert architecture.overview_problems() == []


def test_the_overview_text_is_at_least_11px() -> None:
    svgs = [architecture.render_overview(), *map(architecture.render_flow, FLOWS)]
    for svg in svgs:
        sizes = {int(s) for s in re.findall(r"font-size: (\d+)px", svg)}
        assert min(sizes) >= 11


FLOWS = architecture.FLOWS


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f.name)
def test_every_flow_svg_is_current(flow: architecture.Flow) -> None:
    committed = architecture.flow_path(flow).read_text(encoding="utf-8")
    assert architecture.render_flow(flow) == committed, architecture.STALE


def test_the_svgs_on_disk_are_the_overview_and_the_flows() -> None:
    on_disk = sorted(p.stem for p in architecture.OUT_DIR.glob("*.svg"))
    assert on_disk == sorted(["overview", *(f.name for f in FLOWS)])


def test_every_flow_step_is_a_model_edge_or_marked_free_text() -> None:
    a = architecture
    bad: list[str] = []
    for flow in FLOWS:
        lines = a.step_lines(flow)
        for n, step in enumerate(flow.steps, 1):
            key = (step.src, step.dst, step.verb)
            matches = [e for e in a.EDGES if (e.src, e.dst, e.verb) == key]
            free = key == ("", "", "")
            if not free and len(matches) != 1:
                bad.append(f"{flow.name} step {n}: {len(matches)} model edges")
            if free and "[no AWS edge]" not in lines[n - 1][0]:
                bad.append(f"{flow.name} step {n}: free text is not marked")
    assert bad == []


def test_every_flow_runs_in_order() -> None:
    a = architecture
    assert [p for f in FLOWS for p in a.order_problems(f, a.nodes())] == []
    # A named jump must be a real one: without it, its step is flagged.
    for flow in FLOWS:
        bare = dataclasses.replace(flow, jumps=())
        flagged = {
            int(p.split(" step ")[1].split()[0])
            for p in a.order_problems(bare, a.nodes())
        }
        assert flagged == {n for n, _ in flow.jumps}, flow.name


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f.name)
def test_every_flow_reads_cleanly(flow: architecture.Flow) -> None:
    assert architecture.flow_problems(flow) == []
