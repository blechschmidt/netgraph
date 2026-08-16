"""Prometheus ``file_sd`` targets: monitoring generated from the diagram's source.

The point is not that writing a target list by hand is hard — it is that the
hand-written one drifts. A switch added to the inventory and drawn in the
diagram is not scraped until somebody remembers the other file. Pointing
``file_sd_configs`` at this output removes the remembering::

    scrape_configs:
      - job_name: network
        file_sd_configs:
          - files: [/etc/prometheus/targets/netviz.json]

Prometheus re-reads the file whenever it changes, so regenerating it in the same
pipeline that renders the diagram keeps the two in step by construction.

Shape
-----

The documented ``file_sd`` schema: a JSON array of target groups, each
``{"targets": [...], "labels": {...}}``. One group per element rather than one
group per label set, because the labels are per element — the namespace, kind,
vendor and site of *that* device — and a shared group could not carry them.

Every label is prefixed ``netviz_``. Prometheus reserves ``__``-prefixed
names for its own metadata and a bare ``site`` or ``kind`` would collide with
whatever the rest of the estate already relabels; the prefix makes an alerting
rule that matches on these unambiguous about where they came from. Additional
static labels from ``--label KEY=VALUE`` are merged in unprefixed, since those
are the operator's own vocabulary.

What it drops
-------------

Everything except one address and four labels. There is no topology, no
interface detail and no VLAN membership — a target list is a list of things to
scrape. An element with no routable address cannot be scraped and is recorded
in the manifest rather than emitted with an empty target.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from netviz.export.context import (
    ExportContext,
    NameRegistry,
    elements_of,
    location_of,
    management_address,
    record_addressless,
)
from netviz.render.graph import Layer, Node

__all__ = ["INSTANCE_LABEL", "LABEL_PREFIX", "emit", "is_assignable_label", "is_label_name"]

#: Namespace for every label this emitter invents. See the module docstring.
LABEL_PREFIX: Final = "netviz_"

#: The one unprefixed label this emitter owns. Prometheus already means "the
#: thing being scraped" by it, and the emitter sets it to the element's name.
INSTANCE_LABEL: Final = "instance"

#: What Prometheus accepts as a label name. ``__``-prefixed names are reserved
#: for its own metadata, which is why ``--label`` refuses them rather than
#: emitting a file Prometheus silently drops labels from.
_LABEL_NAME: Final = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def is_label_name(name: str) -> bool:
    """Is ``name`` a label name Prometheus will keep?

    Reserved ``__`` names are rejected: Prometheus strips them after relabelling,
    so a target file that used one would lose the label without saying so.
    """
    return bool(_LABEL_NAME.match(name)) and not name.startswith("__")


def is_assignable_label(name: str) -> bool:
    """May an operator set ``name`` through ``--label``?

    Everything :func:`is_label_name` accepts, *except* the labels this emitter
    computes per element: ``instance`` and the ``netviz_`` namespace. A static
    ``--label instance=core`` would give every target the same identity — every
    series in the estate collapsing onto one instance — from a file that looks
    correctly configured, which is precisely the failure the ``__`` check
    already exists to prevent.
    """
    return is_label_name(name) and name != INSTANCE_LABEL and not name.startswith(LABEL_PREFIX)


def emit(context: ExportContext) -> str:
    """Render the target file as a JSON document, newline-terminated."""
    recorder = context.recorder
    options = context.options
    groups: list[dict[str, Any]] = []
    registry = NameRegistry(recorder)

    nodes = elements_of(context.at(Layer.L1))
    recorder.considered = len(nodes)
    for node in nodes:
        address = management_address(node)
        if address is None:
            record_addressless(node, recorder)
            continue
        name = registry.register(node)
        if name is None:
            continue
        recorder.emitted += 1
        target = address.target if options.port is None else f"{address.target}:{options.port}"
        groups.append({"targets": [target], "labels": _labels(node, name.fqdn, options.labels)})

    groups.sort(key=lambda group: (group["labels"][INSTANCE_LABEL], group["targets"]))
    return json.dumps(groups, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _labels(node: Node, fqdn: str, extra: dict[str, str] | Any) -> dict[str, str]:
    """The label set of one target, in a fixed key order.

    ``instance`` is the one unprefixed label this emitter sets: Prometheus
    already means "the thing being scraped" by it, and overriding it with the
    element's name is what makes a graph legend read ``sw-01.access.north``
    instead of ``10.1.10.2``. Everything else is namespaced.

    A label whose value is empty is omitted rather than emitted as ``""``:
    Prometheus treats the two as identical, and a file full of empty strings
    invites a matcher that can never fire.

    Deliberately absent: the netviz version. Every label here becomes part of
    the *identity* of every time series scraped from the target, so a label that
    changed on upgrade would end each series and start a new one — the version
    of the tool that wrote the file is not worth a break in eighteen months of
    history. This is the one format with nowhere safe to record provenance; the
    docs say where the file came from instead.
    """
    site, room, rack, _, _ = location_of(node)
    element = node.element
    vendor = getattr(element.spec, "vendor", None) if element is not None else None
    labels = {
        INSTANCE_LABEL: fqdn,
        f"{LABEL_PREFIX}element": node.fqn,
        f"{LABEL_PREFIX}name": node.name,
        f"{LABEL_PREFIX}namespace": node.namespace,
        f"{LABEL_PREFIX}kind": node.kind,
        f"{LABEL_PREFIX}vendor": vendor or "",
        f"{LABEL_PREFIX}site": site,
        f"{LABEL_PREFIX}room": room,
        f"{LABEL_PREFIX}rack": rack,
    }
    kept = {key: value for key, value in labels.items() if value}
    # ``--label`` is refused for anything in this emitter's own namespace
    # (:func:`is_assignable_label`), so the merge cannot overwrite an identity
    # label; the ordering here only settles where the operator's labels appear.
    kept.update(sorted(dict(extra).items()))
    return kept
