"""The comparison core: a declared inventory against a captured one.

``netgraph import`` reads a live network and writes YAML. This inverts it: the
YAML is the assertion and the capture is the evidence, and what comes out is the
list of places where the two disagree. The parsing is the importer's, verbatim —
:mod:`netgraph.importer.lldp`, :mod:`netgraph.importer.iproute` and
:mod:`netgraph.importer.csvlinks` produce the same
:class:`~netgraph.importer.draft.Draft` here as they do there — so a dialect
netgraph can import is a dialect netgraph can check against, with no second
parser to keep in step.

**The rule that makes the answer trustworthy.** A capture is always partial, so
the comparison never treats an absence as a deletion unless the dialect that
produced the capture says an absence is meaningful. That judgement lives in
:mod:`netgraph.drift.coverage`; everything absent that it does not vouch for
becomes an :class:`~netgraph.drift.model.Unobserved`, which is reported in its
own section and never counted as drift.

**What is compared, and what deliberately is not.**

*Compared.* Device kind, when the capture determined one. The interface set of a
host an ``iproute`` capture covered. Per interface: ``mac``, ``mtu``, ``enabled``
and ``type``, the address list per family, bridge and bond membership, the VLAN
mode and access VLAN of a sub-interface, and every VLAN a port was seen carrying.
Links, from both ends. Per cable: ``medium``, ``speed`` and ``label``.

*Not compared, on purpose.*

``description``
    An LLDP chassis or port description is a vendor's prose (``Debian GNU/Linux
    12 (bookworm) Linux 6.1.0-18-amd64``); the inventory's is a human's note
    about intent. They are different fields that happen to share a name, and
    every device would drift on the first run.
``vendor``, ``model``, ``serial``, ``location``
    No dialect netgraph reads reports any of them as a field. LLDP's ``descr``
    sometimes contains a model somewhere inside a sentence, and extracting it
    would be guesswork presented as a measurement.
``type`` when the capture says ``ethernet``
    ``ip`` reports a wireless NIC, a 10G port and a USB dongle alike as
    ``link_type: ether``. ``ethernet`` from a capture therefore means "a NIC",
    not "not wifi", so it is only compared when the capture found something more
    specific — a bridge, a bond, a VLAN sub-interface.
A field the inventory leaves unset
    An absent ``mtu:`` is not a claim that the interface has no MTU, so a
    capture reporting one contradicts nothing. Silence cannot drift.
``spec.vlans``, the device VLAN database
    Every VLAN a capture observes is observed *on a port*, and that is where it
    is reported. Repeating it against the database would list the same
    difference twice under two names.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Final

from netgraph.drift.coverage import Coverage, coverage_of
from netgraph.drift.model import Change, Direction, DriftReport, Unobserved
from netgraph.importer.draft import Draft, DraftCable, DraftDevice, DraftInterface, Endpoint
from netgraph.loader.inventory import Inventory, namespace_of, short_name
from netgraph.models import Adapter, Cable, Device, ElementBase, format_bitrate
from netgraph.models.interface import Interface, VlanMode

__all__ = ["EVERYTHING", "CompareSpec", "compare"]

#: Declared interface types no dialect can report, so their absence from a
#: capture is never a deletion. ``loopback`` is skipped by
#: :mod:`netgraph.importer.iproute` by design (it terminates no cable and holds
#: only host-scope addresses); ``tunnel`` is a two-ended element that ``ip``
#: only ever shows one end of.
_UNIMPORTED_TYPES: Final[frozenset[str]] = frozenset({"loopback", "tunnel"})

#: Scalar interface fields compared when both sides carry a value. ``type`` is
#: absent because it needs :meth:`_Comparison._compare_type`'s judgement about
#: what ``ethernet`` from a capture does and does not mean.
_SCALAR_FIELDS: Final[tuple[str, ...]] = ("enabled", "mac", "mtu", "parent")

#: Interface types a capture reporting ``link_type: ether`` is consistent with.
#: ``ip`` cannot tell a wireless NIC from a wired one, so neither can netgraph.
_NIC_TYPES: Final[frozenset[str]] = frozenset({"ethernet", "wifi"})

#: Scalar cable fields, likewise.
_CABLE_FIELDS: Final[tuple[str, ...]] = ("medium", "speed", "label")

#: Kinds that are netgraph's own abstraction rather than a thing on the wire. No
#: capture format has a word for any of them, so an observed ``kind`` is never
#: evidence against one. Mirrors :data:`netgraph.plan.live._UNOBSERVABLE_KINDS`.
_UNOBSERVABLE_KINDS: Final[frozenset[str]] = frozenset(
    {"adapter", "patchpanel", "pdu", "user", "group"}
)


@dataclass(frozen=True, slots=True)
class CompareSpec:
    """Which part of the inventory the comparison covers.

    ``only`` and ``exclude`` are shell-style globs matched against both the
    fully-qualified and the short name of an element, so ``--only 'sw-*'`` and
    ``--only 'sites/north/*'`` both work. A link is compared when at least one
    of its ends is selected and neither is excluded: a cable to a device that is
    out of scope is out of scope too, but a cable to a device that simply was
    not asked for still belongs to the device that was.
    """

    only: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    #: Interface-name globs the capture was taken with (``--exclude-interface``).
    #: A declared interface matching one cannot be missing from the capture,
    #: because the capture was told not to look at it.
    ignore_interfaces: tuple[str, ...] = ()

    def includes(self, *names: str) -> bool:
        """Is an element with these names in scope?"""
        if self.exclude and any(_matches(name, self.exclude) for name in names):
            return False
        return not self.only or any(_matches(name, self.only) for name in names)

    def is_excluded(self, *names: str) -> bool:
        """Was an element with these names taken out of scope explicitly?"""
        return bool(self.exclude) and any(_matches(name, self.exclude) for name in names)

    def ignores_interface(self, name: str) -> bool:
        return _matches(name, self.ignore_interfaces)


def _matches(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


#: "Compare everything." A module-level singleton rather than a default built at
#: call time: the spec is frozen, so one instance serves every caller.
EVERYTHING: Final = CompareSpec()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def compare(
    inventory: Inventory,
    draft: Draft,
    *,
    coverage: Coverage | None = None,
    spec: CompareSpec = EVERYTHING,
    inputs: Sequence[str] = (),
) -> DriftReport:
    """Compare what ``inventory`` declares with what ``draft`` observed.

    Args:
        inventory: The declared tree, as loaded from disk.
        draft: The captured network, built by the importer's dialect readers.
        coverage: What the capture could see. Derived from ``draft`` when
            omitted, which is what the command does.
        spec: Element and interface filters.
        inputs: Input names, for the report header.

    Returns:
        A :class:`~netgraph.drift.model.DriftReport` whose two lists are sorted
        and whose :attr:`~netgraph.drift.model.DriftReport.drifted` flag is true
        only for real disagreements.
    """
    return _Comparison(
        inventory=inventory,
        draft=draft,
        coverage=coverage if coverage is not None else coverage_of(draft),
        spec=spec,
    ).run(inputs)


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Comparison:
    """One run, accumulating changes and blind spots as it goes."""

    inventory: Inventory
    draft: Draft
    coverage: Coverage
    spec: CompareSpec
    changes: list[Change] = field(default_factory=list)
    blind: list[Unobserved] = field(default_factory=list)
    #: Observed device name to the fully-qualified name it resolved to.
    resolved: dict[str, str] = field(default_factory=dict)
    #: Element name used for an observed device, declared or not.
    keys: dict[str, str] = field(default_factory=dict)
    #: The inverse of :attr:`keys`, so a declared name can be traced back to the
    #: capture's name for it without a scan per lookup.
    names: dict[str, str] = field(default_factory=dict)
    compared: list[str] = field(default_factory=list)
    _compared: set[str] = field(default_factory=set)
    filtered: set[str] = field(default_factory=set)

    # -- driver ----------------------------------------------------------

    def run(self, inputs: Sequence[str]) -> DriftReport:
        self._resolve_devices()
        self._compare_devices()
        self._compare_links()
        return DriftReport(
            root=self.inventory.root,
            inputs=tuple(inputs),
            dialects=self.coverage.used,
            changes=tuple(sorted(self.changes, key=lambda change: change.order)),
            unobserved=tuple(sorted(self.blind, key=lambda entry: entry.order)),
            compared=tuple(self.compared),
            observed=tuple(self.draft.devices),
            filtered=tuple(sorted(self.filtered)),
        )

    def _resolve_devices(self) -> None:
        """Match every observed device name against the declared tree.

        Resolution goes through :meth:`~netgraph.loader.inventory.Inventory.lookup`,
        so a capture naming ``sw-core-01`` finds ``sites/north/sw-core-01``
        wherever the inventory keeps it. A name that matches several elements is
        left unresolved: guessing which one the capture meant would put the
        differences of one device on another's report.
        """
        for name in self.draft.devices:
            resolution = self.inventory.lookup(name)
            element = resolution.element
            if resolution.fqn is not None and _is_comparable(element):
                self.resolved[name] = resolution.fqn
                self.keys[name] = resolution.fqn
            else:
                self.keys[name] = name
            self.names.setdefault(self.keys[name], name)

    def _selected(self, *names: str) -> bool:
        return self.spec.includes(*names)

    # -- devices ---------------------------------------------------------

    def _compare_devices(self) -> None:
        for name, observed in self.draft.devices.items():
            fqn = self.resolved.get(name)
            key = self.keys[name]
            if not self._selected(key, short_name(key)):
                self.filtered.add(key)
                continue
            if fqn is None:
                self._undeclared_device(name, observed)
                continue
            declared = self.inventory[fqn]
            self.compared.append(fqn)
            self._compared.add(fqn)
            self._compare_device(fqn, declared, observed)

        for fqn, owner in self.inventory.interface_owners.items():
            if fqn in self._compared:
                continue
            if not self._selected(fqn, short_name(fqn)):
                self.filtered.add(fqn)
                continue
            self.blind.append(
                Unobserved(
                    element=fqn,
                    kind=owner.kind,
                    scope="device",
                    reason=(
                        "no input covered this device, so nothing about it could be "
                        "confirmed or denied"
                    ),
                )
            )

    def _undeclared_device(self, name: str, observed: DraftDevice) -> None:
        seen = ", ".join(observed.sources) or "the capture"
        self.changes.append(
            Change(
                direction=Direction.UNDECLARED,
                scope="device",
                element=name,
                kind=None,
                observed=_kind_text(observed),
                message=(
                    f"{seen} observed a device called {name!r}; the inventory declares no "
                    "element of that name"
                ),
            )
        )

    def _compare_device(self, fqn: str, declared: ElementBase, observed: DraftDevice) -> None:
        self._compare_device_kind(fqn, declared, observed)
        if not isinstance(declared, (Device, Adapter)):
            return
        self._compare_interfaces(fqn, declared, observed)

    def _compare_device_kind(self, fqn: str, declared: ElementBase, observed: DraftDevice) -> None:
        """``kind:``, but only where the capture actually determined one.

        ``computer`` is the importer's neutral fallback for a box nothing said
        anything about (:meth:`~netgraph.importer.draft.Draft.device`), so it is
        never evidence. An adapter, a patch panel, a PDU, a user and a group are
        netgraph's own abstractions, which no capture format has a word for.
        """
        if observed.kind == "computer" or declared.kind in _UNOBSERVABLE_KINDS:
            return
        if observed.kind == declared.kind:
            return
        self.changes.append(
            Change(
                direction=Direction.DISAGREES,
                scope="device",
                element=fqn,
                kind=declared.kind,
                field="kind",
                declared=declared.kind,
                observed=observed.kind,
                message=(
                    f"declared as a {declared.kind}; the capture reports a {observed.kind}"
                    + (f" ({observed.kind_comment})" if observed.kind_comment else "")
                ),
            )
        )

    # -- interfaces ------------------------------------------------------

    def _compare_interfaces(
        self, fqn: str, declared: Device | Adapter, observed: DraftDevice
    ) -> None:
        by_name = {entry.name: entry for entry in declared.spec.interfaces}
        for name, seen in observed.interfaces.items():
            counterpart = by_name.get(name)
            if counterpart is None:
                self._undeclared_interface(fqn, declared, seen)
                continue
            self._compare_interface(fqn, declared, observed, counterpart, seen)

        for name, entry in by_name.items():
            if name in observed.interfaces:
                continue
            self._missing_interface(fqn, declared, observed, entry)

    def _undeclared_interface(
        self, fqn: str, declared: ElementBase, interface: DraftInterface
    ) -> None:
        self.changes.append(
            Change(
                direction=Direction.UNDECLARED,
                scope="interface",
                element=fqn,
                kind=declared.kind,
                path=interface.name,
                observed=interface.type,
                message=(
                    f"the capture reports this interface as {interface.type}; the inventory "
                    "does not declare it"
                ),
            )
        )

    def _missing_interface(
        self,
        fqn: str,
        declared: ElementBase,
        observed: DraftDevice,
        interface: Interface,
    ) -> None:
        """A declared interface no capture reported — drift, or a blind spot."""
        reason = self._interface_blind_spot(observed.name, interface)
        if reason is not None:
            self.blind.append(
                Unobserved(
                    element=fqn,
                    kind=declared.kind,
                    scope="interface",
                    path=interface.name,
                    items=(interface.name,),
                    reason=reason,
                )
            )
            return
        self.changes.append(
            Change(
                direction=Direction.MISSING,
                scope="interface",
                element=fqn,
                kind=declared.kind,
                path=interface.name,
                declared=interface.type.value,
                message=(
                    f"declared as {interface.type.value}, but the capture lists every "
                    f"interface of {observed.name} and this is not among them"
                ),
            )
        )

    def _interface_blind_spot(self, device: str, interface: Interface) -> str | None:
        """Why a missing interface is not a deletion, or ``None`` when it is one."""
        if not self.coverage.observes_interfaces(device):
            dialects = ", ".join(self.coverage.dialects_of(device)) or "the capture"
            return (
                f"no input lists every interface of this device; {dialects} reports only the "
                "ports it happened to see"
            )
        if interface.type.value in _UNIMPORTED_TYPES:
            return (
                f"the 'iproute' dialect does not import {interface.type.value} interfaces, so "
                "this one could not appear in the capture"
            )
        if self.spec.ignores_interface(interface.name):
            return "--exclude-interface kept this interface out of the capture"
        return None

    def _compare_interface(
        self,
        fqn: str,
        declared: ElementBase,
        observed: DraftDevice,
        interface: Interface,
        seen: DraftInterface,
    ) -> None:
        unseen: list[str] = []
        if self._compare_type(fqn, declared, observed, interface, seen) is _UNSEEN:
            unseen.append("type")
        for name in _SCALAR_FIELDS:
            state = self._compare_scalar(fqn, declared, interface, seen, name)
            if state is _UNSEEN:
                unseen.append(name)
        self._compare_addresses(fqn, declared, observed, interface, seen, unseen)
        self._compare_members(fqn, declared, observed, interface, seen)
        self._compare_vlans(fqn, declared, interface, seen)
        if unseen:
            self.blind.append(
                Unobserved(
                    element=fqn,
                    kind=declared.kind,
                    scope="field",
                    path=interface.name,
                    items=tuple(unseen),
                    reason=(
                        "the capture reports no value for these fields, so the declared ones "
                        "could not be checked"
                    ),
                )
            )

    def _compare_type(
        self,
        fqn: str,
        declared: ElementBase,
        observed: DraftDevice,
        interface: Interface,
        seen: DraftInterface,
    ) -> object:
        """``type:``, given that ``ethernet`` from a capture means "a NIC".

        ``ip`` prints ``link_type: ether`` for a wired NIC, a wireless one and a
        USB dongle alike, so the importer's ``ethernet`` is compatible with both
        ``ethernet`` and ``wifi`` and distinguishes neither. It *is* evidence
        against a stacking type, though: a bridge, a bond or a VLAN
        sub-interface would have carried ``linkinfo``, so a port declared as one
        and reported as a plain NIC is a real difference — where the capture
        lists every interface, which is the only case in which its silence about
        ``linkinfo`` means anything.
        """
        expected, found = interface.type.value, seen.type
        if expected == found:
            return _AGREES
        if found == "ethernet":
            if expected in _NIC_TYPES:
                return _AGREES
            if not self.coverage.observes_interfaces(observed.name):
                return _UNSEEN
        self.changes.append(
            Change(
                direction=Direction.DISAGREES,
                scope="field",
                element=fqn,
                kind=declared.kind,
                path=interface.name,
                field="type",
                declared=expected,
                observed=found,
                message=(f"declared as a {expected} interface; the capture reports {found}"),
            )
        )
        return _DIFFERS

    def _compare_scalar(
        self,
        fqn: str,
        declared: ElementBase,
        interface: Interface,
        seen: DraftInterface,
        name: str,
    ) -> object:
        """One scalar field of an interface. Returns the outcome marker."""
        observed = _scalar_value(seen, name)

        if observed is None:
            # Only a *declared* value can go unchecked; an undeclared one was
            # never an assertion in the first place.
            return _UNSEEN if _declared_value(interface, name) is not None else _AGREES
        expected = _declared_value(interface, name)
        if expected is None or expected == observed:
            return _AGREES
        self.changes.append(
            Change(
                direction=Direction.DISAGREES,
                scope="field",
                element=fqn,
                kind=declared.kind,
                path=interface.name,
                field=name,
                declared=expected,
                observed=observed,
                message=f"declared as {expected}; the capture reports {observed}",
            )
        )
        return _DIFFERS

    def _compare_addresses(
        self,
        fqn: str,
        declared: ElementBase,
        observed: DraftDevice,
        interface: Interface,
        seen: DraftInterface,
        unseen: list[str],
    ) -> None:
        for family in ("ipv4", "ipv6"):
            expected = _declared_addresses(interface, family)
            found = _normalised_addresses(getattr(seen, family))
            for address in sorted(found - expected):
                self.changes.append(
                    Change(
                        direction=Direction.UNDECLARED,
                        scope="address",
                        element=fqn,
                        kind=declared.kind,
                        path=interface.name,
                        field=family,
                        observed=address,
                        message=(
                            f"the capture reports {address} here; the inventory does not declare it"
                        ),
                    )
                )
            absent = sorted(expected - found)
            if not absent:
                continue
            if self.coverage.observes_addresses(observed.name, family):
                for address in absent:
                    self.changes.append(
                        Change(
                            direction=Direction.MISSING,
                            scope="address",
                            element=fqn,
                            kind=declared.kind,
                            path=interface.name,
                            field=family,
                            declared=address,
                            message=(
                                f"{address} is declared here; the capture reports this "
                                "host's addresses and that is not one of them"
                            ),
                        )
                    )
            else:
                self.blind.append(
                    Unobserved(
                        element=fqn,
                        kind=declared.kind,
                        scope="address",
                        path=f"{interface.name}.{family}",
                        items=tuple(absent),
                        reason=(
                            f"no input reported an {family} address for this device; "
                            "'ip -j addr show' does, 'ip -j link show' and LLDP do not"
                        ),
                    )
                )

    def _compare_members(
        self,
        fqn: str,
        declared: ElementBase,
        observed: DraftDevice,
        interface: Interface,
        seen: DraftInterface,
    ) -> None:
        expected = set(interface.members or ())
        found = set(seen.members)
        if not expected and not found:
            return
        for member in sorted(found - expected):
            self.changes.append(
                Change(
                    direction=Direction.UNDECLARED,
                    scope="member",
                    element=fqn,
                    kind=declared.kind,
                    path=interface.name,
                    field="members",
                    observed=member,
                    message=(
                        f"{member!r} is enslaved here on the device; the inventory does not "
                        "list it as a member"
                    ),
                )
            )
        absent = sorted(expected - found)
        if not absent:
            return
        if found and self.coverage.observes_members(observed.name):
            for member in absent:
                self.changes.append(
                    Change(
                        direction=Direction.MISSING,
                        scope="member",
                        element=fqn,
                        kind=declared.kind,
                        path=interface.name,
                        field="members",
                        declared=member,
                        message=(
                            f"{member!r} is declared as a member; the capture reports what "
                            "this aggregate holds and that is not one of them"
                        ),
                    )
                )
            return
        self.blind.append(
            Unobserved(
                element=fqn,
                kind=declared.kind,
                scope="member",
                path=f"{interface.name}.members",
                items=tuple(absent),
                reason="no input reported what this bridge or bond aggregates",
            )
        )

    def _compare_vlans(
        self,
        fqn: str,
        declared: ElementBase,
        interface: Interface,
        seen: DraftInterface,
    ) -> None:
        """VLAN mode, access VLAN, and every VLAN the port was seen carrying.

        The observed block is one of two things, and the difference matters. A
        VLAN sub-interface yields a real observation — ``ip`` printed the id. A
        parent port yields an *inference*: the sub-interfaces stacked on it can
        only work if it trunks their VLANs, so
        :func:`~netgraph.importer.iproute._infer_parent_trunks` writes the
        minimum set that must be there and marks it with a comment. That set is
        a lower bound, so a VLAN in it and not in the inventory is a real
        difference while the converse says nothing at all.
        """
        observed = seen.vlan
        if observed is None:
            if interface.vlan is not None:
                self.blind.append(
                    Unobserved(
                        element=fqn,
                        kind=declared.kind,
                        scope="vlan",
                        path=f"{interface.name}.vlan",
                        items=(str(interface.vlan.mode.value),),
                        reason=(
                            "no input reported the VLAN configuration of this port; only a "
                            "VLAN sub-interface makes one visible to 'ip'"
                        ),
                    )
                )
            return

        inferred = observed.comment is not None
        if not inferred:
            self._compare_vlan_scalars(
                fqn, declared, interface, observed.mode, observed.access_vlan
            )

        carried = _declared_vlans(interface)
        for vlan in sorted({*observed.trunk_vlans} - carried):
            self.changes.append(
                Change(
                    direction=Direction.UNDECLARED,
                    scope="vlan",
                    element=fqn,
                    kind=declared.kind,
                    path=interface.name,
                    field="vlan",
                    observed=str(vlan),
                    message=(
                        f"VLAN {vlan} is carried on this port; the inventory declares "
                        f"{_vlan_text(carried)} here"
                    ),
                )
            )

        absent = sorted(_declared_trunk_vlans(interface) - {*observed.trunk_vlans})
        if absent:
            self.blind.append(
                Unobserved(
                    element=fqn,
                    kind=declared.kind,
                    scope="vlan",
                    path=f"{interface.name}.vlan.trunk_vlans",
                    items=tuple(str(vlan) for vlan in absent),
                    reason=(
                        "no dialect netgraph reads prints the VLAN set of a trunk; what was "
                        "observed is the minimum the stacked sub-interfaces imply"
                    ),
                )
            )

    def _compare_vlan_scalars(
        self,
        fqn: str,
        declared: ElementBase,
        interface: Interface,
        mode: str,
        access_vlan: int | None,
    ) -> None:
        expected = interface.vlan
        if expected is None:
            return
        if expected.mode.value != mode:
            self.changes.append(
                Change(
                    direction=Direction.DISAGREES,
                    scope="vlan",
                    element=fqn,
                    kind=declared.kind,
                    path=interface.name,
                    field="vlan.mode",
                    declared=expected.mode.value,
                    observed=mode,
                    message=(
                        f"declared as a {expected.mode.value} port; the capture reports {mode}"
                    ),
                )
            )
        if (
            access_vlan is not None
            and expected.access_vlan is not None
            and expected.access_vlan != access_vlan
        ):
            self.changes.append(
                Change(
                    direction=Direction.DISAGREES,
                    scope="vlan",
                    element=fqn,
                    kind=declared.kind,
                    path=interface.name,
                    field="vlan.access_vlan",
                    declared=str(expected.access_vlan),
                    observed=str(access_vlan),
                    message=(
                        f"declared in VLAN {expected.access_vlan}; the capture reports "
                        f"VLAN {access_vlan}"
                    ),
                )
            )

    # -- links -----------------------------------------------------------

    def _compare_links(self) -> None:
        """Cables, matched by the endpoint pair rather than by name.

        A capture never learns what a cable is *called*, so a name-based match
        would report every link twice. The identity of a link is the two
        ``device:interface`` ends it joins, sorted — which is exactly what
        :attr:`~netgraph.importer.draft.DraftCable.key` already is, once the
        observed device names have been mapped onto the declared ones.
        """
        declared = self._declared_links()
        observed = {self._link_key(cable): cable for cable in self.draft.cables.values()}
        terminated = {endpoint for key in observed for endpoint in key}

        for key, seen in observed.items():
            if key in declared:
                fqn, cable = declared[key]
                # A link that matched *was* compared, whatever it turned up, and
                # a report format that lists what it checked has to know that.
                self.compared.append(fqn)
                self._compared.add(fqn)
                self._compare_link_fields(fqn, cable, seen)
                continue
            if not self._link_in_scope(key):
                continue
            self._undeclared_link(key, seen)

        for key, (fqn, _) in declared.items():
            if key in observed:
                continue
            if not self._link_in_scope(key):
                self.filtered.add(fqn)
                continue
            self._missing_link(fqn, key, terminated)

    def _declared_links(self) -> dict[tuple[Endpoint, Endpoint], tuple[str, Cable]]:
        """Every declared cable, keyed by its resolved endpoint pair.

        A cable whose endpoint names do not resolve is skipped: that is
        ``E001``'s business, and reporting it here as a link the network lacks
        would blame the network for a broken reference.
        """
        links: dict[tuple[Endpoint, Endpoint], tuple[str, Cable]] = {}
        for fqn, cable in self.inventory.cables.items():
            namespace = namespace_of(fqn)
            endpoints: list[Endpoint] = []
            for ref in cable.spec.endpoints:
                target = self.inventory.resolve_fqn(ref.device, namespace=namespace)
                if target is None:
                    break
                endpoints.append((target, ref.interface))
            if len(endpoints) != 2:
                continue
            first, second = sorted(endpoints)
            links[(first, second)] = (fqn, cable)
        return links

    def _link_key(self, cable: DraftCable) -> tuple[Endpoint, Endpoint]:
        """``cable``'s endpoints with observed device names mapped onto declared ones."""
        ends = [(self.keys.get(device, device), port) for device, port in cable.key]
        first, second = sorted(ends)
        return (first, second)

    def _link_in_scope(self, key: tuple[Endpoint, Endpoint]) -> bool:
        names = [name for device, _ in key for name in (device, short_name(device))]
        if self.spec.is_excluded(*names):
            return False
        return any(self._selected(device, short_name(device)) for device, _ in key)

    def _undeclared_link(self, key: tuple[Endpoint, Endpoint], cable: DraftCable) -> None:
        (device_a, port_a), (device_b, port_b) = key
        seen = ", ".join(cable.sources) or "the capture"
        self.changes.append(
            Change(
                direction=Direction.UNDECLARED,
                scope="link",
                element=cable.name or f"{device_a}:{port_a}-{device_b}:{port_b}",
                kind="cable",
                observed=f"{device_a}:{port_a} <-> {device_b}:{port_b}",
                message=(
                    f"{seen} shows {device_a}:{port_a} connected to {device_b}:{port_b}; no "
                    "cable declares that link"
                ),
            )
        )

    def _missing_link(
        self, fqn: str, key: tuple[Endpoint, Endpoint], terminated: set[Endpoint]
    ) -> None:
        """A declared cable no capture saw — drift only where a port contradicts it."""
        (device_a, port_a), (device_b, port_b) = key
        contradicting = [end for end in key if end in terminated]
        if not contradicting:
            self.blind.append(
                Unobserved(
                    element=fqn,
                    kind="cable",
                    scope="link",
                    items=(f"{device_a}:{port_a}", f"{device_b}:{port_b}"),
                    reason=self._link_blind_reason(key),
                )
            )
            return
        ends = ", ".join(f"{device}:{port}" for device, port in contradicting)
        self.changes.append(
            Change(
                direction=Direction.MISSING,
                scope="link",
                element=fqn,
                kind="cable",
                declared=f"{device_a}:{port_a} <-> {device_b}:{port_b}",
                message=(
                    f"the inventory cables {device_a}:{port_a} to {device_b}:{port_b}; the "
                    f"capture shows {ends} connected to something else"
                ),
            )
        )

    def _link_blind_reason(self, key: tuple[Endpoint, Endpoint]) -> str:
        """Why an unseen cable is not a removed cable."""
        observing = [
            device
            for device, _ in key
            if self.coverage.observes_links(self.names.get(device, device))
        ]
        if not observing:
            return (
                "no input reported the neighbours of either end; 'lldp' and 'csv' do, "
                "'iproute' does not"
            )
        return (
            f"{', '.join(observing)} reported no neighbour on this port; a device that does "
            "not speak LLDP, or a port left out of a cabling list, is invisible to the capture"
        )

    def _compare_link_fields(self, fqn: str, declared: Cable, observed: DraftCable) -> None:
        for name in _CABLE_FIELDS:
            value = _cable_observation(observed, name)
            if value is None:
                continue
            expected = _declared_cable_value(declared, name)
            if expected is None or expected == value:
                continue
            self.changes.append(
                Change(
                    direction=Direction.DISAGREES,
                    scope="field",
                    element=fqn,
                    kind="cable",
                    field=name,
                    declared=expected,
                    observed=value,
                    message=f"{name} is declared as {expected}; the capture reports {value}",
                )
            )


# --------------------------------------------------------------------------- #
# Value extraction
# --------------------------------------------------------------------------- #

#: Outcome markers for :meth:`_Comparison._compare_scalar`. Sentinels rather
#: than an enum: they never leave this module and never reach a report.
_AGREES: Final = object()
_DIFFERS: Final = object()
_UNSEEN: Final = object()


def _is_comparable(element: ElementBase | None) -> bool:
    """Can an observed device be matched against this declared element?

    A cable and a tunnel are links, not boxes, and a capture that named one as a
    *device* is naming something else that happens to share the name.
    """
    return element is not None and element.kind not in ("cable", "tunnel")


def _kind_text(observed: DraftDevice) -> str:
    """What the capture concluded a device is, marked when it concluded nothing."""
    return observed.kind if observed.kind != "computer" else "computer (assumed)"


def _scalar_value(seen: DraftInterface, name: str) -> str | None:
    """One observed scalar, rendered, or ``None`` when the capture had none."""
    value = getattr(seen, name)
    if value is None:
        return None
    return _text(value)


def _declared_value(interface: Interface, name: str) -> str | None:
    value = getattr(interface, name)
    if value is None:
        return None
    return _text(value)


def _text(value: object) -> str:
    """One scalar as the report spells it.

    ``bool`` is the only type that needs a decision: ``str(True)`` is ``True``,
    which is Python's spelling of it and not YAML's, and a diagnostic that says
    ``declared as True`` next to a document reading ``enabled: true`` invites
    the reader to wonder whether the two are the same thing.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _declared_addresses(interface: Interface, family: str) -> set[str]:
    config = getattr(interface, family)
    if config is None:
        return set()
    return {_normalise_address(str(address)) for address in config.addresses}


def _normalised_addresses(values: Iterable[str]) -> set[str]:
    return {_normalise_address(value) for value in values}


def _normalise_address(value: str) -> str:
    """``2001:DB8:10::20/64`` → ``2001:db8:10::20/64``.

    ``ip`` and the inventory can spell one address two ways — upper case, an
    uncompressed IPv6 run, a leading zero — and a difference in spelling is not
    a difference in address. Anything :mod:`ipaddress` cannot parse is compared
    verbatim; it cannot have come from a validated model, so it came from a
    capture, and echoing it back unchanged is the honest thing to do.
    """
    try:
        return str(ipaddress.ip_interface(value))
    except ValueError:
        return value


def _declared_vlans(interface: Interface) -> set[int]:
    """Every VLAN the inventory says a port carries, tagged or not."""
    config = interface.vlan
    if config is None:
        return set()
    carried = {*(config.trunk_vlans or ())}
    for single in (config.access_vlan, config.native_vlan):
        if single is not None:
            carried.add(single)
    return carried


def _declared_trunk_vlans(interface: Interface) -> set[int]:
    config = interface.vlan
    if config is None or config.mode is not VlanMode.TRUNK:
        return set()
    return {*(config.trunk_vlans or ())}


def _vlan_text(vlans: Iterable[int]) -> str:
    vlans = set(vlans)
    if not vlans:
        return "no VLAN"
    return "VLAN " + ", ".join(str(vlan) for vlan in sorted(vlans))


def _cable_observation(cable: DraftCable, name: str) -> str | None:
    """One observed cable field, or ``None`` when nothing stated it.

    ``medium`` is the awkward one: it is required of a document, so the draft
    always holds a value, and ``medium_stated`` is what says whether an input
    supplied it or the importer fell back to ``copper``.
    """
    if name == "medium":
        return cable.medium if cable.medium_stated else None
    value = getattr(cable, name)
    return None if value is None else str(value)


def _declared_cable_value(cable: Cable, name: str) -> str | None:
    value = getattr(cable.spec, name)
    if value is None:
        return None
    if name == "speed":
        return format_bitrate(value)
    if name == "medium":
        return str(value.value)
    return str(value)
