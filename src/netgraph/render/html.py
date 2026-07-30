"""One interactive HTML page, self-contained, with nothing to fetch.

``-f svg`` already produces a diagram that answers questions — it carries a
tooltip, a link and an id per element — but a browser can do more with an
inventory than pop a native tooltip over it, and ``netgraph web`` cannot be
handed to anybody: it is an editor for a document stream, backed by a Python
process on the user's own machine. This format is the artefact in between: one
``.html`` file to email, commit next to the YAML, publish to GitHub Pages or
open from a ``file://`` URL, that pans, zooms, searches and explains itself.

Self-contained means what it says
---------------------------------

The page makes **no network requests of any kind**. There is no CDN, no
stylesheet, no font, no script and no image URL in it: the style sheet and the
client are files in this package
(``netgraph/render/assets/page.css``, ``page.js`` and the ``detail.js`` the web
preview also serves) inlined at render time, the diagram is an inline ``<svg>``,
and an ``--icons`` theme is already a ``data:`` URI by the time it gets here.
The only URLs a page can hold are the ones ``--link-template`` was asked for,
and those are links a reader clicks, not resources the page loads.

That is enforced rather than promised: the document ships its own strict
:func:`Content-Security-Policy <policy>` in a ``<meta>``, built from the
SHA-256 of each inline block, so a page that grew a fetch would be refused by
the browser rather than quietly making one. Nothing in the client needs ``eval``
or ``new Function``, and no markup is ever assigned from a record — see
``page.js``.

Not a second renderer
---------------------

Everything on the page is something netgraph already produces:

* the picture is the SVG :func:`~netgraph.render.dot.to_image` lays out, with
  ``--element-ids`` forced on (the ids are how a shape and a record find each
  other) and made embeddable by :mod:`netgraph.render.fragment`;
* the records are :func:`~netgraph.render.details.build_details`, which are the
  ``-f json`` export plus an element id and a links cross-reference, so the
  detail panel cannot disagree with ``netgraph render -f json``;
* how a record is *drawn* is ``detail.js``, shared with ``netgraph web``.

What the page adds is navigation: pan and zoom, a search over every name,
address, MAC and VLAN, a detail panel, a deep-linkable selection, and toggles.

Why several drawings
--------------------

A browser cannot lay a graph out, so a toggle that changes what a label prints
cannot re-flow the diagram: Graphviz decided where every shape goes, and hiding
the addresses inside a node would leave the box the size it was laid out at.
The page therefore embeds one *drawing per view* — every layer, with and
without the address and VLAN annotations — and switching one on is showing a
different, properly laid out picture. Identical drawings are emitted once and shared (an inventory with
no VLAN renders the same with and without them), so the cost is paid only where
there is something to see.

``--show-ips`` and ``--show-vlans`` set the *ceiling* rather than the state: off
means the page holds no drawing that prints them and no record that carries
them, because "do not print the addresses" has to mean all of the printing or
the flag is a trap. On means the page opens with them and can turn them off.

A view costs its drawing, and nothing else
------------------------------------------

A drawing is irreducible — it is a layout, and only Graphviz can produce one —
so a page must grow with the views it holds. What it must *not* do is grow with
them in anything else, and entry 8 of ``docs/follow-ups.md`` measured three
places where it did. Two are in the drawings and are fixed in
:mod:`netgraph.render.fragment`: the repeated font attributes and the repeated
``--icons`` payload, which is now one shared :class:`~netgraph.render.fragment.IconLibrary`
per page. The third is here.

The records used to be written once **per layer**, and a device's record does
not depend on the layer it is drawn at — only the ``links`` cross-reference
does, since which cables are drawn is exactly what a layer decides. So the page
carries two pools and an index into them:

.. code-block:: javascript

    {"records": [ … ],                  // one entry per distinct record
     "links":   [ … ],                  // one entry per distinct link list
     "layers": [{"elements": {"<element id>": [recordIndex, linksIndex]}}]}

``linksIndex`` is ``-1`` for a record that has no ``links`` at all, which is
every edge. A consumer rebuilds what it used to read by looking both up and
putting the links back on the record; ``page.js`` does exactly that, once per
layer, the first time that layer is shown. Nothing about a record itself
changed — it is still the ``-f json`` export plus an element id — so
``detail.js`` and ``netgraph web`` are untouched by this.

Determinism
-----------

Two runs over the same inventory produce byte-identical HTML: the drawings come
from Graphviz, the records are ordered by the graph, the JSON is dumped in a
fixed order, and nothing here reads a clock, a hostname or a random number.
"""

from __future__ import annotations

import json
from base64 import b64encode
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from functools import cache, lru_cache
from hashlib import sha256
from importlib import resources
from typing import Any, Final

from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup

from netgraph import __version__
from netgraph.errors import RenderError
from netgraph.models import API_VERSION
from netgraph.render.details import build_details, printable
from netgraph.render.dot import to_image
from netgraph.render.fragment import IconLibrary, fragment
from netgraph.render.graph import Graph, Layer
from netgraph.render.ids import element_ids
from netgraph.render.options import RenderOptions

__all__ = ["PAGE_KIND", "asset_text", "html_document", "policy", "to_html"]

#: ``kind`` of the document embedded in the page, mirroring the element
#: envelope of §3 and the ``NetworkGraph`` of the JSON exporter. A consumer
#: reading the records out of a published page can pin what it parses.
PAGE_KIND: Final = "NetworkGraphPage"

#: The id of the ``<script>`` holding those records.
DATA_ELEMENT_ID: Final = "netgraph-data"

#: What each layer is called in the switcher. The value is the flag to pass;
#: the clause after it is what the reader is actually choosing between.
_LAYER_LABELS: Final[Mapping[Layer, str]] = {
    Layer.L1: "l1 — physical",
    Layer.L2: "l2 — VLANs",
    Layer.L3: "l3 — IP subnets",
    Layer.OVERLAY: "overlay — tunnels",
    Layer.ROUTING: "routing — BGP and OSPF",
    Layer.POWER: "power — PDUs and feeds",
}

#: Files inlined into every page, in the order they are concatenated. The
#: renderer's own assets directory is also where ``netgraph web`` reads
#: ``detail.js`` from: one file, two front ends.
_SCRIPTS: Final[tuple[str, ...]] = ("detail.js", "page.js")
_STYLES: Final[tuple[str, ...]] = ("page.css",)

#: Shown when neither ``--title`` nor an inventory path says otherwise.
_FALLBACK_TITLE: Final = "netgraph"


def to_html(graph: Graph, options: RenderOptions | None = None) -> str:
    """Render ``graph`` as one self-contained interactive page."""
    return html_document([graph], options)


def html_document(graphs: Sequence[Graph], options: RenderOptions | None = None) -> str:
    """Render one page holding ``graphs``, one layer each.

    Args:
        graphs: The layers to draw, in the order the switcher offers them. A
            page built from one graph has no switcher.
        options: How much detail to carry. ``element_ids`` is forced on — the
            ids are the whole interface between a shape and its record — and
            ``show_ips``/``show_vlans`` become the ceiling the page's toggles
            move under.

    Raises:
        RenderError: There is nothing to draw, or Graphviz is missing or failed.
    """
    if not graphs:
        raise RenderError("an HTML page needs at least one layer to draw")
    opts = replace(options or RenderOptions(), element_ids=True)

    drawings: dict[bytes, str] = {}
    views: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []
    namespaces: set[str] = set()
    pool = _Pool()
    library = IconLibrary()

    for graph in graphs:
        identity = element_ids(graph)
        details = build_details(graph, opts, ids=identity)
        namespaces.update(name for name in graph.namespaces if name)
        entry: dict[str, Any] = {
            "layer": graph.layer.value,
            "label": _LAYER_LABELS.get(graph.layer, graph.layer.value),
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "views": [],
            "elements": {element: pool.add(record) for element, record in details.items()},
        }
        if graph.dangling:
            # Only reachable behind --force; a reader must be able to tell that
            # the picture is missing links rather than that the links do not exist.
            entry["dangling"] = [printable(text) for text in graph.dangling]
        for show_ips, show_vlans in _variants(opts):
            payload = to_image(
                graph,
                replace(opts, show_ips=show_ips, show_vlans=show_vlans, tooltips=False),
                format="svg",
            )
            view = drawings.get(payload)
            if view is None:
                view = f"v{len(drawings) + 1}"
                drawings[payload] = view
                views.append(
                    {
                        "id": view,
                        "first": not views,
                        # The page holds several drawings of one inventory, so
                        # every id in this one is prefixed with the view it
                        # belongs to: getElementById answers with whichever
                        # copy came first, and that is not a coin the page can
                        # afford to toss.
                        "svg": Markup(
                            fragment(
                                payload,
                                tooltips=False,
                                links=opts.link_template is not None,
                                prefix=view,
                                # One library for the whole page: an icon theme
                                # is a fixed cost, not a per-view one.
                                icons=library,
                            )
                        ),
                    }
                )
            entry["views"].append({"showIps": show_ips, "showVlans": show_vlans, "view": view})
        layers.append(entry)

    title = printable(opts.title or graphs[0].root.name or _FALLBACK_TITLE)
    style = asset_text(_STYLES)
    script = asset_text(_SCRIPTS)
    data = _script_json(
        {
            "apiVersion": API_VERSION,
            "kind": PAGE_KIND,
            "generator": _generator(),
            "title": title,
            "options": {
                "showIps": opts.show_ips,
                "showVlans": opts.show_vlans,
                "tooltips": opts.tooltips,
                "links": opts.link_template is not None,
            },
            "records": pool.records,
            "links": pool.links,
            "layers": layers,
        }
    )
    return (
        _environment()
        .get_template("page.html.j2")
        .render(
            csp=policy(style, (data, script)),
            generator=_generator(),
            title=title,
            style=Markup(style),
            script=Markup(script),
            data=Markup(data),
            icons=Markup(library.markup()),
            views=views,
            layers=layers,
            namespaces=sorted(namespaces),
            toggles={"ips": opts.show_ips, "vlans": opts.show_vlans},
            links=opts.link_template is not None,
        )
    )


def render_html(graph: Graph, options: RenderOptions | None = None) -> str:
    """Kept beside ``render_dot`` and ``render_json``; :func:`to_html` is canonical."""
    return to_html(graph, options)


# --------------------------------------------------------------------------- #
# The drawings
# --------------------------------------------------------------------------- #


def _variants(options: RenderOptions) -> tuple[tuple[bool, bool], ...]:
    """Which drawings to lay out, the one the page opens with first.

    An option that is *off* has no variants: see the module docstring on why a
    ceiling rather than a state.
    """
    ips = (True, False) if options.show_ips else (False,)
    vlans = (True, False) if options.show_vlans else (False,)
    return tuple((i, v) for i in ips for v in vlans)


# --------------------------------------------------------------------------- #
# The records
# --------------------------------------------------------------------------- #

#: The cross-reference a node record carries and an edge record does not: which
#: edges terminate on it, at the layer being drawn. It is the one part of a
#: record that a layer decides, so it is the one part stored per layer.
_LINKS: Final = "links"

#: What ``elements`` holds instead of a links index when a record has no
#: ``links`` key at all.
NO_LINKS: Final = -1


class _Pool:
    """Distinct records and link lists, each stored once for the whole page.

    The same device is drawn at every layer it appears in, and its record is
    the same text every time — the ``links`` list is the exception, because a
    layer decides which edges exist. Splitting the two apart and storing each
    by content makes both a function of the *network* rather than of the layer
    count; see the module docstring for the shape a consumer reads.

    Keying on the serialised form rather than on an element id is deliberate:
    it needs no assumption about what a layer may and may not change, and two
    records that differ in any way at all end up as two entries.
    """

    def __init__(self) -> None:
        self.records: list[Any] = []
        self.links: list[Any] = []
        self._records: dict[str, int] = {}
        self._links: dict[str, int] = {}

    def add(self, record: Mapping[str, Any]) -> tuple[int, int]:
        """Store ``record``, returning where its two halves went."""
        cleaned = _clean(dict(record))
        links = cleaned.pop(_LINKS, None)
        return (
            _intern(self.records, self._records, cleaned),
            NO_LINKS if links is None else _intern(self.links, self._links, links),
        )


def _intern(values: list[Any], seen: dict[str, int], value: Any) -> int:
    """The index of ``value`` in ``values``, appending it the first time.

    Two values are the same when they serialise the same way, which for these
    is exactly when the page would have written the same bytes twice.
    """
    key = json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    index = seen.get(key)
    if index is None:
        index = len(values)
        seen[key] = index
        values.append(value)
    return index


def _clean(value: Any) -> Any:
    """``value`` with everything unprintable dropped from every string in it.

    The same characters the DOT backend refuses to emit
    (:func:`~netgraph.render.details.printable`): a C0 control, and the
    bidirectional overrides that reorder the text *around* them. A record is
    inserted into the page as text and cannot become markup, but a right-to-left
    override in a description would still rewrite the panel it lands in, and
    one short string a reader trusts at a glance has no business being able to
    do that.
    """
    if isinstance(value, str):
        return printable(value)
    if isinstance(value, Mapping):
        return {_clean(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _script_json(payload: Mapping[str, Any]) -> str:
    """``payload`` as JSON that cannot escape the ``<script>`` element it sits in.

    ``</script>`` in a description ends the element as far as an HTML parser is
    concerned, whatever the JSON around it says, so the three characters that
    could start such a sequence are written as their ``\\u`` escapes — which
    JSON.parse reads back as the characters themselves. The two line separators
    go the same way: they are whitespace to a JSON parser and a line break to
    some JavaScript ones.
    """
    text = json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    for character, escape in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(character, escape)
    return text


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


def _generator() -> str:
    return f"netgraph {__version__}"


def policy(style: str, scripts: Iterable[str] = ()) -> str:
    """A page's own Content-Security-Policy, by hash of what it inlines.

    A self-contained page has to inline its script and its style, and the usual
    way to allow that — ``'unsafe-inline'`` — allows *any* inline script,
    including one an intermediary added on the way to the reader. A hash source
    allows exactly the blocks this renderer wrote and nothing else, so the
    policy stays strict without a server, a nonce or a build step.

    Everything else is refused: ``default-src 'none'`` covers fetch, frame,
    font, connect and media in one clause, and the two exceptions are what the
    page genuinely holds — inline pictures (an ``--icons`` theme, embedded as
    ``data:`` URIs) and its own two blocks.
    """
    hashes = [_hash(text) for text in scripts]
    # A page with no script at all -- every page ``netgraph report`` writes --
    # says so, rather than emitting an empty ``script-src`` an implementation
    # would be free to read as "no restriction".
    sources = " ".join(hashes) if hashes else "'none'"
    return (
        f"default-src 'none'; img-src data:; style-src {_hash(style)}; "
        f"script-src {sources}; base-uri 'none'; form-action 'none'"
    )


def _hash(text: str) -> str:
    """``'sha256-…'``, over the exact bytes the template writes."""
    digest = sha256(text.encode("utf-8")).digest()
    return f"'sha256-{b64encode(digest).decode('ascii')}'"


@cache
def asset_text(names: tuple[str, ...], *, package: str = "netgraph.render") -> str:
    """The named files in ``<package>/assets``, concatenated.

    Cached: ``netgraph watch -f html`` re-renders on every save, and the files
    do not change between two of them. ``package`` is a parameter because
    ``netgraph report`` inlines its own style sheet the same way, out of its own
    package, and the caching and the joining are the parts worth sharing.
    """
    return "\n".join(
        (resources.files(package) / "assets" / name).read_text(encoding="utf-8") for name in names
    )


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """The Jinja2 environment for the page, built once and reused.

    Deliberately its own rather than the DOT backend's: that one renders into
    Graphviz's HTML-*like* label syntax and carries a ``dot_string`` filter that
    would be nonsense here, and sharing an environment between two escaping
    contexts is how a renderer ends up quoting for the wrong one.
    """
    return Environment(
        loader=PackageLoader("netgraph.render", "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
