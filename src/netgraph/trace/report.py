"""Two renderings of a trace: one for a person, one for a program.

The text form is hop-by-hop and deliberately vertical. A single line per hop
would have to hold an element, two interface names, two addresses, a link with
its medium and rate, and a VLAN or a prefix — well past a terminal's width on
any real inventory, and the first thing to be truncated would be the addresses,
which are the part an operator is checking. So each hop is a small block: the
element on one line, the ports under it, and the link that leaves it under
those. It reads top to bottom the way traffic flows, and ``grep`` still finds a
port name on a line that says which element it is on.

The JSON form is the same facts under the envelope every netgraph document
carries (``apiVersion`` / ``kind``), so a consumer can pin what it parses. Its
one shaping decision is that a path is two arrays — ``waypoints`` and ``links``,
where link *i* joins waypoint *i* to *i + 1* — rather than one array of
alternating kinds; see :mod:`netgraph.trace.model`.

Both are stable: nothing here iterates a set without sorting it, so a trace is
a thing a test can assert on and a golden file can hold.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from netgraph.errors import compact_ids
from netgraph.models import API_VERSION, format_bitrate
from netgraph.render.graph import Layer, TunnelView
from netgraph.trace.model import Endpoint, Frontier, Link, TracedPath, TraceResult, Waypoint

__all__ = ["PATH_KIND", "REPORT_FORMATS", "render_trace", "to_json", "to_text"]

#: ``kind`` of the exported document, mirroring the element envelope of §3 and
#: the ``NetworkGraph`` of :mod:`netgraph.render.jsonexport`.
PATH_KIND = "NetworkPath"

#: What ``-F`` accepts.
REPORT_FORMATS: tuple[str, ...] = ("text", "json")

#: Column at which an interface's addresses start, so the ports of a five-hop
#: path line up into a column a reader can scan for a subnet.
_DETAIL_COLUMN = 30


def render_trace(result: TraceResult, output_format: str, *, all_paths: bool = False) -> str:
    """Render ``result`` in one of :data:`REPORT_FORMATS`."""
    if output_format == "json":
        return to_json(result, all_paths=all_paths)
    return to_text(result, all_paths=all_paths)


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def to_text(result: TraceResult, *, all_paths: bool = False) -> str:
    """The hop-by-hop report, newline-terminated."""
    return "\n".join(_text_lines(result, all_paths=all_paths)) + "\n"


def _text_lines(result: TraceResult, *, all_paths: bool) -> Iterator[str]:
    yield from _header(result, all_paths=all_paths)
    if not result.found:
        yield ""
        yield from _no_path(result)
        return
    for index, path in enumerate(result.selected(all_paths=all_paths), start=1):
        yield ""
        yield from _path_lines(path, index=index, total=len(result.paths))


def _header(result: TraceResult, *, all_paths: bool) -> Iterator[str]:
    verdict = "no path" if not result.found else _plural(len(result.paths), "path")
    yield f"{result.source} -> {result.destination}: {verdict}"
    yield f"  source       {_endpoint_text(result.source)}"
    yield f"  destination  {_endpoint_text(result.destination)}"
    if result.layer is not None:
        yield f"  layer        {_layer_text(result.layer, result.paths)}"
    if result.forced_vlan is not None:
        yield f"  vlan         {result.forced_vlan} (forced with --vlan)"
    elif result.found and result.paths[0].vlans:
        yield f"  vlan         {compact_ids(result.paths[0].vlans)} (assumed by the trace)"
    if result.found and len(result.paths) > 1 and not all_paths:
        yield "  showing      the shortest; pass --all for the rest"
    if result.truncated:
        yield (
            f"  note         the search stopped after {len(result.paths)} paths; there may be more"
        )
    for note in result.notes:
        yield f"  note         {note}"


def _endpoint_text(endpoint: Endpoint) -> str:
    parts = [endpoint.port, f"[{endpoint.kind}]"]
    if endpoint.address:
        parts.append(endpoint.address)
    return "  ".join(parts)


def _layer_text(layer: Layer, paths: Sequence[TracedPath]) -> str:
    if layer is Layer.L3:
        family = paths[0].family if paths else None
        return f"3, routed{f' ({family})' if family else ''}"
    return "2, switched"


def _path_lines(path: TracedPath, *, index: int, total: int) -> Iterator[str]:
    summary = [f"path {index} of {total}", _plural(path.hops, "hop")]
    if path.vlans:
        summary.append(f"vlan {compact_ids(path.vlans)}")
    if path.family:
        summary.append(path.family)
    yield " · ".join(summary)

    for step, waypoint in enumerate(path.waypoints):
        yield f"  {step + 1:>2}  {waypoint.element}  [{waypoint.kind}]"
        yield from _port_lines(waypoint, internal=path.hops == 0)
        if step < len(path.links):
            yield f"      ->  {_link_text(path.links[step])}"


def _port_lines(waypoint: Waypoint, *, internal: bool) -> Iterator[str]:
    """The port lines under an element, addresses aligned into a column.

    Normally ``in`` then ``out``, which is the order traffic meets them. A
    zero-hop path — both ends of the trace on one element — has no "through" to
    describe, so it is spelled ``from``/``to`` and printed in the order the
    arguments were given; ``in port3, out port1`` would read backwards.
    """
    ports = (
        (
            ("from", waypoint.egress, waypoint.egress_addresses),
            ("to  ", waypoint.ingress, waypoint.ingress_addresses),
        )
        if internal
        else (
            ("in  ", waypoint.ingress, waypoint.ingress_addresses),
            ("out ", waypoint.egress, waypoint.egress_addresses),
        )
    )
    for label, name, addresses in ports:
        if not name:
            continue
        detail = ", ".join(addresses)
        cell = f"      {label}{name}"
        yield f"{cell.ljust(_DETAIL_COLUMN)}  {detail}".rstrip() if detail else cell


def _link_text(link: Link) -> str:
    """One line describing a crossed link: what it is, and what it carries."""
    parts = [f"{link.kind} {link.name}"]
    attributes = [
        attribute
        for attribute in (
            link.medium or None,
            format_bitrate(link.speed) if link.speed is not None else None,
            link.label,
            f"{_metres(link.length_m)}" if link.length_m is not None else None,
        )
        if attribute
    ]
    if attributes:
        parts.append(f"({', '.join(attributes)})")
    if link.subnet:
        parts.append(" -> ".join(link.addresses) if link.addresses else link.subnet)
    if link.vlans:
        parts.append(f"vlan {compact_ids(link.vlans)}")
    if link.tunnel is not None:
        parts.append(_tunnel_text(link.tunnel))
    return "  ".join(parts)


def _tunnel_text(view: TunnelView) -> str:
    """The encapsulation a hop enters and leaves, and whether it protects anything."""
    parts = [view.stack_text]
    if view.vni is not None:
        parts.append(f"vni {view.vni}")
    if view.encrypted:
        parts.append("encrypted")
    elif view.encrypted_by is not None:
        parts.append(f"encrypted by {view.encrypted_by}")
    else:
        # The same fact ``W127`` reports about the inventory, said here about
        # this route: everything crossing this hop crosses it in the clear.
        parts.append("CLEARTEXT")
    return f"[{', '.join(parts)}]"


def _no_path(result: TraceResult) -> Iterator[str]:
    """Why there is no path, and where to start looking.

    The furthest element each search reached is the last place the traffic could
    still have got to, so the break is between it and whatever should have come
    next — which is a far more useful answer than "unreachable".
    """
    if result.source.element == result.destination.element:
        # Both ends are one element; the notes above already said why nothing
        # crosses it, and there is no topology to have reached.
        yield f"nothing crosses {result.source.element} between those two ports."
        return
    yield (
        f"no path from {result.source.element} to {result.destination.element} "
        f"within {_plural(result.max_hops, 'hop')}."
    )
    for frontier in result.frontiers:
        yield f"  {_frontier_text(frontier)}"


def _frontier_text(frontier: Frontier) -> str:
    layer = "layer 2" if frontier.layer is Layer.L2 else "layer 3"
    if frontier.is_isolated:
        return f"{layer}: the source reaches nothing at all — check its cabling and VLANs"
    return (
        f"{layer}: reached {_plural(frontier.reached, 'element')}; the furthest was "
        f"{frontier.furthest} at {_plural(frontier.depth, 'hop')}"
    )


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def to_json(result: TraceResult, *, all_paths: bool = False, indent: int = 2) -> str:
    """The same trace as a JSON document, newline-terminated.

    Unlike the text form, this always carries **every** path found: a program
    asking for the routes between two elements wants the redundant pair, and
    ``--all`` is a decision about how much to put on a screen.
    """
    payload = trace_to_dict(result, all_paths=all_paths)
    return json.dumps(payload, indent=indent, ensure_ascii=False, sort_keys=False) + "\n"


def trace_to_dict(result: TraceResult, *, all_paths: bool = False) -> dict[str, Any]:
    """The trace as plain JSON-compatible data."""
    document: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": PATH_KIND,
        "source": _endpoint(result.source),
        "destination": _endpoint(result.destination),
        "found": result.found,
        "layer": str(result.layer) if result.layer is not None else None,
        "maxHops": result.max_hops,
        "pathCount": len(result.paths),
        "truncated": result.truncated,
    }
    if result.forced_vlan is not None:
        document["forcedVlan"] = result.forced_vlan
    # ``--all`` shapes the *report*, not the data: a consumer asked for the
    # routes, and the shortest one alone would be a different answer.
    document["paths"] = [_path(path) for path in result.paths]
    if not result.found:
        document["frontiers"] = [_frontier(frontier) for frontier in result.frontiers]
    if result.notes:
        document["notes"] = list(result.notes)
    del all_paths
    return document


def _endpoint(endpoint: Endpoint) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "spec": endpoint.spec,
        "element": endpoint.element,
        "name": endpoint.name,
        "kind": endpoint.kind,
    }
    if endpoint.interface is not None:
        payload["interface"] = endpoint.interface
    if endpoint.address is not None:
        payload["address"] = endpoint.address
    return payload


def _path(path: TracedPath) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hops": path.hops,
        "layer": str(path.layer),
        "elements": list(path.elements),
        "vlans": sorted(path.vlans),
        "waypoints": [_waypoint(waypoint) for waypoint in path.waypoints],
        "links": [_link(link) for link in path.links],
    }
    if path.family is not None:
        payload["family"] = path.family
    if path.tunnels:
        payload["tunnels"] = [view.fqn for view in path.tunnels]
    if path.cleartext_tunnels:
        payload["cleartextTunnels"] = [view.fqn for view in path.cleartext_tunnels]
    return payload


def _waypoint(waypoint: Waypoint) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "element": waypoint.element,
        "name": waypoint.name,
        "kind": waypoint.kind,
    }
    if waypoint.ingress is not None:
        payload["ingress"] = {
            "interface": waypoint.ingress,
            "addresses": list(waypoint.ingress_addresses),
        }
    if waypoint.egress is not None:
        payload["egress"] = {
            "interface": waypoint.egress,
            "addresses": list(waypoint.egress_addresses),
        }
    if waypoint.vlans:
        payload["vlans"] = sorted(waypoint.vlans)
    return payload


def _link(link: Link) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": link.id,
        "kind": link.kind,
        "name": link.name,
        "endpoints": [
            {"node": link.source, "interface": link.source_port},
            {"node": link.target, "interface": link.target_port},
        ],
    }
    if link.medium:
        payload["medium"] = link.medium
    if link.speed is not None:
        payload["speed"] = link.speed
    if link.label:
        payload["label"] = link.label
    if link.length_m is not None:
        payload["lengthM"] = link.length_m
    if link.subnet is not None:
        payload["subnet"] = link.subnet
    if link.addresses:
        payload["addresses"] = list(link.addresses)
    payload["vlans"] = sorted(link.vlans)
    if link.tunnel is not None:
        payload["tunnel"] = _tunnel(link.tunnel)
    return payload


def _tunnel(view: TunnelView) -> dict[str, Any]:
    """Just enough of a tunnel to say what a hop entered and what protects it.

    The full record — cipher, mode, MTU, endpoints — is what
    ``netgraph render -f json --layer overlay`` is for; repeating it per hop
    would make a five-hop trace mostly tunnel.
    """
    payload: dict[str, Any] = {
        "id": view.fqn,
        "type": view.type,
        "layer": view.layer,
        "stack": list(view.stack),
        "encrypted": view.encrypted,
        "protected": view.protected,
    }
    if view.vni is not None:
        payload["vni"] = view.vni
    if view.over is not None:
        payload["over"] = view.over
    if view.encrypted_by is not None:
        payload["encryptedBy"] = view.encrypted_by
    return payload


def _frontier(frontier: Frontier) -> dict[str, Any]:
    return {
        "layer": str(frontier.layer),
        "reached": frontier.reached,
        "furthest": frontier.furthest,
        "depth": frontier.depth,
    }


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def _metres(metres: float) -> str:
    return f"{int(metres)}m" if float(metres).is_integer() else f"{metres}m"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
