"""The target state ``--from-live`` plans against: the declaration, plus what was seen.

``netgraph drift`` answers "does the network agree with the file?". This module
answers the follow-up: "what would the file say if it did?". It takes the same
:class:`~netgraph.importer.draft.Draft` the importer builds and the same
:class:`~netgraph.drift.coverage.Coverage` that decides which silences are
meaningful, and returns an :class:`~netgraph.loader.Inventory` — the declared
one with every observation written into it. Diffing that against the declaration
gives a changeset that adopts the network into the inventory, which is the write
half of the drift loop.

The whole design rests on one rule: **the target starts as the source**. It is
not the capture rendered as YAML. A capture sees a fraction of an inventory —
``lldp`` sees neighbours and no interfaces, ``iproute`` sees interfaces and no
neighbours, neither sees a rack position or a description — and a target built
from what was seen would propose deleting everything that was not. So every
declared document is carried over untouched, and an observation only ever
overwrites the field it is an observation *of*.

The second rule follows from it: **absence only deletes where coverage vouches
for it**. A declared interface no capture mentioned is removed only when the
dialect that saw the device lists every interface it has. Everywhere else the
declaration stands and the reason is recorded, so ``netgraph plan --from-live``
on a partial capture proposes additions and corrections and never a cull.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from netgraph.drift.compare import EVERYTHING, CompareSpec
from netgraph.drift.coverage import Coverage, coverage_of
from netgraph.errors import SchemaError
from netgraph.importer.draft import Draft, DraftCable, DraftDevice, DraftInterface, Endpoint
from netgraph.importer.names import element_name
from netgraph.loader.inventory import (
    Inventory,
    SourceLocation,
    namespace_of,
    qualify,
    short_name,
)
from netgraph.models import API_VERSION, Adapter, Device, Element, parse_document
from netgraph.plan.document import document_of

__all__ = ["Adoption", "adopt"]

#: Interface types no dialect netgraph reads ever reports, so their absence from
#: a capture is never evidence that they are gone.
_UNIMPORTED_TYPES: Final[frozenset[str]] = frozenset({"loopback", "tunnel"})

#: A capture's ``ethernet`` means "a NIC" and distinguishes nothing finer, so it
#: is never adopted over one of these.
_NIC_TYPES: Final[frozenset[str]] = frozenset({"ethernet", "wifi"})

#: Kinds that are netgraph's own abstraction: no capture has a word for one, so
#: an observed ``kind`` is never written over them.
_UNOBSERVABLE_KINDS: Final[frozenset[str]] = frozenset({"adapter", "patchpanel", "pdu"})

#: Interface scalars taken verbatim from a capture that reported one.
_SCALARS: Final[tuple[str, ...]] = ("enabled", "mac", "mtu", "parent")


@dataclass(frozen=True, slots=True)
class Adoption:
    """The target inventory, and what could not be folded into it."""

    inventory: Inventory
    #: One line per thing the capture could not vouch for, so the plan can say
    #: what it deliberately left alone.
    unobserved: tuple[str, ...] = ()
    #: One line per adopted document that would not validate. The declared
    #: document is kept instead, so a plan is never built on a broken target.
    rejected: tuple[str, ...] = ()


@dataclass(slots=True)
class _Adoption:
    """One run, accumulating documents and notes as it goes."""

    inventory: Inventory
    draft: Draft
    coverage: Coverage
    spec: CompareSpec
    #: Fully-qualified name to the document it should have after adoption.
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Fully-qualified names whose document changed, so only those re-validate.
    touched: set[str] = field(default_factory=set)
    #: Fully-qualified names the capture contradicts the existence of.
    removed: set[str] = field(default_factory=set)
    #: Namespace for each newly created element.
    namespaces: dict[str, str] = field(default_factory=dict)
    #: Observed device name to the declared name it resolved to.
    resolved: dict[str, str] = field(default_factory=dict)
    #: Observed device name to the name it is known by here, declared or not.
    keys: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    # -- driver ----------------------------------------------------------

    def run(self) -> Adoption:
        self.documents = {
            fqn: document_of(element) for fqn, element in self.inventory.elements.items()
        }
        self._resolve_devices()
        self._adopt_devices()
        self._adopt_links()
        return Adoption(
            inventory=self._build(),
            unobserved=tuple(self.notes),
            rejected=tuple(self.rejected),
        )

    def _selected(self, *names: str) -> bool:
        return self.spec.includes(*names)

    # -- devices ---------------------------------------------------------

    def _resolve_devices(self) -> None:
        """Match every observed device name onto the declared tree.

        Identical to what :mod:`netgraph.drift.compare` does, and deliberately
        so: a plan built from a capture has to adopt onto exactly the element
        the drift report was about.
        """
        for name in self.draft.devices:
            resolution = self.inventory.lookup(name)
            element = resolution.element
            if resolution.fqn is not None and element is not None and _is_a_box(element):
                self.resolved[name] = resolution.fqn
                self.keys[name] = resolution.fqn
            else:
                self.keys[name] = qualify("", element_name(name)[0] or name)

    def _adopt_devices(self) -> None:
        for name, observed in self.draft.devices.items():
            key = self.keys[name]
            if not self._selected(key, short_name(key)):
                continue
            fqn = self.resolved.get(name)
            if fqn is None:
                self._create_device(key, observed)
                continue
            self._adopt_device(fqn, observed)

    def _create_device(self, fqn: str, observed: DraftDevice) -> None:
        document: dict[str, Any] = {
            "apiVersion": API_VERSION,
            "kind": observed.kind or "computer",
            "metadata": {"name": short_name(fqn)},
            "spec": {},
        }
        if observed.description:
            document["metadata"]["description"] = observed.description
        interfaces = [_interface_document(seen) for seen in observed.interfaces.values()]
        if interfaces:
            document["spec"]["interfaces"] = interfaces
        self.documents[fqn] = document
        self.namespaces[fqn] = namespace_of(fqn)
        self.touched.add(fqn)

    def _adopt_device(self, fqn: str, observed: DraftDevice) -> None:
        declared = self.inventory.elements[fqn]
        document = self.documents[fqn]
        if _adopts_kind(declared.kind, observed.kind):
            document["kind"] = observed.kind
            self.touched.add(fqn)
        if not isinstance(declared, Device | Adapter):
            return
        self._adopt_interfaces(fqn, observed, document)

    # -- interfaces ------------------------------------------------------

    def _adopt_interfaces(self, fqn: str, observed: DraftDevice, document: dict[str, Any]) -> None:
        spec = document.setdefault("spec", {})
        interfaces: list[dict[str, Any]] = list(spec.get("interfaces", ()))
        by_name = {entry["name"]: entry for entry in interfaces if "name" in entry}

        for name, seen in observed.interfaces.items():
            entry = by_name.get(name)
            if entry is None:
                interfaces.append(_interface_document(seen))
                self.touched.add(fqn)
                continue
            if self._adopt_interface(observed, entry, seen):
                self.touched.add(fqn)

        kept = [entry for entry in interfaces if self._keeps(fqn, observed, entry)]
        if len(kept) != len(interfaces):
            self.touched.add(fqn)
        if kept:
            spec["interfaces"] = kept
        else:
            spec.pop("interfaces", None)

    def _keeps(self, fqn: str, observed: DraftDevice, entry: Mapping[str, Any]) -> bool:
        """Does a declared interface survive a capture that did not mention it?"""
        name = str(entry.get("name", ""))
        if name in observed.interfaces:
            return True
        if self.spec.ignores_interface(name):
            return True
        if str(entry.get("type", "ethernet")) in _UNIMPORTED_TYPES:
            return True
        if not self.coverage.observes_interfaces(observed.name):
            self.notes.append(
                f"{fqn}: kept interface {name!r}; no input lists the interfaces of this device"
            )
            return True
        return False

    def _adopt_interface(
        self, observed: DraftDevice, entry: dict[str, Any], seen: DraftInterface
    ) -> bool:
        """Write every observation onto one declared interface. Did anything change?"""
        changed = False
        if _adopts_type(str(entry.get("type", "ethernet")), seen.type) and (
            seen.type != "ethernet" or self.coverage.observes_interfaces(observed.name)
        ):
            entry["type"] = seen.type
            changed = True
        for name in _SCALARS:
            value = getattr(seen, name)
            if value is not None and entry.get(name) != value:
                entry[name] = value
                changed = True
        changed |= self._adopt_addresses(observed, entry, seen)
        changed |= self._adopt_members(observed, entry, seen)
        changed |= _adopt_vlan(entry, seen)
        return changed

    def _adopt_addresses(
        self, observed: DraftDevice, entry: dict[str, Any], seen: DraftInterface
    ) -> bool:
        changed = False
        for family in ("ipv4", "ipv6"):
            found = list(getattr(seen, family))
            declared = _declared_addresses(entry, family)
            complete = self.coverage.observes_addresses(observed.name, family)
            if not found and not declared:
                continue
            wanted = _normalised(found) if complete else _normalised([*declared, *found])
            if wanted == _normalised(declared):
                continue
            if not complete and set(_normalised(declared)) - set(wanted):  # pragma: no cover
                continue
            config = entry.setdefault(family, {})
            if wanted:
                config["addresses"] = wanted
            else:
                config.pop("addresses", None)
                if not config:
                    entry.pop(family, None)
            changed = True
        return changed

    def _adopt_members(
        self, observed: DraftDevice, entry: dict[str, Any], seen: DraftInterface
    ) -> bool:
        declared = list(entry.get("members", ()))
        found = list(seen.members)
        if not declared and not found:
            return False
        complete = bool(found) and self.coverage.observes_members(observed.name)
        wanted = found if complete else [*declared, *(m for m in found if m not in declared)]
        if wanted == declared:
            return False
        if not complete and set(declared) - set(wanted):  # pragma: no cover
            return False
        if wanted:
            entry["members"] = wanted
        else:
            entry.pop("members", None)
        return True

    # -- links -----------------------------------------------------------

    def _adopt_links(self) -> None:
        """Cables, matched by endpoint pair — a capture never learns a name."""
        declared = self._declared_links()
        observed = {self._link_key(cable): cable for cable in self.draft.cables.values()}
        terminated = {endpoint for key in observed for endpoint in key}
        moved = self._moved_links(declared, observed)
        for key, cable in observed.items():
            if key in declared:
                self._adopt_link(declared[key], cable)
                continue
            if key in moved:
                self._move_link(moved[key], key, cable)
                continue
            if self._link_in_scope(key):
                self._create_link(key, cable)
        for key, fqn in declared.items():
            if key in observed or fqn in moved.values() or not self._link_in_scope(key):
                continue
            self._drop_link(fqn, key, terminated)

    def _moved_links(
        self,
        declared: Mapping[tuple[Endpoint, Endpoint], str],
        observed: Mapping[tuple[Endpoint, Endpoint], DraftCable],
    ) -> dict[tuple[Endpoint, Endpoint], str]:
        """Observed links that are a *declared* cable somebody re-patched.

        Matched on the label, which is the one thing about a cable that survives
        being moved to another port: the sticker on the lead is what an operator
        reads back off the patch list. Without this a re-patched run would be
        planned as a cable destroyed and an unrelated one created, losing its
        length, its category and every comment on its document for no reason.

        Only unambiguous pairs count — one unmatched declared cable and one
        unmatched observed link carrying that label.
        """
        by_label: dict[str, list[str]] = {}
        for key, fqn in declared.items():
            if key in observed:
                continue
            label = self.inventory.cables[fqn].spec.label
            if label:
                by_label.setdefault(str(label), []).append(fqn)
        candidates: dict[str, list[tuple[Endpoint, Endpoint]]] = {}
        for key, cable in observed.items():
            if key in declared or not cable.label:
                continue
            candidates.setdefault(str(cable.label), []).append(key)
        return {
            keys[0]: fqns[0]
            for label, fqns in by_label.items()
            if len(fqns) == 1 and len(keys := candidates.get(label, [])) == 1
        }

    def _move_link(self, fqn: str, key: tuple[Endpoint, Endpoint], cable: DraftCable) -> None:
        """Point a declared cable at the ports the capture found it on."""
        namespace = namespace_of(fqn)
        (device_a, port_a), (device_b, port_b) = key
        spec = self.documents[fqn].setdefault("spec", {})
        spec["endpoints"] = [
            f"{self._spell(device_a, namespace)}:{port_a}",
            f"{self._spell(device_b, namespace)}:{port_b}",
        ]
        self.touched.add(fqn)
        self._adopt_link(fqn, cable)

    def _spell(self, fqn: str, namespace: str) -> str:
        """How a document in ``namespace`` should name ``fqn``.

        The short name when it resolves back to the same element from there —
        which is how the rest of the inventory is written (§2.2) — and the
        qualified name when it does not.
        """
        short = short_name(fqn)
        if self.inventory.lookup(short, namespace=namespace).fqn == fqn:
            return short
        return _reference(fqn, namespace)

    def _declared_links(self) -> dict[tuple[Endpoint, Endpoint], str]:
        links: dict[tuple[Endpoint, Endpoint], str] = {}
        for fqn, cable in self.inventory.cables.items():
            namespace = namespace_of(fqn)
            ends: list[Endpoint] = []
            for reference in cable.spec.endpoints:
                target = self.inventory.resolve_fqn(reference.device, namespace=namespace)
                if target is None:
                    break
                ends.append((target, reference.interface))
            if len(ends) == 2:
                first, second = sorted(ends)
                links[(first, second)] = fqn
        return links

    def _link_key(self, cable: DraftCable) -> tuple[Endpoint, Endpoint]:
        ends = [(self.keys.get(device, device), port) for device, port in cable.key]
        first, second = sorted(ends)
        return (first, second)

    def _link_in_scope(self, key: tuple[Endpoint, Endpoint]) -> bool:
        names = [name for device, _ in key for name in (device, short_name(device))]
        if self.spec.is_excluded(*names):
            return False
        return any(self._selected(device, short_name(device)) for device, _ in key)

    def _adopt_link(self, fqn: str, cable: DraftCable) -> None:
        document = self.documents[fqn]
        spec = document.setdefault("spec", {})
        # ``medium`` is required of a document, so the draft always holds one;
        # ``medium_stated`` is what says whether an input supplied it or the
        # importer fell back to copper. Only the former is an observation.
        observations = {
            "medium": cable.medium if cable.medium_stated else None,
            "speed": cable.speed,
            "label": cable.label,
        }
        for name, value in observations.items():
            if value is None or spec.get(name) == value:
                continue
            spec[name] = value
            self.touched.add(fqn)

    def _create_link(self, key: tuple[Endpoint, Endpoint], cable: DraftCable) -> None:
        (device_a, port_a), (device_b, port_b) = key
        namespace = _common_namespace(device_a, device_b)
        derived = f"cbl-{short_name(device_a)}-{short_name(device_b)}"
        name = cable.name or element_name(derived)[0] or "cbl"
        fqn = _unused(qualify(namespace, name), self.documents)
        spec: dict[str, Any] = {
            "endpoints": [
                f"{self._spell(device_a, namespace)}:{port_a}",
                f"{self._spell(device_b, namespace)}:{port_b}",
            ],
            "medium": cable.medium or "copper",
        }
        for name_ in ("speed", "label"):
            value = getattr(cable, name_)
            if value is not None:
                spec[name_] = value
        self.documents[fqn] = {
            "apiVersion": API_VERSION,
            "kind": "cable",
            "metadata": {"name": short_name(fqn)},
            "spec": spec,
        }
        self.namespaces[fqn] = namespace
        self.touched.add(fqn)

    def _drop_link(
        self, fqn: str, key: tuple[Endpoint, Endpoint], terminated: set[Endpoint]
    ) -> None:
        """A declared cable no capture reported.

        Removed only where a port *contradicts* it — where the capture shows one
        of its ends plugged into something else. A port simply not mentioned is
        no evidence at all: a device that does not speak LLDP, or a run left off
        a cabling list, is invisible to the capture, and a plan that proposed
        pulling every such cable would be unusable on any real network.
        """
        if not any(endpoint in terminated for endpoint in key):
            (device_a, port_a), (device_b, port_b) = key
            self.notes.append(
                f"{fqn}: kept the cable; no input shows {device_a}:{port_a} or "
                f"{device_b}:{port_b} connected to anything else"
            )
            return
        self.removed.add(fqn)

    # -- assembly --------------------------------------------------------

    def _build(self) -> Inventory:
        """Re-index everything, re-validating only what was actually touched.

        An untouched element is carried over as the model object the loader
        already built for it. That is not only faster, it is safer: nothing that
        the capture never spoke about can be changed by a round trip through
        the schema.
        """
        target = Inventory(root=self.inventory.root)
        for fqn, element in self.inventory.elements.items():
            if fqn in self.removed:
                continue
            adopted = self._element(fqn, element)
            target.add(
                adopted,
                namespace=namespace_of(fqn),
                source=self.inventory.sources.get(fqn) or _synthetic(self.inventory, fqn),
            )
        for fqn in self.documents:
            if fqn in self.inventory.elements or fqn in self.removed:
                continue
            created = self._parse(fqn)
            if created is not None:
                target.add(
                    created,
                    namespace=self.namespaces.get(fqn, namespace_of(fqn)),
                    source=_synthetic(self.inventory, fqn),
                )
        for fqn, layout in self.inventory.layouts.items():
            target.add_layout(
                layout,
                namespace=namespace_of(fqn),
                source=self.inventory.layout_sources[fqn],
            )
        return target

    def _element(self, fqn: str, element: Element) -> Element:
        if fqn not in self.touched:
            return element
        parsed = self._parse(fqn)
        return element if parsed is None else parsed

    def _parse(self, fqn: str) -> Element | None:
        try:
            return parse_document(self.documents[fqn], source=f"capture:{fqn}")
        except SchemaError as error:
            self.rejected.append(f"{fqn}: {error}")
            return None


def adopt(
    inventory: Inventory,
    draft: Draft,
    *,
    coverage: Coverage | None = None,
    spec: CompareSpec = EVERYTHING,
) -> Adoption:
    """The inventory as it would read if it agreed with ``draft``.

    Args:
        inventory: The declared tree, as loaded from disk.
        draft: The captured network, built by the importer's dialect readers.
        coverage: What the capture could see. Derived from ``draft`` when
            omitted, as the command does.
        spec: Element and interface filters, the same ones ``drift`` takes.

    Returns:
        An :class:`Adoption` holding the target inventory and the notes about
        what the capture was not entitled to change.
    """
    return _Adoption(
        inventory=inventory,
        draft=draft,
        coverage=coverage if coverage is not None else coverage_of(draft),
        spec=spec,
    ).run()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_a_box(element: Element) -> bool:
    """A capture naming a *device* never means a cable or a tunnel."""
    return element.kind not in ("cable", "tunnel")


def _adopts_kind(declared: str, observed: str | None) -> bool:
    """``computer`` is the importer's "I could not tell", so it is never evidence."""
    if not observed or observed == "computer" or declared in _UNOBSERVABLE_KINDS:
        return False
    return observed != declared


def _adopts_type(declared: str, observed: str) -> bool:
    """A capture's ``ethernet`` is compatible with ``wifi`` and settles nothing."""
    if declared == observed:
        return False
    return not (observed == "ethernet" and declared in _NIC_TYPES)


def _interface_document(seen: DraftInterface) -> dict[str, Any]:
    """One observed interface, in the shape §6.2 writes it."""
    entry: dict[str, Any] = {"name": seen.name, "type": seen.type}
    for name in _SCALARS:
        value = getattr(seen, name)
        if value is not None:
            entry[name] = value
    for family in ("ipv4", "ipv6"):
        addresses = _normalised(getattr(seen, family))
        if addresses:
            entry[family] = {"addresses": addresses}
    if seen.members:
        entry["members"] = list(seen.members)
    _adopt_vlan(entry, seen)
    return entry


def _adopt_vlan(entry: dict[str, Any], seen: DraftInterface) -> bool:
    """``vlan.mode``, its access VLAN, and — as a union only — its trunk set.

    No dialect netgraph reads prints a port's whole VLAN set
    (:meth:`~netgraph.drift.coverage.Coverage.observes_trunk_vlans`), so an
    observed trunk list is evidence that a VLAN *is* carried and never evidence
    that another one is not: it is merged in, never substituted. That is also
    what keeps the adoption valid — ``NG-V002`` requires a trunk port to name
    the VLANs it carries, and a mode taken from the capture without them would
    produce a document the schema refuses.
    """
    observed = seen.vlan
    if observed is None:
        return False
    block: dict[str, Any] = dict(entry.get("vlan") or {})
    changed = False
    if block.get("mode") != observed.mode:
        block["mode"] = observed.mode
        changed = True
    if observed.mode == "access":
        if observed.access_vlan is not None and block.get("access_vlan") != observed.access_vlan:
            block["access_vlan"] = observed.access_vlan
            changed = True
    else:
        carried = sorted({*_vlan_ids(block.get("trunk_vlans")), *observed.trunk_vlans})
        if carried and carried != _vlan_ids(block.get("trunk_vlans")):
            block["trunk_vlans"] = carried
            changed = True
    if changed:
        entry["vlan"] = block
    return changed


def _vlan_ids(value: Any) -> list[int]:
    """``[10, 20]``, ``"10,20"`` and ``"10-12"`` all read as a list of ids.

    A ``vlan-set`` may be written three ways (§6.4) and a declared document may
    hold any of them, while a capture only ever produces a list of integers.
    Comparing the two as sets of numbers is the only way to tell whether the
    observation adds anything.
    """
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    items = value.split(",") if isinstance(value, str) else value
    ids: set[int] = set()
    for item in items:
        text = str(item).strip()
        first, separator, last = text.partition("-")
        try:
            ids.update(range(int(first), int(last) + 1) if separator else [int(first)])
        except ValueError:  # pragma: no cover - a validated document holds numbers
            continue
    return sorted(ids)


def _declared_addresses(entry: Mapping[str, Any], family: str) -> list[str]:
    config = entry.get(family)
    if not isinstance(config, Mapping):
        return []
    out: list[str] = []
    for address in config.get("addresses", ()):
        if isinstance(address, Mapping):
            out.append(f"{address.get('ip')}/{address.get('prefix_length')}")
        else:
            out.append(str(address))
    return out


def _normalised(values: Sequence[str]) -> list[str]:
    """Canonical ``a.b.c.d/len`` strings, deduplicated, in the order given.

    A difference in *spelling* — upper-case hex, an uncompressed IPv6 run — is
    not a difference in address, and normalising both sides here is what keeps
    the diff from proposing to rewrite an address as itself.
    """
    out: list[str] = []
    for value in values:
        text = _normalise(str(value))
        if text not in out:
            out.append(text)
    return out


def _normalise(value: str) -> str:
    try:
        return str(ipaddress.ip_interface(value))
    except ValueError:
        return value


def _common_namespace(first: str, second: str) -> str:
    """The nearest namespace that holds both ends, so the cable sits between them."""
    left = namespace_of(first).split("/") if namespace_of(first) else []
    right = namespace_of(second).split("/") if namespace_of(second) else []
    shared: list[str] = []
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        shared.append(a)
    return "/".join(shared)


def _reference(fqn: str, namespace: str) -> str:
    """How to spell a reference to ``fqn`` from inside ``namespace``."""
    if namespace and fqn.startswith(f"{namespace}/"):
        return fqn[len(namespace) + 1 :]
    return fqn


def _unused(fqn: str, taken: Mapping[str, Any]) -> str:
    if fqn not in taken:
        return fqn
    for suffix in range(2, 1000):  # pragma: no branch - a thousand clashes is a bug
        candidate = f"{fqn}-{suffix}"
        if candidate not in taken:
            return candidate
    raise AssertionError("could not find an unused name")  # pragma: no cover


def _synthetic(inventory: Inventory, fqn: str) -> SourceLocation:
    """Provenance for a document the capture produced: there is no file yet."""
    return SourceLocation(path=inventory.root / "-", relative="-", index=0)
