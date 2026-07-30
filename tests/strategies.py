"""Hypothesis strategies that generate structurally valid netgraph inventories.

Every other test in this suite asserts behaviour on an inventory somebody wrote
by hand, which samples the input space at whatever points the author happened to
think of. The invariants that matter most are not samples but *universally
quantified statements* — "for every valid inventory, formatting is idempotent",
"for every name, no rendering can be broken out of" — and those need input
nobody chose.

That is what this module produces. The unit of generation is an
:class:`InventoryPlan`: a list of documents, each with the namespace it belongs
in, which can be written to disk in several *layouts* (one file per document,
one file per namespace, everything in one stream) so that a property can also
assert what must not depend on the layout.

The hard requirement is that generation stays inside the schema. A strategy that
produced documents the loader rejects would turn every property into a test of
the rejection path, so the cross-field constraints are encoded here rather than
filtered for afterwards:

* an interface's ``members`` name interfaces of the same element, never itself;
* a ``vlan`` sub-interface carries a ``parent`` and an access-mode ``vlan``
  block; ``lag`` and ``bridge`` carry ``members`` and nothing else does;
* a hub takes ethernet ports with no VLAN and no IP configuration (§6.5);
* an adapter's downstream ports never collide with its upstream port;
* a cable joins two interfaces that exist and are cableable;
* a tunnel's endpoints are ``tunnel`` interfaces, each listed once, and
  ``spec.over`` points at a tunnel earlier in the list, so a stack can nest but
  cannot loop;
* ``vni`` is present for VXLAN/Geneve and absent everywhere else, ``mode`` only
  for IPsec, ``port`` only for a transport that has one.

Free text — descriptions, labels, vendor strings, cable labels — is the opposite:
it is drawn from a deliberately hostile alphabet (:data:`HOSTILE_FRAGMENTS`),
because those fields are the ones that reach a DOT string, a Mermaid label, a
JSON value and an HTML attribute, and the escaping of those is the
security-relevant half of the renderers.

Two characters are excluded from that alphabet on purpose. ``\\r`` and the
other Unicode line breaks are normalised by *YAML itself* (a YAML processor is
required to fold them to ``\\n``), and lone surrogates cannot be encoded as
UTF-8 at all; a document containing either does not survive a round trip through
any conforming YAML implementation, so generating one would assert a property of
the format rather than of netgraph.
"""

from __future__ import annotations

import itertools
import re
import string
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import yaml
from hypothesis import strategies as st

from netgraph.models import API_VERSION

__all__ = [
    "BREAKOUT_PAYLOADS",
    "CABLEABLE",
    "DEVICE_KINDS",
    "HOSTILE_FRAGMENTS",
    "MAX_NAME_LENGTH",
    "TUNNEL_TYPES",
    "InventoryPlan",
    "PlannedDocument",
    "TemplateCase",
    "adapter_documents",
    "cable_documents",
    "commented_yaml",
    "device_documents",
    "dump_documents",
    "element_names",
    "free_text",
    "if_names",
    "interfaces",
    "inventory_plans",
    "labels",
    "mac_addresses",
    "namespaces",
    "optional_text",
    "patchpanel_documents",
    "range_cases",
    "template_cases",
    "tunnel_documents",
    "unique_names",
    "vlan_ids",
    "vlan_sets",
]

# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #

#: Fragments that have historically broken one output format or another. A
#: description is free text, so every one of these is a legal value that a
#: renderer has to carry through without letting it become syntax.
HOSTILE_FRAGMENTS: Final[tuple[str, ...]] = (
    '"',
    "'",
    "\\",
    "\\n",
    "\\u0041",
    "<",
    ">",
    "&",
    "&amp;",
    "&quot;",
    "</script>",
    "<!--",
    "-->",
    "]]>",
    "<svg onload=alert(1)>",
    "javascript:alert(1)",
    "${expr}",
    "`cmd`",
    "|",
    "[",
    "]",
    "{",
    "}",
    "#",
    ";",
    ":",
    "-",
    "--",
    "/*",
    "*/",
    "%",
    "\n",
    "\t",
    " ",
    "  ",
    "é",
    "ü",
    "日本語",
    "→",
    "​",
    "\U0001f4a1",
    "ok",
    "port 3",
    "1",
    "0",
    "null",
    "yes",
)

#: The payloads an escaping property plants deliberately: each one is a fragment
#: that *is* syntax in at least one output format, so if it ever reaches the
#: output unescaped a parser will see more structure than the graph has.
BREAKOUT_PAYLOADS: Final[tuple[str, ...]] = (
    '" [shape=box label="pwned',
    '"]; n999 [label="pwned"]; {rank=same; "',
    '"}]|{ evil',
    "</script><script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    '\\" }, {"id": "pwned"',
    "]-->|x|Z[pwned]",
    "*/ alert(1) /*",
    "--> <!-- ",
    "&lt;script&gt;",
    # U+2028: whitespace to a JSON parser, a *line break* to some JavaScript
    # ones, so a JSON string carrying it can still end a statement early.
    "\u2028pwned",
)


def free_text(*, min_size: int = 0, max_size: int = 4) -> st.SearchStrategy[str]:
    """Free-form text as an inventory really carries it: mostly awkward."""
    return st.lists(st.sampled_from(HOSTILE_FRAGMENTS), min_size=min_size, max_size=max_size).map(
        "".join
    )


def optional_text(*, max_size: int = 3) -> st.SearchStrategy[str | None]:
    """``free_text`` or nothing, with nothing being the common case."""
    return st.one_of(st.none(), free_text(min_size=1, max_size=max_size))


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #

_SEGMENT_ALPHABET: Final = string.ascii_letters + string.digits
_SEGMENT_SEPARATORS: Final = "-_."

#: The longest ``metadata.name`` the schema admits (§4.1).
MAX_NAME_LENGTH: Final = 253


@st.composite
def element_names(draw: st.DrawFn, *, max_parts: int = 3) -> str:
    """A ``metadata.name``: one segment of ``[A-Za-z0-9]+([-_.][A-Za-z0-9]+)*``.

    Includes the awkward-but-legal forms the grammar admits — a single
    character, a name that is all digits, dotted and underscored segments — and
    the two length boundaries, which is where an off-by-one in a length check
    would hide.
    """
    shape = draw(st.integers(min_value=0, max_value=9))
    if shape == 0:
        return draw(st.sampled_from(("a", "1", "Z", "0")))
    if shape == 1:
        # Exactly at the length ceiling: 253 characters, still one segment.
        body = draw(st.text(alphabet=_SEGMENT_ALPHABET, min_size=1, max_size=1))
        return (body * MAX_NAME_LENGTH)[:MAX_NAME_LENGTH]
    if shape == 2:
        # One character short of the ceiling.
        body = draw(st.text(alphabet=_SEGMENT_ALPHABET, min_size=1, max_size=1))
        return (body * MAX_NAME_LENGTH)[: MAX_NAME_LENGTH - 1]

    parts = draw(
        st.lists(
            st.text(alphabet=_SEGMENT_ALPHABET, min_size=1, max_size=6),
            min_size=1,
            max_size=max_parts,
        )
    )
    separators = draw(
        st.lists(
            st.sampled_from(_SEGMENT_SEPARATORS),
            min_size=len(parts) - 1,
            max_size=len(parts) - 1,
        )
    )
    name = parts[0]
    for separator, part in zip(separators, parts[1:], strict=True):
        name += separator + part
    return name[:MAX_NAME_LENGTH]


_IFNAME_ALPHABET: Final = string.ascii_letters + string.digits + "._/-"


def if_names() -> st.SearchStrategy[str]:
    """An interface name: ``[A-Za-z0-9._/-]+``, at most 64 characters.

    Deliberately admits the shapes that look like something else to a consumer
    downstream: ``front/7`` is how a patch-panel port is spelled, ``a.b`` is how
    a sub-interface usually is, and ``--`` is a Graphviz edge operator.
    """
    return st.one_of(
        st.sampled_from(("eth0", "a", "0", "ge-0/0/1", "front/7", "a.b", "--", "x/")),
        st.text(alphabet=_IFNAME_ALPHABET, min_size=1, max_size=12),
    )


def unique_names(count: int) -> st.SearchStrategy[list[str]]:
    """``count`` distinct element names."""
    return st.lists(element_names(), min_size=count, max_size=count, unique=True)


def namespaces(*, max_depth: int = 2) -> st.SearchStrategy[str]:
    """A namespace: the ``/``-joined directory path a document is found under."""
    return st.lists(
        st.sampled_from(("sites", "berlin", "rack1", "core", "a", "b")),
        min_size=0,
        max_size=max_depth,
    ).map("/".join)


# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #


def mac_addresses() -> st.SearchStrategy[str]:
    """An EUI-48 address in one of the three spellings §5 accepts."""
    octets = st.lists(st.integers(min_value=0, max_value=255), min_size=6, max_size=6)

    def spell(values: list[int]) -> str:
        digits = [f"{value:02x}" for value in values]
        return ":".join(digits)

    return octets.map(spell)


def vlan_ids() -> st.SearchStrategy[int]:
    return st.integers(min_value=1, max_value=4094)


def vlan_sets() -> st.SearchStrategy[Any]:
    """Any of the spellings a ``vid-range-type`` accepts."""
    return st.one_of(
        vlan_ids(),
        st.lists(vlan_ids(), min_size=1, max_size=3),
        st.sampled_from(("all", "none", "10,20", "100-110", "1-4094", " 10 , 20-30 ")),
    )


def labels() -> st.SearchStrategy[dict[str, str]]:
    """``metadata.labels`` — keys are a restricted grammar, values are free text."""
    keys = st.sampled_from(("role", "tier", "owner", "env", "example.com/team"))
    return st.dictionaries(keys, free_text(max_size=2), max_size=3)


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #

#: Interface types that may terminate a cable (``NG-C009``).
CABLEABLE: Final[frozenset[str]] = frozenset({"ethernet", "wifi", "lag"})


@st.composite
def _ipv4_block(draw: st.DrawFn, *, index: int) -> dict[str, Any]:
    """An ``ipv4`` container with one address from a per-interface /24."""
    host = draw(st.integers(min_value=1, max_value=250))
    shorthand = draw(st.booleans())
    address: Any = (
        f"10.{index // 256 % 256}.{index % 256}.{host}/24"
        if shorthand
        else {"ip": f"10.{index // 256 % 256}.{index % 256}.{host}", "prefix_length": 24}
    )
    block: dict[str, Any] = {"addresses": [address]}
    if draw(st.booleans()):
        block["gateway"] = f"10.{index // 256 % 256}.{index % 256}.254"
    if draw(st.booleans()):
        block["forwarding"] = draw(st.booleans())
    return block


@st.composite
def _ipv6_block(draw: st.DrawFn, *, index: int) -> dict[str, Any]:
    host = draw(st.integers(min_value=1, max_value=250))
    return {"addresses": [f"2001:db8:0:{index:x}::{host:x}/64"]}


@st.composite
def interfaces(
    draw: st.DrawFn,
    *,
    count: int,
    layer3: bool = True,
    vlan_aware: bool = True,
    allowed_types: Sequence[str] = ("ethernet", "wifi", "loopback", "bridge", "vlan", "lag"),
    reserved: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """A consistent ``spec.interfaces`` list.

    The list is built in dependency order — the physical ports first, then the
    stacked types that reference them — because ``parent`` and ``members`` have
    to name an interface of the same element (``NG-I002``, ``NG-I003``) and a
    strategy that drew them independently would spend its time being filtered.
    """
    taken = set(reserved)
    names: list[str] = []
    for _ in range(count):
        name = draw(if_names().filter(lambda value: value not in taken))
        taken.add(name)
        names.append(name)

    physical_types = [t for t in ("ethernet", "wifi") if t in allowed_types]
    entries: list[dict[str, Any]] = []

    for position, name in enumerate(names):
        # The first interface is always a physical one, so that everything
        # stacked on top of it has something to name.
        if position == 0 or not entries:
            itype = draw(st.sampled_from(physical_types)) if physical_types else allowed_types[0]
        else:
            itype = draw(st.sampled_from(list(allowed_types)))

        entry: dict[str, Any] = {"name": name, "type": itype}

        if itype == "vlan":
            parents = [e["name"] for e in entries if e["name"] != name]
            if not parents:
                itype = physical_types[0] if physical_types else allowed_types[0]
                entry["type"] = itype
            else:
                entry["parent"] = draw(st.sampled_from(parents))
                entry["vlan"] = {"mode": "access", "access_vlan": draw(vlan_ids())}
        if itype in ("lag", "bridge"):
            candidates = [e["name"] for e in entries if e["name"] != name]
            if not candidates:
                itype = physical_types[0] if physical_types else allowed_types[0]
                entry["type"] = itype
            else:
                entry["members"] = draw(
                    st.lists(
                        st.sampled_from(candidates),
                        min_size=1,
                        max_size=len(candidates),
                        unique=True,
                    )
                )

        if draw(st.booleans()):
            entry["description"] = draw(free_text(min_size=1, max_size=3))
        if draw(st.booleans()):
            entry["enabled"] = draw(st.booleans())
        if draw(st.booleans()):
            entry["mac"] = draw(mac_addresses())

        wants_ipv6 = layer3 and draw(st.booleans())
        if draw(st.booleans()):
            # An interface carrying IPv6 needs at least the v6 minimum MTU
            # (``NG-I011``), so the bound depends on what is configured.
            entry["mtu"] = draw(st.integers(min_value=1280 if wants_ipv6 else 68, max_value=9216))
        if layer3 and draw(st.booleans()):
            entry["ipv4"] = draw(_ipv4_block(index=position))
        if wants_ipv6:
            entry["ipv6"] = draw(_ipv6_block(index=position))

        if vlan_aware and "vlan" not in entry and itype in CABLEABLE and draw(st.booleans()):
            entry["vlan"] = draw(
                st.one_of(
                    st.builds(
                        lambda vid: {"mode": "access", "access_vlan": vid},
                        vlan_ids(),
                    ),
                    st.builds(
                        lambda vlans: {"mode": "trunk", "trunk_vlans": vlans},
                        vlan_sets(),
                    ),
                )
            )

        entries.append(entry)

    return entries


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlannedDocument:
    """One YAML document, and the namespace it has to be written into."""

    #: ``""`` for the inventory root, otherwise a ``/``-joined directory path.
    namespace: str
    #: File stem *hint*. Derived from the element name and truncated, so it is
    #: nearly always unique across the plan but not guaranteed to be:
    #: :meth:`InventoryPlan.per_document` is what makes the file names unique, and
    #: it has to, for two reasons. Two names differing only past the truncation
    #: point produce one stem; two names differing only in case produce one *file*
    #: on macOS and Windows. Either way a document would be silently dropped, and
    #: a property comparing layouts would fail with no hint as to why.
    stem: str
    data: dict[str, Any]

    @property
    def kind(self) -> str:
        kind = self.data.get("kind")
        return kind if isinstance(kind, str) else ""

    @property
    def name(self) -> str:
        metadata = self.data.get("metadata")
        if isinstance(metadata, Mapping):
            name = metadata.get("name")
            if isinstance(name, str):
                return name
        return ""

    @property
    def fqn(self) -> str:
        return f"{self.namespace}/{self.name}" if self.namespace else self.name


def dump_documents(documents: Sequence[Mapping[str, Any]]) -> str:
    """Render a YAML stream, one ``---`` document per mapping."""
    return yaml.safe_dump_all(
        [dict(document) for document in documents],
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


@dataclass(frozen=True, slots=True)
class InventoryPlan:
    """A generated inventory, independent of how it is spread over files."""

    documents: tuple[PlannedDocument, ...]

    def __len__(self) -> int:
        return len(self.documents)

    def of_kind(self, kind: str) -> tuple[PlannedDocument, ...]:
        return tuple(document for document in self.documents if document.kind == kind)

    def names(self) -> tuple[str, ...]:
        return tuple(document.fqn for document in self.documents)

    # -- layouts ---------------------------------------------------------

    def per_document(self) -> dict[str, str]:
        """One file per document, named after it, one file per document.

        The clause is repeated because it is the invariant, and ``stem`` alone does
        not deliver it. Names are unique, stems are truncated, and file systems
        disagree about case — so ``per_document`` compares *folded* paths and
        suffixes a clash rather than letting the second document overwrite the
        first. Without that, a property quantified over layouts would fail on
        macOS and Windows for a reason nothing in the failure output mentions.
        """
        files: dict[str, str] = {}
        taken: set[str] = set()
        for document in self.documents:
            prefix = f"{document.namespace}/" if document.namespace else ""
            path, index = f"{prefix}{document.stem}.yaml", 1
            while path.casefold() in taken:
                index += 1
                path = f"{prefix}{document.stem}-{index}.yaml"
            taken.add(path.casefold())
            files[path] = dump_documents([document.data])
        return files

    def per_namespace(self) -> dict[str, str]:
        """One multi-document file per namespace."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for document in self.documents:
            grouped.setdefault(document.namespace, []).append(document.data)
        return {
            (f"{namespace}/elements.yaml" if namespace else "elements.yaml"): dump_documents(datas)
            for namespace, datas in grouped.items()
        }

    def paired(self) -> dict[str, str]:
        """Documents packed two to a file, which is neither of the above."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for index, document in enumerate(self.documents):
            prefix = f"{document.namespace}/" if document.namespace else ""
            grouped.setdefault(f"{prefix}group{index // 2}.yaml", []).append(document.data)
        return {path: dump_documents(datas) for path, datas in grouped.items()}

    def layouts(self) -> dict[str, dict[str, str]]:
        """Every layout, by name, so a property can quantify over them."""
        return {
            "per-document": self.per_document(),
            "per-namespace": self.per_namespace(),
            "paired": self.paired(),
        }

    def stream(self) -> str:
        """The whole plan as one YAML stream. Root-namespace plans only."""
        return dump_documents([document.data for document in self.documents])

    def write(self, root: Path, files: Mapping[str, str] | None = None) -> Path:
        """Write ``files`` (default: one per document) below ``root``."""
        for relative, text in (files if files is not None else self.per_document()).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    def flattened(self) -> InventoryPlan:
        """The same documents, all in the root namespace.

        References are written as short names and every name in a plan is
        unique, so flattening cannot change what a reference resolves *to* —
        only the fully-qualified names it resolves to it *by*.
        """
        return InventoryPlan(tuple(replace(document, namespace="") for document in self.documents))


def _envelope(
    kind: str, name: str, spec: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": {"name": name, **metadata},
        "spec": spec,
    }


@st.composite
def _metadata_extras(draw: st.DrawFn) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if draw(st.booleans()):
        extras["description"] = draw(free_text(min_size=1, max_size=3))
    if draw(st.booleans()):
        extras["labels"] = draw(labels())
    if draw(st.booleans()):
        rack: dict[str, Any] = {"rack": draw(st.sampled_from(("r1", "r2")))}
        if draw(st.booleans()):
            rack["site"] = draw(st.sampled_from(("hq", "dc1")))
        if draw(st.booleans()):
            rack["position"] = draw(st.integers(min_value=1, max_value=40))
            rack["height"] = draw(st.integers(min_value=1, max_value=2))
        extras["location"] = rack
    return extras


DEVICE_KINDS: Final[tuple[str, ...]] = ("switch", "router", "hub", "computer", "server")


@st.composite
def device_documents(
    draw: st.DrawFn,
    *,
    name: str,
    namespace: str,
    kind: str | None = None,
    with_tunnel: bool = False,
) -> PlannedDocument:
    """One device document, obeying the per-kind constraints of §6.5.

    ``with_tunnel`` appends a ``tunnel`` interface. Drawing one by chance is far
    too rare to exercise the overlay layer — a device has at most four ports and
    seven types to choose between — so the caller asks for one instead.
    """
    kind = kind if kind is not None else draw(st.sampled_from(DEVICE_KINDS))
    layer3 = kind != "hub"
    vlan_aware = kind != "hub"
    allowed: tuple[str, ...] = (
        ("ethernet",)
        if kind == "hub"
        else ("ethernet", "wifi", "loopback", "bridge", "vlan", "lag", "tunnel")
    )

    spec: dict[str, Any] = {
        "interfaces": draw(
            interfaces(
                count=draw(st.integers(min_value=1, max_value=4)),
                layer3=layer3,
                vlan_aware=vlan_aware,
                allowed_types=allowed,
            )
        )
    }
    for key in ("vendor", "model", "serial", "location"):
        value = draw(optional_text())
        if value is not None:
            spec[key] = value
    if vlan_aware and draw(st.booleans()):
        # ``dot1qtypes:name-type`` is 32 characters, so the free text is clipped
        # rather than filtered: a rejected draw here would only shrink the
        # alphabet, and the length bound is not what this strategy is probing.
        spec["vlans"] = [
            {"id": vid, "name": name_text[:32]}
            for vid, name_text in zip(
                draw(st.lists(vlan_ids(), min_size=1, max_size=3, unique=True)),
                draw(st.lists(free_text(max_size=2), min_size=3, max_size=3)),
                strict=False,
            )
        ]
    if vlan_aware and layer3 and draw(st.booleans()):
        spec["bridge"] = {"type": "customer-vlan-bridge"}
    if layer3 and draw(st.booleans()):
        spec["forwarding"] = {"ipv4": draw(st.booleans()), "ipv6": draw(st.booleans())}

    if with_tunnel and "tunnel" in allowed:
        taken = {entry["name"] for entry in spec["interfaces"]}
        port = draw(if_names().filter(lambda value: value not in taken))
        overlay: dict[str, Any] = {"name": port, "type": "tunnel"}
        if draw(st.booleans()):
            overlay["parent"] = spec["interfaces"][0]["name"]
        if draw(st.booleans()):
            overlay["ipv4"] = draw(_ipv4_block(index=200))
        spec["interfaces"].append(overlay)

    return PlannedDocument(
        namespace=namespace,
        stem=f"dev-{name}"[:60],
        data=_envelope(kind, name, spec, draw(_metadata_extras())),
    )


@st.composite
def adapter_documents(
    draw: st.DrawFn, *, name: str, namespace: str, hosts: Sequence[str]
) -> PlannedDocument:
    """An ``adapter`` whose downstream ports never collide with its upstream one."""
    upstream = draw(if_names())
    spec: dict[str, Any] = {
        "upstream": {
            "name": upstream,
            "type": draw(st.sampled_from(("usb", "usb-c", "thunderbolt", "pcie", "m2", "sfp"))),
        },
        "interfaces": draw(
            interfaces(
                count=draw(st.integers(min_value=1, max_value=2)),
                layer3=True,
                vlan_aware=True,
                allowed_types=("ethernet", "wifi", "lag"),
                reserved=(upstream,),
            )
        ),
    }
    attached = draw(st.booleans())
    if hosts and attached:
        spec["upstream"]["attached_to"] = draw(st.sampled_from(list(hosts)))
    if draw(st.booleans()):
        spec["passthrough"] = draw(st.booleans())
    if draw(st.booleans()):
        spec["form_factor"] = draw(free_text(min_size=1, max_size=2))
    return PlannedDocument(
        namespace=namespace,
        stem=f"ada-{name}"[:60],
        data=_envelope("adapter", name, spec, draw(_metadata_extras())),
    )


@st.composite
def patchpanel_documents(draw: st.DrawFn, *, name: str, namespace: str) -> PlannedDocument:
    """A ``patchpanel``, optionally cross-wired."""
    count = draw(st.integers(min_value=1, max_value=6))
    spec: dict[str, Any] = {"ports": draw(st.sampled_from((count, f"1-{count}")))}
    if count > 1 and draw(st.booleans()):
        # A permutation of the positions, so no two fronts share a rear.
        numbers = [str(index) for index in range(1, count + 1)]
        rotated = numbers[1:] + numbers[:1]
        spec["couplers"] = dict(zip(numbers, rotated, strict=True))
    if draw(st.booleans()):
        spec["form_factor"] = draw(free_text(min_size=1, max_size=2))
    return PlannedDocument(
        namespace=namespace,
        stem=f"pp-{name}"[:60],
        data=_envelope("patchpanel", name, spec, draw(_metadata_extras())),
    )


@dataclass(frozen=True, slots=True)
class _Port:
    """One cableable interface of one element, as a cable endpoint names it."""

    element: str
    interface: str

    def __str__(self) -> str:
        return f"{self.element}:{self.interface}"


def _cableable_ports(document: PlannedDocument) -> Iterator[_Port]:
    """Every interface of ``document`` a cable may terminate on (``NG-C009``)."""
    spec = document.data.get("spec", {})
    if document.kind == "patchpanel":
        raw = spec.get("ports")
        count = raw if isinstance(raw, int) else int(str(raw).split("-")[-1])
        for number in range(1, count + 1):
            yield _Port(document.name, f"front/{number}")
            yield _Port(document.name, f"rear/{number}")
        return
    for entry in spec.get("interfaces", []):
        if entry.get("type") in CABLEABLE:
            yield _Port(document.name, entry["name"])


def _tunnel_ports(document: PlannedDocument) -> Iterator[_Port]:
    """Every ``tunnel`` interface of ``document`` (``NG-T003``)."""
    for entry in document.data.get("spec", {}).get("interfaces", []):
        if entry.get("type") == "tunnel":
            yield _Port(document.name, entry["name"])


@st.composite
def cable_documents(
    draw: st.DrawFn, *, name: str, namespace: str, ends: tuple[_Port, _Port]
) -> PlannedDocument:
    medium = draw(st.sampled_from(("copper", "fiber", "wireless")))
    spec: dict[str, Any] = {
        "endpoints": [str(ends[0]), str(ends[1])],
        "medium": medium,
    }
    if draw(st.booleans()):
        spec["speed"] = draw(st.sampled_from((1000, "1Gbps", "100Mbps", "10Gbps")))
    if draw(st.booleans()):
        spec["duplex"] = draw(st.sampled_from(("full", "half")))
    if medium != "wireless" and draw(st.booleans()):
        spec["length_m"] = draw(st.sampled_from((0, 1, 2.5, 30)))
    if draw(st.booleans()):
        spec["label"] = draw(free_text(min_size=1, max_size=3))
    return PlannedDocument(
        namespace=namespace,
        stem=f"cbl-{name}"[:60],
        data=_envelope("cable", name, spec, draw(_metadata_extras())),
    )


#: ``spec.type`` -> (needs vni, allows mode, has a port)
TUNNEL_TYPES: Final[dict[str, tuple[bool, bool, bool]]] = {
    "wireguard": (False, False, True),
    "ipsec": (False, True, False),
    "openvpn": (False, False, True),
    "pptp": (False, False, False),
    "l2tp": (False, True, True),
    "gre": (False, False, False),
    "vxlan": (True, False, True),
    "geneve": (True, False, True),
}


@st.composite
def tunnel_documents(
    draw: st.DrawFn,
    *,
    name: str,
    namespace: str,
    ends: Sequence[_Port],
    over: str | None = None,
) -> PlannedDocument:
    """A ``tunnel``, with the per-type fields §14.1 permits and no others."""
    ttype = draw(st.sampled_from(sorted(TUNNEL_TYPES)))
    needs_vni, allows_mode, has_port = TUNNEL_TYPES[ttype]
    spec: dict[str, Any] = {
        "type": ttype,
        "endpoints": [str(port) for port in ends],
    }
    if needs_vni:
        spec["vni"] = draw(st.integers(min_value=0, max_value=16_777_215))
    if allows_mode and draw(st.booleans()):
        spec["mode"] = draw(st.sampled_from(("tunnel", "transport")))
    if has_port and draw(st.booleans()):
        spec["port"] = draw(st.integers(min_value=1, max_value=65535))
    if over is not None:
        spec["over"] = over
    if draw(st.booleans()):
        spec["mtu"] = draw(st.integers(min_value=1280, max_value=9000))
    if draw(st.booleans()):
        spec["label"] = draw(free_text(min_size=1, max_size=3))
    return PlannedDocument(
        namespace=namespace,
        stem=f"tun-{name}"[:60],
        data=_envelope("tunnel", name, spec, draw(_metadata_extras())),
    )


# --------------------------------------------------------------------------- #
# Whole inventories
# --------------------------------------------------------------------------- #


@st.composite
def inventory_plans(
    draw: st.DrawFn,
    *,
    min_devices: int = 1,
    max_devices: int = 4,
    adapters: bool = True,
    panels: bool = True,
    tunnels: bool = True,
    cables: bool = True,
) -> InventoryPlan:
    """A whole inventory the loader accepts, references resolved by construction.

    Names are unique across the *plan*, not merely within a namespace, so that a
    short reference resolves to exactly one element however the documents are
    later spread over directories — which is what lets
    :meth:`InventoryPlan.flattened` be meaningful.
    """
    device_count = draw(st.integers(min_value=min_devices, max_value=max_devices))
    adapter_count = draw(st.integers(min_value=0, max_value=1)) if adapters else 0
    panel_count = draw(st.integers(min_value=0, max_value=1)) if panels else 0

    total = device_count + adapter_count + panel_count
    #: One pool for every name in the plan — elements, cables and tunnels alike.
    #: Uniqueness has to hold across kinds, not just within one: a cable named
    #: after a switch would be a second element with that short name, and a
    #: reference to it would then be ambiguous rather than resolvable.
    pool = draw(unique_names(total + 6))
    names, link_names, tunnel_names = pool[:total], pool[total : total + 4], pool[total + 4 :]
    namespace = draw(namespaces())
    spread = draw(st.booleans())

    def namespace_for(index: int) -> str:
        if not spread or not namespace:
            return namespace
        return namespace if index % 2 == 0 else namespace.rsplit("/", 1)[0]

    overlaid = tunnels and draw(st.booleans())
    documents: list[PlannedDocument] = []
    for index in range(device_count):
        documents.append(
            draw(
                device_documents(
                    name=names[index],
                    namespace=namespace_for(index),
                    with_tunnel=overlaid,
                )
            )
        )
    hosts = [document.name for document in documents]
    for offset in range(adapter_count):
        index = device_count + offset
        documents.append(
            draw(adapter_documents(name=names[index], namespace=namespace_for(index), hosts=hosts))
        )
    for offset in range(panel_count):
        index = device_count + adapter_count + offset
        documents.append(
            draw(patchpanel_documents(name=names[index], namespace=namespace_for(index)))
        )

    # -- cables ----------------------------------------------------------
    used: set[str] = set()
    if cables:
        by_element = {
            document.name: list(_cableable_ports(document))
            for document in documents
            if document.kind != "cable"
        }
        candidates = [name for name, ports in by_element.items() if ports]
        pairs: list[tuple[_Port, _Port]] = []
        for first, second in itertools.pairwise(candidates):
            left = next((port for port in by_element[first] if str(port) not in used), None)
            right = next((port for port in by_element[second] if str(port) not in used), None)
            if left is None or right is None:
                continue
            used.add(str(left))
            used.add(str(right))
            pairs.append((left, right))
        keep = draw(st.integers(min_value=0, max_value=min(len(pairs), len(link_names))))
        for index, pair in enumerate(pairs[:keep]):
            documents.append(
                draw(
                    cable_documents(
                        name=link_names[index],
                        namespace=namespace_for(index),
                        ends=pair,
                    )
                )
            )

    # -- tunnels ---------------------------------------------------------
    if tunnels:
        endpoints = [port for document in documents for port in _tunnel_ports(document)]
        # One endpoint per element, so no tunnel lists the same port twice.
        by_element_first: dict[str, _Port] = {}
        for port in endpoints:
            by_element_first.setdefault(port.element, port)
        available = list(by_element_first.values())
        if len(available) >= 2:
            stack = draw(st.integers(min_value=1, max_value=len(tunnel_names)))
            previous: str | None = None
            for depth in range(stack):
                tunnel_name = tunnel_names[depth]
                document = draw(
                    tunnel_documents(
                        name=tunnel_name,
                        namespace=namespace_for(depth),
                        ends=available[: draw(st.integers(min_value=2, max_value=len(available)))],
                        over=previous,
                    )
                )
                documents.append(document)
                previous = tunnel_name

    return InventoryPlan(tuple(documents))


# --------------------------------------------------------------------------- #
# Ranges and templates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TemplateCase:
    """A document written the short way, and the same thing written out.

    Both halves are produced by the strategy rather than by netgraph, so the
    comparison is against an independent expansion instead of against the code
    under test.
    """

    #: The documents as written, using ``range:`` or ``spec.from``.
    written: tuple[PlannedDocument, ...]
    #: The one document the written form must expand to.
    expanded: PlannedDocument


@st.composite
def range_cases(draw: st.DrawFn) -> TemplateCase:
    """A device using ``interfaces[].range``, plus its hand-expansion (§6.2.5)."""
    name = draw(element_names())
    prefix = draw(st.sampled_from(("ge-", "eth", "Gi1/0/", "p")))
    spans = draw(st.integers(min_value=1, max_value=2))

    bounds: list[tuple[int, int, int]] = []
    pattern = prefix
    for index in range(spans):
        low = draw(st.integers(min_value=0, max_value=3))
        high = draw(st.integers(min_value=low, max_value=low + 3))
        width = draw(st.sampled_from((1, 2)))
        bounds.append((low, high, width))
        pattern += f"[{low:0{width}d}-{high}]"
        if index < spans - 1:
            pattern += "/"

    def values(low: int, high: int, width: int) -> list[str]:
        return [f"{value:0{width}d}" for value in range(low, high + 1)]

    combinations: list[tuple[str, ...]] = [()]
    for low, high, width in bounds:
        combinations = [
            (*prior, value) for prior in combinations for value in values(low, high, width)
        ]

    # ``{}`` and ``%d`` both stand for the *last* span (§6.2.5), and ``{i}``
    # names one by position -- so an indexed placeholder is only offered when
    # the pattern actually has that span.
    templates = ["port {}", "{} on {}", "%d", "{{literal}} {}", "50% done {}"]
    templates += [f"{{{index}}}-{{}}" for index in range(spans)]
    description = draw(st.one_of(st.none(), st.sampled_from(templates)))
    entry: dict[str, Any] = {"range": pattern, "type": "ethernet"}
    if description is not None:
        entry["description"] = description
    enabled = draw(st.booleans())
    entry["enabled"] = enabled

    expanded_entries: list[dict[str, Any]] = []
    for combination in combinations:
        expanded_name = prefix + "/".join(combination)
        item: dict[str, Any] = {"name": expanded_name, "type": "ethernet"}
        if description is not None:
            item["description"] = _substitute(description, combination)
        item["enabled"] = enabled
        expanded_entries.append(item)

    metadata = draw(_metadata_extras())
    written = PlannedDocument(
        namespace="",
        stem="ranged",
        data=_envelope("switch", name, {"interfaces": [entry]}, metadata),
    )
    expanded = PlannedDocument(
        namespace="",
        stem="ranged",
        data=_envelope("switch", name, {"interfaces": expanded_entries}, metadata),
    )
    return TemplateCase(written=(written,), expanded=expanded)


_INDEXED: Final = re.compile(r"\{(\d+)\}")


def _substitute(text: str, values: Sequence[str]) -> str:
    """§6.2.5's placeholder rules, written out independently of the loader.

    ``{}`` and ``%d`` both stand for the *last* span — the one that varies
    fastest — ``{i}`` names a span by position, and ``{{``/``}}``/``%%`` are the
    literal characters.
    """
    out: list[str] = []
    position = 0
    while position < len(text):
        for token, replacement in (
            ("{{", "{"),
            ("}}", "}"),
            ("%%", "%"),
            ("{}", values[-1]),
            ("%d", values[-1]),
        ):
            if text.startswith(token, position):
                out.append(replacement)
                position += len(token)
                break
        else:
            match = _INDEXED.match(text, position)
            if match is not None:
                out.append(values[int(match.group(1))])
                position = match.end()
            else:
                out.append(text[position])
                position += 1
    return "".join(out)


_TEMPLATE_SCALARS: Final[tuple[str, ...]] = ("vendor", "model", "serial", "location")


@st.composite
def template_cases(draw: st.DrawFn) -> TemplateCase:
    """A device inheriting a template, plus the merged document it must equal.

    Built backwards: the merged result is generated first and then *split* into
    the two halves, so the expectation is a construction rather than a second
    implementation of the merge rules.
    """
    device_name, template_name = draw(unique_names(2))
    entries = draw(interfaces(count=draw(st.integers(min_value=2, max_value=4))))
    split = draw(st.integers(min_value=1, max_value=len(entries) - 1))

    merged_spec: dict[str, Any] = {"interfaces": entries}
    template_spec: dict[str, Any] = {"interfaces": entries[:split]}
    device_spec: dict[str, Any] = {"interfaces": entries[split:], "from": template_name}

    for key in _TEMPLATE_SCALARS:
        where = draw(st.sampled_from(("neither", "template", "device", "both")))
        if where == "neither":
            continue
        template_value = draw(free_text(min_size=1, max_size=2))
        device_value = draw(free_text(min_size=1, max_size=2))
        if where in ("template", "both"):
            template_spec[key] = template_value
        if where in ("device", "both"):
            device_spec[key] = device_value
        # A key the device declares wins; otherwise the template's is inherited.
        merged_spec[key] = device_value if where in ("device", "both") else template_value

    metadata = draw(_metadata_extras())
    template = PlannedDocument(
        namespace="",
        stem="tpl",
        data={
            "apiVersion": API_VERSION,
            "kind": "template",
            "metadata": {"name": template_name},
            "spec": template_spec,
        },
    )
    device = PlannedDocument(
        namespace="",
        stem="dev",
        data=_envelope("switch", device_name, device_spec, metadata),
    )
    expanded = PlannedDocument(
        namespace="",
        stem="dev",
        data=_envelope("switch", device_name, merged_spec, metadata),
    )
    return TemplateCase(written=(template, device), expanded=expanded)


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #

#: Comment bodies the formatter has to carry through untouched.
_COMMENT_TEXTS: Final[tuple[str, ...]] = (
    "# a note",
    "#no space",
    "#",
    "# trailing spaces   ",
    "# unicode: 日本語 →",
    '# "quoted" and \\backslash',
    "# ---",
)


@st.composite
def commented_yaml(draw: st.DrawFn, text: str) -> str:
    """``text`` with whole-line comments interleaved at legal positions.

    Comments go *before* a line at that line's own indentation, which is where a
    reader writes one and the only position that is unambiguously a whole-line
    comment rather than something inside a scalar.
    """
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if draw(st.integers(min_value=0, max_value=3)) == 0:
            indent = " " * (len(line) - len(line.lstrip(" ")))
            out.append(indent + draw(st.sampled_from(_COMMENT_TEXTS)))
        out.append(line)
    if draw(st.booleans()):
        out.append(draw(st.sampled_from(_COMMENT_TEXTS)))
    return "\n".join(out) + "\n"
