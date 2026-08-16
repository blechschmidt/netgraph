"""``--from lldp``: neighbour records from ``lldpctl``/``lldpcli``, as cables.

An LLDP neighbour record is very nearly a ``cable`` document already. It names
the local port, the neighbour's system name and the neighbour's port — which is
exactly the pair of ``device:interface`` endpoints a cable joins — so the
translation is mostly a matter of surviving the shape lldpd chose to print.

Two things make this the highest-value dialect of the three:

* **Both ends at once.** One capture on one host yields the host's ports *and*
  a stub for every neighbour, including neighbours that will never be captured
  themselves — a printer, an unmanaged switch, an access point.
* **It is symmetric, so it is self-checking.** Run on both ends of a link, LLDP
  reports the adjacency twice. :attr:`~netviz.importer.draft.DraftCable.key`
  sorts the endpoint pair, so the second report merges into the first instead of
  producing a duplicate cable — and a link that only ever appears once is
  visible as such, because only one host's name ends up in its comment.

**Shape tolerance.** lldpd has two JSON encodings and has changed both across
releases: ``-f json`` keys objects by name (``{"eth0": {...}}``) and inlines
scalars, ``-f json0`` wraps everything in single-element lists and every scalar
in ``{"value": ...}``. Neither is documented as stable. Rather than pick one,
every access here goes through :func:`_entries`, :func:`_mapping` and
:func:`_text`, which accept all of them; a shape none of them recognises yields
"not present", which is reported, rather than a traceback.

**What is deliberately not imported.** ``mgmt-ip`` is an observed address, but
LLDP says neither which interface holds it nor its prefix length, and a netviz
address needs both. It is recorded as a comment on the device instead of being
placed on an interface behind an invented ``/24``.
"""

from __future__ import annotations

from typing import Any, Final

from netviz.importer.draft import Draft, DraftCable, DraftDevice, comment_text
from netviz.importer.names import element_name, interface_name

__all__ = ["read_lldp"]

#: LLDP system capabilities mapped to the netviz kind they imply, most
#: specific first. A box that says it is bridging is drawn as a switch even when
#: it also routes: in this model an L3 switch is still a switch, and preferring
#: ``Bridge`` also keeps the command away from the failure mode of promoting
#: every host that happens to forward packets into a ``router``.
_KIND_BY_CAPABILITY: Final[tuple[tuple[str, str], ...]] = (
    ("bridge", "switch"),
    ("router", "router"),
    ("repeater", "hub"),
    ("wlan", "switch"),
)

#: Port-id subtypes whose value is the port's *name*. Anything else (a MAC, a
#: network address, an agent circuit id) identifies the port without naming it,
#: so the description is tried before falling back to the id.
_NAMING_PORT_SUBTYPES: Final[frozenset[str]] = frozenset({"ifname", "local", "ifalias"})

#: Keys that mark a mapping as an ``lldp.interface[]`` record rather than as an
#: index keyed by interface name. ``port`` is deliberately absent: a switch port
#: really can be called ``port``, and misreading an index as a record is worse
#: than the other way round. See :func:`_entries`.
_INTERFACE_MARKERS: Final[frozenset[str]] = frozenset({"chassis", "via", "rid", "age", "ttl"})

#: The same, for a chassis record. A system named ``id``, ``descr`` or
#: ``capability`` is not a thing; a chassis carrying those keys and no name is.
_CHASSIS_MARKERS: Final[frozenset[str]] = frozenset(
    {"id", "descr", "capability", "mgmt-ip", "mgmt-iface"}
)


def read_lldp(payload: Any, *, source: str, host: str, draft: Draft) -> None:
    """Fold one ``lldpctl -f json`` capture, taken on ``host``, into ``draft``.

    Args:
        payload: The parsed JSON document.
        source: Name of the input, for comments and the run report.
        host: Element name of the device the capture was taken on. LLDP output
            is a view *from* one device and never says which, so the caller
            supplies it from ``--host`` or the file name.
        draft: Accumulator, mutated in place.
    """
    neighbours = _entries(_lldp_root(payload).get("interface"), markers=_INTERFACE_MARKERS)
    local = draft.device(host)
    local.observed_in(source)
    local.note(
        "the interfaces below are the ones LLDP reported a neighbour on; a port "
        "with no LLDP neighbour is not visible to this capture and is missing here"
    )

    if not neighbours:
        draft.note(f"{source}: no LLDP neighbours in the capture, so no cables came from it")
        return

    for record in neighbours:
        _read_neighbour(record, source=source, local=local, draft=draft)


def _read_neighbour(
    record: dict[str, Any], *, source: str, local: DraftDevice, draft: Draft
) -> None:
    """One ``lldp.interface[]`` entry: a local port and what it can see."""
    local_port = _local_port(record, source=source, local=local, draft=draft)
    if local_port is None:
        return

    chassis_entries = _entries(record.get("chassis"), markers=_CHASSIS_MARKERS)
    if not chassis_entries:
        draft.note(
            f"{source}: the record for {local.name}:{local_port} names no neighbour chassis, "
            "so no cable came from it"
        )
        return

    port = _mapping(record.get("port"))
    for chassis in chassis_entries:
        neighbour = _neighbour_device(chassis, source=source, draft=draft)
        if neighbour is None:
            continue
        remote_port = _remote_port(port, neighbour=neighbour, source=source, draft=draft)
        if remote_port is None:
            continue
        _link(
            local=(local.name, local_port),
            remote=(neighbour.name, remote_port),
            source=source,
            draft=draft,
        )


def _local_port(
    record: dict[str, Any], *, source: str, local: DraftDevice, draft: Draft
) -> str | None:
    """Register the local end of one neighbour record and return its name."""
    raw = _text(record.get("name"))
    if raw is None:
        draft.note(f"{source}: an interface record carries no name and was skipped")
        return None
    name, original = interface_name(raw)
    if name is None:
        draft.note(f"{source}: interface name {raw!r} holds no usable characters and was skipped")
        return None
    interface = local.interface(name)
    if original is not None:
        _note_rename(interface.comments, "interface", original)
    return name


def _neighbour_device(chassis: dict[str, Any], *, source: str, draft: Draft) -> DraftDevice | None:
    """The device a neighbour record names, created on first sight."""
    identifier = _mapping(chassis.get("id"))
    raw = _text(chassis.get("name"))
    from_chassis_id = raw is None
    if from_chassis_id:
        # No system name advertised. The chassis id is still an observed,
        # stable identifier of that box, so it names the element rather than a
        # counter would — and the device carries a comment saying as much.
        raw = _text(identifier.get("value"))
    if raw is None:
        draft.note(f"{source}: a neighbour advertises neither a system name nor a chassis id")
        return None

    name, original = element_name(raw)
    if name is None:
        draft.note(f"{source}: neighbour name {raw!r} holds no usable characters and was skipped")
        return None

    device = draft.device(name)
    device.observed_in(source)
    if original is not None:
        _note_rename(device.comments, "device", original)
    if from_chassis_id:
        device.note(
            "this neighbour advertised no system name, so it is named after the "
            f"{_text(identifier.get('type')) or 'chassis'} id LLDP reported for it"
        )
    _apply_chassis(device, chassis)
    return device


def _apply_chassis(device: DraftDevice, chassis: dict[str, Any]) -> None:
    """Everything a chassis record says about the device it describes."""
    kind, capabilities = _kind_of(chassis)
    if kind is not None:
        device.refine_kind(
            kind,
            f"inferred: LLDP advertises the {', '.join(sorted(capabilities))} capability",
        )
    description = _text(chassis.get("descr"))
    if description is not None and device.description is None:
        device.description = comment_text(description)
    for address in _texts(chassis.get("mgmt-ip")):
        device.note(
            f"LLDP reported management address {address}; it is not on an interface here "
            "because the capture names neither the interface nor the prefix length"
        )


def _kind_of(chassis: dict[str, Any]) -> tuple[str | None, frozenset[str]]:
    """The netviz kind the advertised capabilities imply, if any."""
    capabilities = _capabilities(chassis)
    for capability, kind in _KIND_BY_CAPABILITY:
        if capability in capabilities:
            return kind, capabilities
    return None, capabilities


def _capabilities(chassis: dict[str, Any]) -> frozenset[str]:
    """The system capabilities a chassis advertises *and* has enabled."""
    raw = chassis.get("capability")
    items = raw if isinstance(raw, list) else [raw]
    enabled: set[str] = set()
    for item in items:
        entry = _mapping(item)
        name = _text(entry.get("type"))
        if name is not None and _flag(entry.get("enabled")):
            enabled.add(name.lower())
    return frozenset(enabled)


def _remote_port(
    port: dict[str, Any], *, neighbour: DraftDevice, source: str, draft: Draft
) -> str | None:
    """The neighbour's port, registered on the neighbour device."""
    identifier = _mapping(port.get("id"))
    subtype = (_text(identifier.get("type")) or "").lower()
    value = _text(identifier.get("value"))
    description = _text(port.get("descr"))

    raw = value if subtype in _NAMING_PORT_SUBTYPES and value else description or value
    if raw is None:
        draft.note(
            f"{source}: a neighbour on {neighbour.name} reports no usable port id, "
            "so no cable came from it"
        )
        return None

    name, original = interface_name(raw)
    if name is None:
        draft.note(f"{source}: port name {raw!r} holds no usable characters and was skipped")
        return None

    interface = neighbour.interface(name)
    if original is not None:
        _note_rename(interface.comments, "interface", original)
    if subtype == "mac" and value is not None and interface.mac is None:
        interface.mac = value.lower()
    if description is not None and description != raw and interface.description is None:
        interface.description = comment_text(description)
    return name


def _link(
    *,
    local: tuple[str, str],
    remote: tuple[str, str],
    source: str,
    draft: Draft,
) -> None:
    """Record the adjacency between ``local`` and ``remote`` as a cable."""
    draft.add_cable(
        DraftCable(
            endpoints=(local, remote),
            comments=[f"observed by LLDP on {local[0]!r}: {local[1]} sees {remote[0]}:{remote[1]}"],
            sources=[source],
        )
    )


def _note_rename(comments: list[str], what: str, original: str) -> None:
    if not any(comment.startswith(f"the {what} is named ") for comment in comments):
        comments.append(
            f"the {what} is named {comment_text(original)!r} on the device; renamed here "
            "because a netviz name may only hold letters, digits and separators"
        )


# --------------------------------------------------------------------------- #
# Shape tolerance
# --------------------------------------------------------------------------- #


def _lldp_root(payload: Any) -> dict[str, Any]:
    """The ``lldp`` container, however deeply the caller's tool wrapped it."""
    if isinstance(payload, dict):
        inner = payload.get("lldp")
        if inner is not None:
            return _mapping(inner)
        return payload
    if isinstance(payload, list):
        return _mapping(payload)
    return {}


def _entries(
    value: Any, *, name_key: str = "name", markers: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Normalise lldpd's container encodings into a list of named mappings.

    ``{"eth0": {...}}``, ``[{"eth0": {...}}]`` and ``[{"name": "eth0", ...}]``
    all become ``[{"name": "eth0", ...}]``.

    The one ambiguity in the encoding is a mapping with neither shape marked:
    is ``{"id": {...}}`` a record holding an ``id``, or an index keyed by the
    name ``id``? ``markers`` resolves it — a key no system or port could
    plausibly be *called*, but that every record of this kind carries. A chassis
    with an ``id`` and no ``name`` is exactly the case lldpd emits for a
    neighbour that advertises no system name, so getting this wrong would name
    that neighbour ``id``.
    """
    if isinstance(value, list):
        return [
            record
            for item in value
            for record in _entries(item, name_key=name_key, markers=markers)
        ]
    if not isinstance(value, dict):
        return []
    if name_key in value or markers & value.keys():
        return [value]
    records: list[dict[str, Any]] = []
    for key, item in value.items():
        for entry in _leaves(item):
            records.append({name_key: key, **entry})
    return records


def _leaves(value: Any) -> list[dict[str, Any]]:
    """The mapping, or mappings, a name-keyed index points at."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _mapping(value: Any) -> dict[str, Any]:
    """The mapping ``value`` is, or the first one it wraps."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _text(value: Any) -> str | None:
    """The scalar ``value`` holds, through lldpd's ``{"value": ...}`` wrapping."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            text = _text(item)
            if text is not None:
                return text
        return None
    if isinstance(value, dict):
        return _text(value.get("value"))
    return None


def _texts(value: Any) -> list[str]:
    """Every scalar ``value`` holds — ``mgmt-ip`` may be one address or several."""
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    text = _text(value)
    return [text] if text is not None else []


def _flag(value: Any) -> bool:
    """A boolean, however lldpd spelled it."""
    if isinstance(value, bool):
        return value
    text = _text(value)
    return text is not None and text.lower() in {"true", "yes", "1"}
