"""What a reader gets on hover: one record per drawn node and edge, and its text.

The diagram a reader gets is deliberately thin — a shape, a name, an interface
or two — because a picture that spelled everything out would be unreadable.
Everything that did not fit lives here instead, keyed by the same identifier the
shape carries (:mod:`netgraph.render.ids`), so a consumer can answer "what is
this?" for whatever is under the pointer.

Two consumers, one builder
--------------------------

:func:`build_details` produces the records. ``netgraph web`` serves them to its
info boxes as JSON; :mod:`netgraph.render.dot` turns each one into a line or
eight of plain text (:func:`detail_text`) and emits it as a Graphviz ``tooltip``,
which is what makes a committed ``topology.svg`` answer the same questions as
the live preview with no server and no JavaScript behind it. The two must not
drift, so the text is derived from the records rather than written a second
time.

The records *are* the JSON export
---------------------------------

Every field comes from :func:`~netgraph.render.jsonexport.graph_to_dict`,
unchanged, with two additions: an ``element`` id per record, and a ``links``
cross-reference on each node naming the edges that terminate on it. That is a
deliberate constraint rather than an implementation detail — a hover box that
computed its own view of an inventory would eventually disagree with
``netgraph render -f json``, and the person comparing the two would have no way
to tell which one was lying.

Display options
---------------

The records are narrowed by the same :class:`~netgraph.render.options.RenderOptions`
the drawing was made with, so ``--no-show-ips`` keeps addresses out of the
tooltips of a diagram published to a wiki as well as off its labels: "do not
print the addresses" has to mean all of the printing, or the flag is a trap.
The web preview passes :data:`DETAIL_OPTIONS` instead — everything shown —
because there the info box *is* the affordance for what the label left out, and
hiding an address from a label is a decision about legibility rather than about
secrecy.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Final

from netgraph.errors import clip_text, count_text
from netgraph.render.graph import Graph
from netgraph.render.ids import ElementIds, element_ids
from netgraph.render.jsonexport import graph_to_dict
from netgraph.render.options import RenderOptions

__all__ = [
    "DETAIL_OPTIONS",
    "MAX_DETAIL_LENGTH",
    "build_details",
    "detail_text",
    "namespace_text",
    "plain_text",
    "printable",
]

#: Display options the web preview's records are built with: everything shown.
#: See the module docstring for why this differs from a rendering's own options.
DETAIL_OPTIONS: Final = RenderOptions(show_ips=True, show_vlans=True)

#: Longest tooltip produced, in characters. A tooltip is a native browser
#: pop-up: past a few lines it covers the diagram it is explaining, and a
#: description of unbounded length would cover all of it.
MAX_DETAIL_LENGTH: Final = 1200

#: Interfaces, links or endpoints spelled out before the rest are counted off.
_MAX_ROWS: Final = 8

#: Addresses or element names listed in one line before the rest are counted off.
_MAX_VALUES: Final = 6

#: Characters removed from anything an inventory wrote. A C0/C1 control cannot
#: appear in XML at any escape — Graphviz refuses a label holding one, and would
#: emit an unparseable document if it did not — and the bidirectional overrides
#: and isolates reorder the text *around* them, which one short string a reader
#: trusts at a glance has no business being able to do.
#:
#: Tab, newline and carriage return are the exceptions: they are legal XML and
#: they carry meaning in a multi-line label.
_UNPRINTABLE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


# --------------------------------------------------------------------------- #
# The records
# --------------------------------------------------------------------------- #


def build_details(
    graph: Graph,
    options: RenderOptions | None = None,
    *,
    ids: ElementIds | None = None,
) -> dict[str, dict[str, Any]]:
    """One record per node and edge of ``graph``, keyed by element id.

    The keys are exactly the ``id`` attributes a rendering made with
    ``element_ids`` carries, so a front end looks a record up with the ``id`` of
    the SVG element under the cursor and nothing else.

    Args:
        options: How much detail to carry; defaults to :data:`DETAIL_OPTIONS`.
        ids: The identity assigned to this graph. Pass the same
            :class:`~netgraph.render.ids.ElementIds` the rendering was made
            with; the default computes it, which is correct but wasteful when
            the caller already has one.
    """
    opts = options or DETAIL_OPTIONS
    identity = ids or element_ids(graph)
    document = graph_to_dict(graph, opts)
    _narrow(document, opts)
    nodes: list[dict[str, Any]] = document["nodes"]
    edges: list[dict[str, Any]] = document["edges"]

    details: dict[str, dict[str, Any]] = {}
    links: dict[str, list[dict[str, Any]]] = {node["id"]: [] for node in nodes}
    for index, edge in enumerate(edges):
        element = identity.edge(index)
        if element is None:  # pragma: no cover - the ids were built from this graph
            continue
        record = dict(edge, element=element, type="edge")
        record["endpoints"] = [
            dict(endpoint, element=identity.node(endpoint["node"]))
            for endpoint in edge["endpoints"]
        ]
        details[element] = record
        for near, far in ((0, 1), (1, 0)):
            endpoint, other = record["endpoints"][near], record["endpoints"][far]
            # A self-link terminates twice on the same node and is listed twice,
            # once per end: the two ends are different ports and a reader
            # looking at the node wants to see both.
            links.setdefault(endpoint["node"], []).append(
                {
                    "element": element,
                    "kind": edge["kind"],
                    "interface": endpoint.get("interface"),
                    "peer": other["node"],
                    "peerElement": other.get("element"),
                    "peerInterface": other.get("interface"),
                    "medium": edge.get("medium"),
                    # A tunnel runs over no medium, so the stack it belongs to
                    # is what the "medium" column of the links table shows
                    # instead: 'vxlan over ipsec' rather than a blank cell.
                    "stack": " over ".join(edge["tunnel"]["stack"]) if "tunnel" in edge else None,
                    "speedText": edge.get("speedText"),
                    "label": edge.get("label"),
                    "vlans": edge.get("vlans", []),
                }
            )

    for node in nodes:
        element = identity.node(node["id"])
        if element is None:  # pragma: no cover - the ids were built from this graph
            continue
        details[element] = dict(node, element=element, links=links.get(node["id"], []))
    return details


def _narrow(document: dict[str, Any], options: RenderOptions) -> None:
    """Apply the display options the export itself does not gate.

    ``graph_to_dict`` treats VLAN membership and a prefix's addresses as
    topology rather than decoration and always exports them, which is right for
    a machine-readable document and wrong for a tooltip: see the module
    docstring on display options.
    """
    # A bundled edge carries its members' records inside it, and they are
    # records of exactly the same shape, so the options have to reach them too.
    members = [
        link for edge in document["edges"] for link in edge.get("bundle", {}).get("links", ())
    ]
    if not options.show_vlans:
        for record in (*document["nodes"], *document["edges"], *members):
            record["vlans"] = []
            if "aggregate" in record:
                record["aggregate"] = dict(record["aggregate"], vlans=[])
    if not options.show_ips:
        for node in document["nodes"]:
            if "subnet" in node:
                node["subnet"] = dict(node["subnet"], addresses=[])
            if "aggregate" in node:
                node["aggregate"] = dict(node["aggregate"], subnets=[])
        for edge in (*document["edges"], *members):
            edge.pop("addresses", None)


# --------------------------------------------------------------------------- #
# The text
# --------------------------------------------------------------------------- #


def detail_text(record: Mapping[str, Any]) -> str:
    """One record as the plain text a tooltip shows.

    Lines, not prose: a browser renders a tooltip in a fixed-width-ish box with
    no wrapping to speak of, so the format is one fact per line, ordered from
    identity outwards. The whole is bounded by :data:`MAX_DETAIL_LENGTH`; the
    lists inside it are bounded first, so what is dropped is the tail of the
    longest table rather than the last three sections.
    """
    lines = _edge_lines(record) if record.get("type") == "edge" else _node_lines(record)
    return clip_text("\n".join(line for line in lines if line), limit=MAX_DETAIL_LENGTH)


def namespace_text(namespace: str, records: Iterable[Mapping[str, Any]]) -> str:
    """The tooltip of the cluster drawn around ``namespace``.

    ``records`` are the node records inside it — what the box is a box *of*,
    which is the one thing its label does not say.
    """
    members = list(records)
    kinds: dict[str, int] = {}
    vlans: set[int] = set()
    for record in members:
        kind = plain_text(str(record.get("kind") or "element"))
        kinds[kind] = kinds.get(kind, 0) + 1
        vlans.update(record.get("vlans", ()))
    lines = [
        f"namespace {plain_text(namespace)}",
        count_text(len(members), "element")
        + (
            ": " + ", ".join(count_text(count, kind) for kind, count in sorted(kinds.items()))
            if kinds
            else ""
        ),
    ]
    if vlans:
        lines.append(f"vlans: {_compact_ids(vlans)}")
    return clip_text("\n".join(lines), limit=MAX_DETAIL_LENGTH)


def _node_lines(record: Mapping[str, Any]) -> Iterator[str]:
    """A node: what it is, then what is configured on it, then what it touches."""
    yield f"{plain_text(str(record.get('name', '')))} [{_subtitle(record)}]"
    namespace = record.get("namespace")
    if namespace:
        yield f"namespace: {plain_text(str(namespace))}"
    description = record.get("description")
    if description:
        yield plain_text(str(description))
    labels = record.get("labels")
    if labels:
        yield "labels: " + _listed(
            f"{plain_text(key)}={plain_text(str(value))}" for key, value in labels.items()
        )

    subnet, tunnel = record.get("subnet"), record.get("tunnel")
    if isinstance(subnet, Mapping):
        yield from _subnet_lines(subnet)
    if isinstance(tunnel, Mapping):
        yield from _tunnel_lines(tunnel)
    aggregate = record.get("aggregate")
    if isinstance(aggregate, Mapping):
        yield from _aggregate_lines(aggregate)

    vlans = record.get("vlans")
    if vlans:
        yield f"vlans: {_compact_ids(vlans)}"
    yield from _rows("interfaces", record.get("interfaces", ()), _interface_row)
    yield from _rows("links", record.get("links", ()), _link_row)


def _subtitle(record: Mapping[str, Any]) -> str:
    """The bracketed kind after a node's name, as the diagram itself spells it."""
    subnet, tunnel = record.get("subnet"), record.get("tunnel")
    if isinstance(subnet, Mapping):
        return f"{subnet.get('family', 'ip')} subnet"
    if isinstance(tunnel, Mapping):
        return f"{tunnel.get('type', 'tunnel')} tunnel"
    if isinstance(record.get("aggregate"), Mapping):
        return "namespace"
    return plain_text(str(record.get("kind", "element")))


def _aggregate_lines(aggregate: Mapping[str, Any]) -> Iterator[str]:
    """A collapsed namespace: how big it is, what is in it, what it swallowed.

    The elements are listed rather than only counted. A reader hovering over one
    box that stands for two hundred devices is asking *which* devices, and a
    tooltip that answered "two hundred" would be the box's label again.
    """
    elements = list(aggregate.get("elements", ()))
    counts = aggregate.get("countsByKind")
    yield (
        f"collapsed namespace {plain_text(str(aggregate.get('namespace', '')))}: "
        + count_text(len(elements), "element")
        + (
            ": " + ", ".join(count_text(int(number), kind) for kind, number in counts.items())
            if isinstance(counts, Mapping) and counts
            else ""
        )
    )
    internal = list(aggregate.get("internalLinks", ()))
    if internal:
        # Without this the reader would count the edges on the page and
        # conclude the site has no cabling in it.
        yield count_text(len(internal), "link") + " inside, not drawn"
    subnets = list(aggregate.get("subnets", ()))
    if subnets:
        yield "subnets: " + _listed(plain_text(str(prefix)) for prefix in subnets)
    if elements:
        yield "elements: " + _listed(plain_text(str(element)) for element in elements)


def _subnet_lines(subnet: Mapping[str, Any]) -> Iterator[str]:
    """A derived prefix: how populated it is, and by whom."""
    addresses = list(subnet.get("addresses", ()))
    elements = list(subnet.get("elements", ()))
    yield (
        f"prefix {plain_text(str(subnet.get('prefix', '')))}: "
        f"{count_text(len(elements), 'element')}, {count_text(len(addresses), 'address', 'addresses')}"
    )
    if addresses:
        yield "addresses: " + _listed(plain_text(str(address)) for address in addresses)
    if elements:
        yield "elements: " + _listed(plain_text(str(element)) for element in elements)


def _tunnel_lines(tunnel: Mapping[str, Any]) -> Iterator[str]:
    """A tunnel: the stack it belongs to, what protects it, and where it lands."""
    stack = [str(step) for step in tunnel.get("stack", ())] or [str(tunnel.get("type", "tunnel"))]
    yield "stack: " + plain_text(" over ".join(stack))

    parts: list[str] = []
    if tunnel.get("vni") is not None:
        parts.append(f"vni {tunnel['vni']}")
    if tunnel.get("mode"):
        parts.append(f"mode {plain_text(str(tunnel['mode']))}")
    transport = tunnel.get("transport")
    if transport:
        port = tunnel.get("port")
        parts.append(f"transport {plain_text(str(transport))}" + (f"/{port}" if port else ""))
    if tunnel.get("encrypted"):
        cipher = tunnel.get("cipher")
        parts.append(f"encrypted: {plain_text(str(cipher))}" if cipher else "encrypted")
    elif tunnel.get("encryptedBy"):
        parts.append(f"cleartext, carried by {plain_text(str(tunnel['encryptedBy']))}")
    else:
        # The single most expensive thing a diagram can get wrong is a tunnel a
        # reader assumes is private and is not, so it is never left implicit.
        parts.append("cleartext")
    if tunnel.get("auth"):
        parts.append(f"auth {plain_text(str(tunnel['auth']))}")
    if tunnel.get("mtu") is not None:
        parts.append(f"mtu {tunnel['mtu']} (overhead {tunnel.get('overheadBytes', 0)} B)")
    yield ", ".join(parts)

    yield from _rows("endpoints", tunnel.get("endpoints", ()), _endpoint_row)


def _bundle_lines(bundle: Mapping[str, Any]) -> Iterator[str]:
    """The links one drawn edge stands for, one per row.

    This is the whole affordance for a bundle: the picture says "four links",
    and the only place a reader can find out *which* four is here.
    """
    links = list(bundle.get("links", ()))
    aggregate = bundle.get("aggregate")
    if isinstance(aggregate, list) and aggregate:
        yield "link aggregation: " + " — ".join(
            _endpoint(end) for end in aggregate if isinstance(end, Mapping)
        )
    yield from _rows("bundled links", links, _bundled_row)


def _bundled_row(link: Mapping[str, Any]) -> str:
    """One member of a bundle: its ports, then how it differs from its siblings."""
    parts = [" — ".join(_endpoint(endpoint) for endpoint in link.get("endpoints", ()))]
    detail = [
        plain_text(str(value))
        for value in (link.get("id"), link.get("medium"), link.get("speedText"), link.get("label"))
        if value
    ]
    if detail:
        parts.append(f"({', '.join(detail)})")
    return "  ".join(parts)


def _edge_lines(record: Mapping[str, Any]) -> Iterator[str]:
    """An edge: what it is, what it joins, and what it carries."""
    kind = plain_text(str(record.get("kind", "link")))
    identity = plain_text(str(record.get("id", "")))
    yield f"{kind} {identity}" if identity else kind
    yield " — ".join(_endpoint(endpoint) for endpoint in record.get("endpoints", ()))

    bundle = record.get("bundle")
    if isinstance(bundle, Mapping):
        yield from _bundle_lines(bundle)

    tunnel = record.get("tunnel")
    if isinstance(tunnel, Mapping):
        yield from _tunnel_lines(tunnel)
    else:
        parts: list[str] = []
        if record.get("medium"):
            parts.append(f"medium: {plain_text(str(record['medium']))}")
        if record.get("speedText"):
            parts.append(f"speed: {plain_text(str(record['speedText']))}")
        if record.get("lengthM") is not None:
            parts.append(f"length: {_number(record['lengthM'])} m")
        if record.get("label"):
            parts.append(f"label: {plain_text(str(record['label']))}")
        if parts:
            yield ", ".join(parts)

    addresses = record.get("addresses")
    if addresses:
        yield "addresses: " + _listed(plain_text(str(address)) for address in addresses)
    vlans = record.get("vlans")
    if vlans:
        yield f"vlans: {_compact_ids(vlans)}"


# --------------------------------------------------------------------------- #
# Rows and values
# --------------------------------------------------------------------------- #


def _rows(
    heading: str,
    entries: Iterable[Mapping[str, Any]],
    row: Callable[[Mapping[str, Any]], str],
) -> Iterator[str]:
    """A counted, indented table, or nothing when there is nothing to table.

    The heading counts what there *is* and the last row counts what was left
    out, so a bounded tooltip never reads as a complete list of eight ports on
    a switch that has forty-eight.
    """
    items = list(entries)
    if not items:
        return
    yield f"{heading} ({len(items)}):"
    for entry in items[:_MAX_ROWS]:
        yield "  " + row(entry)
    hidden = len(items) - _MAX_ROWS
    if hidden > 0:
        yield f"  (+{hidden} more)"


def _interface_row(port: Mapping[str, Any]) -> str:
    parts = [plain_text(str(port.get("name", "")))]
    if port.get("type"):
        parts.append(plain_text(str(port["type"])))
    if port.get("enabled") is False:
        parts.append("disabled")
    addresses = list(port.get("addresses", ()))
    if addresses:
        parts.append(_listed(plain_text(str(address)) for address in addresses))
    vlan = port.get("vlan")
    if isinstance(vlan, Mapping) and vlan.get("vlans"):
        parts.append(
            f"vlan {_compact_ids(vlan['vlans'])} ({plain_text(str(vlan.get('mode', '')))})"
        )
    if port.get("mac"):
        parts.append(plain_text(str(port["mac"])))
    return "  ".join(parts)


def _link_row(link: Mapping[str, Any]) -> str:
    near = plain_text(str(link.get("interface") or ""))
    far = _endpoint({"node": link.get("peer"), "interface": link.get("peerInterface")})
    parts = [f"{near} — {far}" if near else far]
    detail = [
        plain_text(str(value))
        for value in (
            link.get("kind"),
            link.get("stack") or link.get("medium"),
            link.get("speedText"),
        )
        if value
    ]
    if detail:
        parts.append(f"({', '.join(detail)})")
    if link.get("vlans"):
        parts.append(f"vlan {_compact_ids(link['vlans'])}")
    return "  ".join(parts)


def _endpoint_row(end: Mapping[str, Any]) -> str:
    return _endpoint(end)


def _endpoint(end: Mapping[str, Any]) -> str:
    """``sites/hq/sw-core:port1``, or the element alone when it names no port."""
    node = plain_text(str(end.get("node") or ""))
    interface = end.get("interface")
    return f"{node}:{plain_text(str(interface))}" if interface else node


def _listed(values: Iterable[str]) -> str:
    """A comma-separated list, bounded to :data:`_MAX_VALUES` entries."""
    items = list(values)
    if len(items) <= _MAX_VALUES:
        return ", ".join(items)
    return ", ".join(items[:_MAX_VALUES]) + f", (+{len(items) - _MAX_VALUES} more)"


def _compact_ids(ids: Iterable[int]) -> str:
    """VLAN ids as coalesced ranges: ``10,20,100-110``."""
    ordered = sorted({int(value) for value in ids})
    if not ordered:
        return ""
    ranges: list[list[int]] = []
    for value in ordered:
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ",".join(str(low) if low == high else f"{low}-{high}" for low, high in ranges)


def _number(value: float) -> str:
    """Drop the trailing ``.0`` of a whole-number float."""
    return str(int(value)) if float(value).is_integer() else str(value)


def printable(text: str) -> str:
    """``text`` with the characters of :data:`_UNPRINTABLE` removed.

    Applied at the two chokepoints every string from an inventory passes
    through on its way into a document — the DOT quoted string
    (:func:`netgraph.render.dot._dot_string`) and this module's own text — so
    that no producer has to remember. Line structure is preserved; use
    :func:`plain_text` where a value has to stay on one line.
    """
    return _UNPRINTABLE.sub("", text)


def plain_text(text: str) -> str:
    """Whatever an inventory wrote, as one line of printable text.

    Collapses whitespace — a description spanning three lines would otherwise
    push the rest of a tooltip out of view, and a newline inside a record label
    would break the row it is in — and drops the characters of
    :data:`_UNPRINTABLE`.

    Shared by the tooltips and the drawn labels rather than done twice: both are
    places an inventory's text reaches a published document, and a rule that
    held in one of them only would be a rule nobody could rely on.
    """
    return " ".join(cleaned for token in text.split() if (cleaned := printable(token)))
