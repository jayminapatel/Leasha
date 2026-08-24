"""Turning stored entities and edges into something a person can look at.

Layer: L6

Two outputs, because they answer different questions.

`to_networkx` and `metrics` produce **numbers**: which entities sit at the centre
of the corpus, which cluster together, which bridge two clusters that otherwise
never touch. That last one is usually the interesting finding, and it is invisible
in any list sorted by frequency.

`render_html` produces a **picture** - a standalone pyvis page opened in the
default browser.

**Why the browser and not a panel in the app.** Embedding it would need
`PyQt6-WebEngine`: a separate ~150MB dependency, a second Chromium inside a
process that already holds an ONNX runtime and two databases, and a well-known
source of Qt plugin problems at packaging time. The graph is something you look
at occasionally, not something you search from, so it does not earn that.

The interactive half of the feature - click an entity, see its neighbours, see
the passages behind it, search it - is served by `neighbourhood()` and
`SqliteStore.chunks_mentioning()` in ordinary Qt widgets, which is where it
belongs anyway: those are the operations someone repeats, and a table does them
faster than a force-directed layout ever will.

**Everything here caps before it draws.** A force-directed layout of 50,000 nodes
is not a slow picture, it is a grey rectangle - and it takes minutes to produce.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.core.errors import AppErrorException, make_error

__all__ = [
    "GraphView",
    "to_networkx",
    "metrics",
    "select_top",
    "neighbourhood",
    "render_html",
    "MAX_RENDER_NODES",
    "KIND_COLOURS",
]

#: The spec's number, and it is about the layout engine rather than the data:
#: beyond roughly this many nodes a force-directed graph is a grey disc, and the
#: physics simulation stops converging in reasonable time. Above it, the top
#: nodes by document count are kept and the rest summarised.
MAX_RENDER_NODES = 5000

#: Colour by kind rather than by cluster. Clusters change every time the corpus
#: grows, so a colour that means "cluster 3" means something different next week;
#: "this is an email address" is stable.
KIND_COLOURS = {
    "name": "#4c78a8",
    "acronym": "#f58518",
    "email": "#54a24b",
    "file": "#b279a2",
    "person": "#4c78a8",
    "org": "#e45756",
    "project": "#72b7b2",
    "system": "#ff9da6",
}
_DEFAULT_COLOUR = "#9c9c9c"


@dataclass(frozen=True, slots=True)
class GraphView:
    """A bounded slice of the graph, ready to draw or measure."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    truncated_from: int = 0

    @property
    def truncated(self) -> bool:
        return self.truncated_from > len(self.nodes)


def select_top(
    entities: Sequence[Mapping[str, Any]],
    edges_for: Any,
    *,
    limit: int = MAX_RENDER_NODES,
) -> GraphView:
    """The `limit` best-connected entities and every edge *between* them.

    `entities` is expected already sorted by importance (`top_entities` sorts by
    document count), and `edges_for` is a callable taking a list of ids.

    Edges to entities outside the selection are dropped rather than drawn to a
    stub node. A stub is a lie in two directions at once: it shows a relationship
    to something unnamed, and it makes the node look better connected than the
    picture can justify.
    """
    chosen = list(entities)[:limit]
    ids = [int(row["id"]) for row in chosen]
    raw_edges = edges_for(ids) if len(ids) >= 2 else []
    return GraphView(
        nodes=[dict(row) for row in chosen],
        edges=[dict(row) for row in raw_edges],
        truncated_from=len(entities),
    )


def to_networkx(view: GraphView):
    """Build an `nx.Graph`. Imported lazily so a missing pyvis cannot break search."""
    nx = _require("networkx")
    graph = nx.Graph()
    for node in view.nodes:
        graph.add_node(
            int(node["id"]),
            label=node.get("display", ""),
            kind=node.get("kind", "name"),
            mentions=int(node.get("mentions", 0)),
            doc_count=int(node.get("doc_count", 0)),
        )
    for edge in view.edges:
        a_id, b_id = int(edge["a_id"]), int(edge["b_id"])
        if a_id in graph and b_id in graph:
            graph.add_edge(
                a_id, b_id,
                weight=int(edge.get("weight", 1)),
                pmi=float(edge.get("pmi") or 0.0),
            )
    return graph


def metrics(graph, *, top: int = 20) -> dict[str, Any]:
    """Centrality, bridges and clusters, with the expensive parts sampled.

    Betweenness is the one worth having and the one that costs: exact betweenness
    is O(nodes x edges), which on 5,000 nodes is minutes. `k` samples the source
    nodes instead, giving a ranking that is right about which entities *bridge*
    clusters while being wrong in the third decimal place - and the ranking is
    the entire question being asked.
    """
    nx = _require("networkx")
    if graph.number_of_nodes() == 0:
        return {"nodes": 0, "edges": 0, "central": [], "bridges": [], "communities": []}

    labels = {node: graph.nodes[node].get("label", str(node)) for node in graph}
    degree = nx.degree_centrality(graph)
    sample = min(graph.number_of_nodes(), 200)
    between = nx.betweenness_centrality(graph, k=sample, weight=None, seed=7)

    try:
        raw_communities = nx.community.greedy_modularity_communities(graph, weight="weight")
    except Exception:
        # Community detection is a nice-to-have on top of a graph that is already
        # useful; a disconnected or degenerate graph must not fail the whole view.
        raw_communities = []

    communities = [
        sorted((labels[node] for node in group), key=str.casefold)[:12]
        for group in sorted(raw_communities, key=len, reverse=True)[:10]
    ]

    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "density": round(nx.density(graph), 5),
        "central": _rank(degree, labels, top),
        "bridges": _rank(between, labels, top),
        "communities": communities,
    }


def _rank(scores: Mapping[int, float], labels: Mapping[int, str], top: int):
    ordered = sorted(scores.items(), key=lambda item: (-item[1], labels.get(item[0], "")))
    return [
        {"id": node, "label": labels.get(node, str(node)), "score": round(score, 5)}
        for node, score in ordered[:top]
        if score > 0
    ]


def neighbourhood(
    entity_id: int,
    edges: Sequence[Mapping[str, Any]],
    labels: Mapping[int, str],
    *,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """What one entity is connected to, strongest first.

    This is the interactive path - a click on a node, or a row in a table - and
    it is a plain function over rows so the UI can call it without a layout
    engine, a browser, or anything that can fail.
    """
    found: list[dict[str, Any]] = []
    for edge in edges:
        a_id, b_id = int(edge["a_id"]), int(edge["b_id"])
        if entity_id not in (a_id, b_id):
            continue
        other = b_id if a_id == entity_id else a_id
        found.append({
            "id": other,
            "label": labels.get(other, str(other)),
            "weight": int(edge.get("weight", 0)),
            "pmi": float(edge.get("pmi") or 0.0),
        })
    found.sort(key=lambda row: (-row["pmi"], -row["weight"], row["label"]))
    return found[:limit]


def render_html(view: GraphView, out_path: Path, *, title: str = "Knowledge graph") -> Path:
    """Write a standalone interactive page. Returns the path written.

    `notebook=False` and `cdn_resources="in_line"` together are what make the
    file **work offline**. pyvis defaults to loading vis-network from a CDN,
    which in an application whose entire promise is "fully local" would mean the
    graph silently fails to render on a machine with no internet - and renders
    fine on the developer's, which is the worst possible way for it to break.
    """
    pyvis = _require("pyvis")
    from pyvis.network import Network  # noqa: PLC0415 - lazy on purpose

    del pyvis
    out_path.parent.mkdir(parents=True, exist_ok=True)

    net = Network(
        height="900px", width="100%", bgcolor="#1e1e1e", font_color="#e8e8e8",
        notebook=False, directed=False, cdn_resources="in_line",
    )
    # Repulsion tuned for a few thousand nodes; the defaults assume a few dozen
    # and produce a tangle that never settles.
    net.barnes_hut(gravity=-12000, central_gravity=0.25, spring_length=140, spring_strength=0.02)

    weights = [int(node.get("doc_count", 0)) for node in view.nodes] or [1]
    largest = max(weights) or 1

    for node in view.nodes:
        docs = int(node.get("doc_count", 0))
        # Log scale: on a real corpus the top entity appears in thousands of
        # documents and the median in three. Linear sizing gives one balloon and
        # 4,999 dots.
        size = 8 + 26 * (math.log1p(docs) / math.log1p(largest))
        label = str(node.get("display", ""))
        net.add_node(
            int(node["id"]),
            label=label,
            title=(
                f"{html.escape(label)}\n"
                f"{node.get('kind', '')} · {docs} documents · "
                f"{int(node.get('mentions', 0))} mentions"
            ),
            color=KIND_COLOURS.get(str(node.get("kind", "")), _DEFAULT_COLOUR),
            size=round(size, 1),
        )

    present = {int(node["id"]) for node in view.nodes}
    for edge in view.edges:
        a_id, b_id = int(edge["a_id"]), int(edge["b_id"])
        if a_id in present and b_id in present:
            pmi = float(edge.get("pmi") or 0.0)
            net.add_edge(
                a_id, b_id,
                value=int(edge.get("weight", 1)),
                title=f"together in {edge.get('weight', 0)} passages · npmi {pmi:.2f}",
                color={"opacity": max(0.15, min(1.0, 0.2 + pmi))},
            )

    # NOT net.write_html(). It calls open(path, "w+") with no encoding, so the
    # file is written in the process's locale encoding - cp1252 on a UK/US
    # Windows install. The page contains ’ — · … from entity names and from the
    # tooltips built above, and cp1252 cannot encode them, so it raises
    # UnicodeEncodeError after building the whole document. Nothing on Linux or
    # macOS shows this: their default is already UTF-8.
    #
    # Generating the string and writing it here with an explicit encoding is the
    # fix, and it also removes the need to re-read the file in `_finalise`.
    _write(out_path, _clean(net.generate_html(notebook=False), view, title))
    return out_path


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


#: pyvis emits these two even with `cdn_resources="in_line"` - they are for the
#: filter widgets, which this page does not use. Verified by rendering a graph
#: and grepping the output, not by reading the pyvis docs, which say otherwise.
_CDN_TAGS = (
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.0.0-beta3'
    '/dist/css/bootstrap.min.css" integrity="sha384-eOJMYsd53ii+scO/bJGFsiCZc+5NDVN2yr8+0RDqr0Ql0h+rP48ckxlpbzKgwra6"'
    ' crossorigin="anonymous">',
    '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.0.0-beta3/dist/js/bootstrap.bundle.min.js"'
    ' integrity="sha384-JEW9xMcG8R+pH31jmWH6WWP0WintQrMb4s7ZOdauHnUtxwoG2vI5DkLtS3qm9Ekf"'
    ' crossorigin="anonymous"></script>',
)


def _clean(text: str, view: GraphView, title: str) -> str:
    """Add the caveat banner, cut the CDN tags, and declare UTF-8 on the page.

    **The CDN tags are the important part.** `cdn_resources="in_line"` is supposed
    to inline everything, and it inlines vis-network - but it still emits a
    stylesheet and a script tag pointing at jsdelivr for Bootstrap. On a machine
    with no internet those requests hang and then fail, on a page whose whole
    premise is that nothing leaves the machine. Found by rendering a graph and
    grepping for `https://`, which is the only way it was going to be found: on a
    connected developer machine the page looks perfect.

    The banner states that the view is capped and by how much. A truncated graph
    that does not say so is actively misleading - a missing node reads as
    evidence that nothing connects there.
    """
    for tag in _CDN_TAGS:
        text = text.replace(tag, "")
    # Belt and braces: if pyvis changes the exact attributes above, drop any
    # remaining jsdelivr tag rather than shipping a page that phones out.
    text = re.sub(
        r'<(?:link|script)[^>]*https://cdn\.jsdelivr\.net[^>]*>(?:</script>)?', "", text
    )

    note = ""
    if view.truncated:
        note = (
            f" &mdash; showing the {len(view.nodes):,} most-cited of "
            f"{view.truncated_from:,} entities"
        )
    banner = (
        '<div style="font:14px system-ui;color:#e8e8e8;background:#1e1e1e;'
        'padding:12px 16px;border-bottom:1px solid #333">'
        f"<strong>{html.escape(title)}</strong>{note}"
        f" &middot; {len(view.edges):,} connections"
        "</div>"
    )
    text = text.replace("<body>", "<body>" + banner, 1)

    # The file is written as UTF-8, so the page must say so. pyvis emits a
    # `<meta charset="utf-8">`; if a future version stops, a browser guessing the
    # encoding renders every apostrophe in every entity name as mojibake.
    if "charset" not in text[:2000].lower():
        text = text.replace("<head>", '<head>\n<meta charset="utf-8">', 1)
    return text


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise AppErrorException(make_error(
            "ERR_UNEXPECTED", "graph.render",
            details=f"{module} is not installed, so the graph cannot be drawn.",
            suggestion=(
                f"Run: venv\\Scripts\\python.exe -m pip install {module}\n"
                "Entities and connections are already stored; only the picture is missing, "
                "and 'app.cli graph --top' still lists them."
            ),
        )) from exc


def summarise(view: GraphView, labels: Optional[Mapping[int, str]] = None) -> Iterable[str]:
    """A text description of the graph, for the CLI and for logs.

    Exists because the picture is the *optional* half. Somebody on a machine
    without pyvis, or reading a log after the fact, still needs to know what the
    graph found.
    """
    yield f"{len(view.nodes):,} entities, {len(view.edges):,} connections"
    if view.truncated:
        yield f"(capped from {view.truncated_from:,} entities)"
    by_kind: dict[str, int] = {}
    for node in view.nodes:
        by_kind[str(node.get("kind", "?"))] = by_kind.get(str(node.get("kind", "?")), 0) + 1
    for kind, count in sorted(by_kind.items(), key=lambda item: -item[1]):
        yield f"  {kind:10} {count:,}"
