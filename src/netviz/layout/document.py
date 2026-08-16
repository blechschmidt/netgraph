"""Writing geometry as YAML somebody can still read.

A layout document is generated, and it is *large*: four numbers per node, one
node per device. Written the way every other netviz document is written —
block mappings, one key per line — a hundred-device diagram becomes four hundred
lines, and dragging one switch shows up in a diff as four changed lines buried
in them.

So a point and a size are written **inline**::

    nodes:
      core/sw-core:
        position: {x: 240, y: 396}
      core/rtr-edge:
        position: {x: 240, y: 540}

Two lines per node instead of four, and — the part that matters — *one changed
line per drag*, so a diff of an arrangement reads as a list of what moved. The
parser does not care, flow style being YAML like any other, and ``netviz fmt``
already preserves it; a reviewer very much does.

(The canonical form keeps the innermost flow mapping and expands the one around
it, which is why an entry is two lines rather than one. That is the formatter's
decision, not this module's, and it is the right one: the key is what a reader
scans for.)

Both writers have to agree, because both write these documents: a *new* layout
document goes out through PyYAML (:func:`netviz.edit.apply._emit`), and an
*existing* one is edited through ruamel so its comments survive. :class:`Inline`
is the marker they share; :func:`as_yaml` is what turns it into ruamel's form.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import yaml as pyyaml
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from netviz.layout.geometry import Geometry, LinkGeometry

__all__ = [
    "Inline",
    "as_yaml",
    "canonical_geometry",
    "geometry_sections",
    "inline_entry",
    "link_entry",
]


class Inline(dict[str, Any]):
    """A mapping written on one line, whichever emitter writes it."""


def _represent_inline(dumper: pyyaml.SafeDumper, data: Inline) -> Any:
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True)


pyyaml.add_representer(Inline, _represent_inline, Dumper=pyyaml.SafeDumper)


def inline_entry(value: Mapping[str, Any]) -> Inline:
    """One geometry entry with every mapping under it marked inline.

    A point and a size are always one line: they are two numbers that mean
    nothing apart, and nobody has ever wanted them on four.
    """
    return Inline((key, _written(item)) for key, item in value.items())


def _written(value: Any) -> Any:
    """One value as it goes into the document.

    A coordinate that is a whole number of points is written as an integer:
    ``240`` rather than ``240.0``. The two parse to the same float, and a file
    of four-hundred ``.0`` suffixes reads like a machine wrote it carelessly.
    """
    if isinstance(value, Mapping):
        return inline_entry(value)
    if isinstance(value, (list, tuple)):
        return [_written(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


#: The mapping spellings of the two-number shorthand a point and a size accept.
_PAIR_KEYS: Final[tuple[tuple[str, ...], ...]] = (("x", "y"), ("width", "height"))


def canonical_geometry(value: Any) -> Any:
    """``value`` reduced to what it *means*, so two spellings compare equal.

    ``position: [240, 396]`` and ``position: {x: 240, y: 396}`` are the same
    position, and ``240`` and ``240.0`` are the same number. A writer that could
    not tell would rewrite every hand-written shorthand the first time an
    arrangement was re-seeded, and a re-seed of an unchanged diagram would
    produce a diff — which is exactly what a generated file must not do.
    """
    if isinstance(value, Mapping):
        keys = tuple(str(key) for key in value)
        if keys in _PAIR_KEYS:
            return tuple(_number(item) for item in value.values())
        return {str(key): canonical_geometry(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [canonical_geometry(item) for item in value]
        if len(items) == 2 and all(isinstance(item, float) for item in items):
            return tuple(items)
        return items
    return _number(value)


def _number(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def as_yaml(value: Any) -> Any:
    """``value`` in the form ruamel's round-trip emitter writes.

    Plain builtins go into a ruamel tree perfectly well, but they come out in
    block style — so an :class:`Inline` has to become a ``CommentedMap`` with
    its flow flag set, and everything under it has to be converted too or the
    conversion stops at the first nested mapping.
    """
    if isinstance(value, Mapping):
        mapping = CommentedMap((key, as_yaml(item)) for key, item in value.items())
        if isinstance(value, Inline):
            mapping.fa.set_flow_style()
        return mapping
    if isinstance(value, (list, tuple)):
        sequence = CommentedSeq(as_yaml(item) for item in value)
        sequence.fa.set_flow_style()
        return sequence
    return value


def geometry_sections(geometry: Geometry, *, with_waypoints: bool = False) -> dict[str, Any]:
    """One view's geometry as the ``nodes``/``edges``/``groups`` a document holds.

    Sorted by key, so re-seeding an unchanged diagram writes an unchanged file
    and a diff shows what moved rather than what was re-ordered. An empty
    section is left out entirely rather than written as ``{}``.
    """
    sections: dict[str, Any] = {}
    if geometry.nodes:
        sections["nodes"] = {
            key: inline_entry(placement.to_model().model_dump(exclude_none=True))
            for key, placement in sorted(geometry.nodes.items())
        }
    if with_waypoints or _pinned_beyond_waypoints(geometry):
        edges = {
            key: link_entry(link)
            for key, link in sorted(geometry.edges.items())
            if not link.is_empty
            and (with_waypoints or link.waypoints or link.routing or link.label)
        }
        if edges:
            sections["edges"] = edges
    if geometry.groups:
        sections["groups"] = {
            key: inline_entry(box.to_model().model_dump())
            for key, box in sorted(geometry.groups.items())
        }
    return sections


def link_entry(link: LinkGeometry) -> Inline:
    """One link's geometry as a document entry: bends, style and label.

    Written the same way a node entry is — every mapping under the key inline,
    the key itself expanded by ``netviz fmt`` — so a bend is one line, and
    dragging the third one changes the third line and nothing else.
    """
    entry = link.to_model().model_dump(exclude_none=True)
    written: dict[str, Any] = {}
    if entry.get("waypoints"):
        written["waypoints"] = [inline_entry(point) for point in entry["waypoints"]]
    if "routing" in entry:
        written["routing"] = entry["routing"]
    if "label" in entry:
        written["label"] = inline_entry(entry["label"])
    return Inline(written)


def _pinned_beyond_waypoints(geometry: Geometry) -> bool:
    """Does any link pin something a re-layout could not work out for itself?

    A routing style and a nudged label are *decisions*, not derived numbers, so
    they are written whether or not ``--waypoints`` was asked for — unlike a
    spline, which the render recomputes identically from the node positions.
    """
    return any(
        link.routing is not None or link.label is not None for link in geometry.edges.values()
    )
