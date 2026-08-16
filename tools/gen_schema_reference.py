#!/usr/bin/env python3
"""Generate ``docs/schema-reference.md`` from the pydantic models.

The reference is the field-by-field companion to ``docs/schema.md``: where the
specification explains *why* the schema looks the way it does, the reference
answers "what exactly may I write here?" for one field at a time.

Everything mechanical — the field list, its order, the types, which fields are
required, the defaults — is read straight off ``model_fields``, so the document
cannot drift from the code. The two things pydantic cannot know, the prose
description and the corresponding YANG path, live in
:data:`netgraph.models.fielddocs.FIELD_DOCS`, which :mod:`netgraph.schema` reads
too so that the reference and the JSON Schema say the same thing. That table is
checked against the models on every run: a field with no entry, or an entry
naming a field that no longer exists, aborts the generator rather than producing
a quietly incomplete document.

Usage::

    python tools/gen_schema_reference.py            # rewrite docs/schema-reference.md
    python tools/gen_schema_reference.py --check    # exit 1 if it is out of date

``tests/test_docs.py`` runs the ``--check`` path, so CI fails when a model
changes without the reference being regenerated.
"""

from __future__ import annotations

import argparse
import enum
import ipaddress
import sys
import types
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Union, get_args, get_origin

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from annotated_types import Ge, Gt, Le, Lt, MaxLen, MinLen  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from pydantic.fields import FieldInfo  # noqa: E402

from netgraph.models import (  # noqa: E402
    AcceptableFrames,
    AdapterSpec,
    AnnotationGeometry,
    AreaSelector,
    AreaSpec,
    Assertion,
    BgpConfig,
    BgpNeighbor,
    BridgeConfig,
    BridgeType,
    Bss,
    CableSpec,
    DeviceSpec,
    Duplex,
    EdgeGeometry,
    ElementBase,
    Forwarding,
    GroupGeometry,
    GroupSpec,
    Interface,
    InterfaceRef,
    InterfaceType,
    IPv4Address,
    IPv4Config,
    IPv6Address,
    IPv6Config,
    LabelGeometry,
    LayoutSpec,
    LegendEntry,
    LegendSpec,
    Location,
    Medium,
    Metadata,
    NetnsDefinition,
    NodeGeometry,
    NoteAnchor,
    NoteSpec,
    OspfConfig,
    PatchPanelSpec,
    PduSpec,
    PoeConfig,
    PoeStandard,
    Point,
    PowerConfig,
    PowerDraw,
    PowerInput,
    PowerSource,
    RoutingConfig,
    Size,
    StaticRoute,
    Style,
    TestSuiteSpec,
    TunnelAuth,
    TunnelMode,
    TunnelSpec,
    TunnelTransport,
    TunnelType,
    UpstreamPort,
    UpstreamType,
    UserSpec,
    UserStatus,
    UserType,
    ViewGeometry,
    VlanConfig,
    VlanDefinition,
    VlanMode,
    VlanSet,
    VrfDefinition,
    WirelessConfig,
)
from netgraph.models.document import ELEMENT_MODELS  # noqa: E402
from netgraph.models.element import (  # noqa: E402
    AREA_KIND,
    LAYOUT_KIND,
    LEGEND_KIND,
    NOTE_KIND,
    TEMPLATE_KIND,
    TEST_SUITE_KIND,
)
from netgraph.models.fielddocs import (  # noqa: E402
    DOCUMENTED_MODELS,
    FIELD_DOCS,
    KIND_NOTES,
    NONE,
    check_coverage,
)

OUTPUT: Final = REPO_ROOT / "docs" / "schema-reference.md"


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Section:
    """One model rendered as a table, with a heading and a lead paragraph."""

    model: type[BaseModel]
    title: str
    lead: str
    #: Anything that a field table cannot express: shorthands, cross-field rules.
    notes: tuple[str, ...] = ()
    #: Rows for keys the loader consumes before the models see them, and which
    #: therefore have no ``model_fields`` entry to be read off. Each is a
    #: rendered ``| ... |`` table row, appended after the generated ones.
    extra_rows: tuple[str, ...] = ()


SECTIONS: Final[tuple[Section, ...]] = (
    Section(
        ElementBase,
        "Document envelope",
        "Every document, whatever its kind, carries these three keys plus a `spec` whose shape "
        "is chosen by `kind`.",
        notes=(
            "`spec` is required and its model is listed in the kind table above.",
            "Unknown keys are rejected anywhere in the document (`NG-D005`). A misspelt key that "
            "was silently ignored would produce a diagram disagreeing with the file, which is "
            "the one failure mode this tool exists to prevent.",
        ),
    ),
    Section(
        Metadata,
        "`metadata`",
        "Identity and free-form annotation, shared by every kind.",
    ),
    Section(
        Location,
        "`metadata.location`",
        "Where the hardware physically is. Optional, and shared by every kind: a patch panel "
        "is racked exactly as a server is.",
        notes=(
            "`position` is the **lowest** rack unit the element occupies and `height` how many "
            "it takes, counting upwards; units are numbered from 1 at the bottom of the "
            "cabinet, which is how a rack is labelled.",
            "`site`, `room` and `rack` together identify a rack (`NG-U001`). Two elements that "
            "name the same three share a cabinet and may not overlap; naming `position` or "
            "`rack_height` without `rack` is `NG-U004`.",
            "`netgraph render --layer rack` draws one front elevation per rack, empty units "
            "included.",
        ),
    ),
    Section(
        DeviceSpec,
        "`spec` — switch, router, hub, computer, server",
        "The five device kinds share one spec shape. They differ in which fields they permit "
        "(a `hub` rejects `bridge`, `vlans`, `forwarding` and all layer-3 configuration) and in "
        "the default value of `forwarding`.",
        extra_rows=(
            "| `from` | element reference | no | *unset* | Names a `kind: template` document "
            "whose partial spec is merged underneath this one. Consumed by the loader: it is "
            "gone before validation, the graph or any renderer sees the device. `interfaces` is "
            "required only when `from` is absent. | — |",
        ),
        notes=(
            "`from` merges a template underneath the device: the device's own keys win, "
            "`interfaces` merge by `name`, and every other list the device declares replaces "
            "the template's outright. See §6.6 of [`schema.md`](schema.md).",
        ),
    ),
    Section(
        Forwarding,
        "`spec.forwarding`",
        "The device-wide default for per-interface IP forwarding. Both fields are required once "
        "the block is written at all.",
    ),
    Section(
        BridgeConfig,
        "`spec.bridge`",
        "The 802.1Q bridge component the device implements.",
    ),
    Section(
        VlanDefinition,
        "`spec.vlans[]`",
        "The device VLAN database. A port may reference a VLAN this list does not declare; that "
        "is `NG-V004`, not an error.",
    ),
    Section(
        Interface,
        "`spec.interfaces[]`",
        "One entry per port or logical interface. Used by both devices and adapters.",
        extra_rows=(
            "| `range` | string | no | *unset* | Declares many interfaces at once instead of "
            "`name`, by bracket expansion over one or more numeric spans "
            "(`GigabitEthernet1/0/[1-48]`). Consumed by the loader: the entry is replaced by "
            "the interfaces it expands to before anything else sees the document. Exactly one "
            "of `name` and `range` is written. | — |",
        ),
        notes=(
            "`range` expands as an odometer, the rightmost span varying fastest, and the width "
            "of a span's low bound is its zero padding (`[01-12]` yields `01`…`12`). In "
            "`description`, `{}` and `%d` stand for the last span and `{0}`, `{1}`, … for a "
            "span by position. See §6.2.5 of [`schema.md`](schema.md).",
            "Inside a `spec` that declares `from`, an entry may state only `name` and the "
            "fields it overrides; the template supplies `type` and the rest.",
            "`type: vlan` requires `parent` and a `vlan` block in access mode carrying the "
            "encapsulation VID.",
            "`type: lag` and `type: bridge` require `members`, which must be non-empty, free of "
            "duplicates, and must not name the interface itself.",
            "An interface carrying IPv6 addresses must have an MTU of at least 1280 (`NG-I011`).",
        ),
    ),
    Section(
        IPv4Config,
        "`spec.interfaces[].ipv4`",
        "RFC 8344's `ip:ipv4` container.",
        notes=(
            "A bare list is shorthand for the container: `ipv4: [10.0.0.1/24]` means "
            "`ipv4: {addresses: [{ip: 10.0.0.1, prefix_length: 24}]}`.",
        ),
    ),
    Section(
        IPv4Address,
        "`spec.interfaces[].ipv4.addresses[]`",
        "One IPv4 address. Addresses are unique within the interface (`NG-A002`).",
        notes=(
            "`10.0.0.1/24` is shorthand for the mapping form.",
            "`netmask: 255.255.255.0` may be written instead of `prefix_length`, but not as well "
            "as; it is normalised away on load and never appears in `netgraph show` output.",
        ),
    ),
    Section(
        IPv6Config,
        "`spec.interfaces[].ipv6`",
        "RFC 8344's `ip:ipv6` container.",
    ),
    Section(
        IPv6Address,
        "`spec.interfaces[].ipv6.addresses[]`",
        "One IPv6 address. The `2001:db8::1/64` shorthand applies here too.",
    ),
    Section(
        VlanConfig,
        "`spec.interfaces[].vlan`",
        "The 802.1Q bridge-port configuration of one interface.",
        notes=(
            "In `access` mode: `access_vlan` is allowed and defaults to 1; `trunk_vlans` and "
            "`native_vlan` are rejected (`NG-V002`, `NG-V003`).",
            "In `trunk` mode: `trunk_vlans` is required and `access_vlan` is rejected.",
            '`trunk_vlans` accepts an id, a list, `"10,20,100-110"`, `all` (1–4094) or `none`, '
            "and always serialises back to the coalesced string form.",
        ),
    ),
    Section(
        WirelessConfig,
        "`spec.interfaces[].wireless`",
        "The radio configuration of a `type: wifi` interface: which side of the association it "
        "is on, where on the air it is, and which networks it serves.",
        notes=(
            "`channel` and `width_mhz` both require `band`: channel numbers repeat between the "
            "2.4 GHz and 6 GHz plans, and 320 MHz exists only at 6 GHz (`NG-W003`, `NG-W004`).",
            "A `medium: wireless` cable joins exactly one `ap` radio to one `station` or `mesh` "
            "radio (`NG-W007`); that association is what the layer-2 view labels with "
            "`SSID @ channel/band`.",
        ),
    ),
    Section(
        Bss,
        "`spec.interfaces[].wireless.bss[]`",
        "One basic service set: an SSID the radio beacons, or — on a client radio — the one it "
        "is associated to.",
        notes=(
            "An `ap` radio lists one entry per SSID it serves; a `station` or `mesh` radio "
            "lists at most one (`NG-W006`).",
            "`vlan` is where the SSID's traffic goes on the wired side. It has to be a VLAN the "
            "access point carries somewhere (`NG-W009`), or clients associate and reach nothing.",
        ),
    ),
    Section(
        NetnsDefinition,
        "`spec.netns[]`",
        "One network namespace the machine runs (§23.1) — a whole second network stack, "
        "with its own interfaces, addresses and routing table. An interface joins it with "
        "`netns`.",
        notes=(
            "`parent` names another entry of the same table, which is how namespaces nest: a "
            "namespace is created from inside exactly one other, so the nesting is a tree "
            "(`NG-N021`). Unset means the machine's initial namespace, which no document "
            "declares.",
            "Not a VRF. A VRF partitions the routing table of one stack; a namespace *is* a "
            "second stack, so it partitions the interface names, the addresses and the "
            "sockets as well. An interface can be in both.",
            "Namespaces are joined by veth pairs, which are ordinary `type: ethernet` "
            "interfaces naming each other with `peer` (§23.2).",
            "A namespace no interface is in holds nothing, which is `NG-N026`.",
        ),
    ),
    Section(
        VrfDefinition,
        "`spec.vrfs[]`",
        "One routing instance — a VRF (§16.1). An interface joins it with `vrf`, and that "
        "binding is what makes an address private to the instance.",
        notes=(
            "Two devices that use the same `name` are taken to mean the same VRF; the route "
            "distinguisher is recorded because MPLS needs it, not to identify the instance.",
            "A VRF no interface binds to holds nothing, which is `NG-F014`.",
        ),
    ),
    Section(
        StaticRoute,
        "`spec.routes[]`",
        "One configured static route (§16.2).",
        notes=(
            "At least one of `via`, `dev` and `blackhole` is required, and `blackhole` excludes "
            "the other two (`NG-F004`).",
            "`via` is of the same family as `prefix` (`NG-F003`) and must be on-link: inside a "
            "prefix the device configures, in the same VRF (`NG-F008`).",
        ),
    ),
    Section(
        RoutingConfig,
        "`spec.routing`",
        "The dynamic routing protocols the device takes part in (§16.3). Both blocks are "
        "optional and neither implies the other.",
    ),
    Section(
        OspfConfig,
        "`spec.routing.ospf`",
        "One OSPF area, and the interfaces that run it.",
        notes=(
            "`area` accepts `0` and `0.0.0.0` for the backbone and stores the dotted quad, so "
            "two documents that spell one area differently still compare equal.",
            "One area per device: per-interface areas, and therefore area border routers, are "
            "deferred (§16.5).",
        ),
    ),
    Section(
        BgpConfig,
        "`spec.routing.bgp`",
        "The autonomous system this device is in, and the sessions it configures.",
    ),
    Section(
        BgpNeighbor,
        "`spec.routing.bgp.neighbors[]`",
        "One BGP session. The peer is an **address**, which is what the device is configured "
        "with — never an element name (§16.4).",
        notes=(
            "The address is resolved against every address the inventory configures. A peer "
            "that resolves to nothing is a warning (`NG-F013`), because an eBGP peer may be a "
            "transit provider nobody declares here; a peer whose own `asn` contradicts "
            "`remote_asn` is an error (`NG-F011`).",
        ),
    ),
    Section(
        CableSpec,
        "`spec` — cable",
        "A cable is an undirected physical link between exactly two interfaces, and a "
        "first-class element so that it can carry its own metadata.",
    ),
    Section(
        InterfaceRef,
        "`spec.endpoints[]`",
        "A reference to one port. Written as the string `device:interface`; the mapping form "
        "below is equivalent and both serialise back to the string.",
    ),
    Section(
        AdapterSpec,
        "`spec` — adapter",
        "An adapter presents network interfaces over a non-network host port: USB dongles, "
        "Thunderbolt docks, media converters.",
    ),
    Section(
        UpstreamPort,
        "`spec.upstream`",
        "The host-facing port of an adapter.",
        notes=(
            "Declaring `attached_to` **and** cabling the upstream port is an error (`NG-X005`): "
            "the host attachment is declared exactly once.",
        ),
    ),
    Section(
        TunnelSpec,
        "`spec` — tunnel",
        "A tunnel is an undirected logical link between two or more interfaces of "
        "`type: tunnel`. It is to a logical topology what a cable is to a physical one, and a "
        "first-class element for the same reason.",
        notes=(
            "`endpoints` uses the same `device:interface` form a cable does, and each one must "
            "name an interface of `type: tunnel` (`NG-T003`) — the virtual interface the tunnel "
            "presents, not the physical port its outer packets leave by.",
            "`over` nests one tunnel inside another: `vxlan` over `ipsec` is written by naming "
            "the IPsec tunnel there. The chain must not loop (`NG-T005`).",
            "`type` supplies the defaults for `port`, `encrypted` and `mode`, and the "
            "encapsulation overhead `NG-T011` measures an MTU against. Materialised on load, so "
            "a loaded document states them explicitly.",
            "There is nowhere to put a key, a password or a certificate, and the fields people "
            "reach for are rejected by name (`NG-T010`). `auth` records the *method*.",
        ),
    ),
    Section(
        PatchPanelSpec,
        "`spec` — patchpanel",
        "A patch panel is a passive cross-connect: numbered positions on the front, the same "
        "numbers on the rear, and a coupler joining each front position to one rear position.",
        notes=(
            "`ports` is the only required key. Each position it names becomes two interfaces, "
            "`front/<n>` and `rear/<n>`, which a cable terminates on exactly as it terminates "
            "on a device port (`NG-P001`).",
            "A panel is not a hop. `netgraph render --layer physical` draws it and both cable "
            "segments; every other layer splices the run into the single edge it electrically "
            "is, between the two active ports.",
            "`couplers` is only needed for a panel that is cross-wired. The default is the "
            "identity mapping, which is what the numbering printed on a real panel promises.",
        ),
    ),
    Section(
        PduSpec,
        "`spec` — pdu",
        "A power distribution unit: numbered outlets, a rated capacity, and the supply that "
        "feeds it. The power half of what a patch panel is for data.",
        notes=(
            "`outlets` is the only required key, and takes the same count-or-range shorthand "
            "`ports` does. An outlet is **not** an interface: a power cord is not a `cable`, so "
            "nothing is cabled to a PDU — a device names an outlet in `power.inputs` instead.",
            "A PDU is placed on a rack elevation through `metadata.location`, exactly as a "
            "switch is, and `netgraph render --layer rack` annotates it with its utilisation.",
            "`input_feed` is free text and is compared only for equality. It is what makes A/B "
            "redundancy checkable: two units on one feed fail together (`NG-E015`).",
        ),
    ),
    Section(
        PowerConfig,
        "`spec.power`",
        "What a device draws, which outlets feed it, and how much PoE it hands out. One block "
        "for both directions, because they are one question about one box.",
        notes=(
            "`draw_watts` accepts a bare number as shorthand for `{typical: n}`. The typical "
            "figure is what a load schedule sums; `maximum` is what a breaker has to survive.",
            "`redundant: true` needs at least two `inputs` (`NG-E002`), and they have to land on "
            "different units *and* different `input_feed`s for the claim to hold (`NG-E015`).",
            "`powered_by: poe` excludes `inputs` (`NG-E005`): a device fed over its uplink has "
            "no cord. `NG-E014` then checks the far end of that uplink actually sources power.",
        ),
    ),
    Section(
        PowerDraw,
        "`spec.power.draw_watts`",
        "The nameplate load of one device, in watts. Written as a bare number when only the "
        "typical figure is known.",
    ),
    Section(
        PowerInput,
        "`spec.power.inputs[]`",
        "One power supply and the outlet feeding it. Accepts the compact form `pdu-r1-a:7` and "
        "the equivalent mapping, the same grammar a cable endpoint uses.",
    ),
    Section(
        PoeConfig,
        "`interfaces[].poe`",
        "This port is power sourcing equipment: it hands power down the cable. Only on a type a "
        "cable terminates on — `ethernet` or `lag` (`NG-E006`).",
        notes=(
            "How much the port reserves is said once: a `class`, or a `budget_watts`, never both "
            "(`NG-E004`). With neither, the port reserves its standard's maximum, which is what "
            "a switch with no per-port configuration does.",
            "A `poe` block on a port with nothing on it is a *capability* and takes no budget. A "
            "port that feeds something, or one with an explicit `budget_watts`, does — see "
            "`NG-E013`.",
        ),
    ),
    # Identity (§19). After the hardware and before the geometry: it is a fact
    # about the network rather than about the drawing, but it is the only kind
    # here that describes no equipment.
    Section(
        UserSpec,
        "`spec` of a `user` document",
        "One identity: a person, a service account or a shared login. Owns no interfaces and "
        "terminates no cable — a person is not a host — so it appears only in the `identity` "
        "view.",
        notes=(
            "`login` is optional because `metadata.name` is usually the account name already. "
            "Absent means the two are the same; everything downstream reads the materialised "
            "value, so nothing has to re-apply the default.",
            "A `departed` account is *kept*, not deleted: the group memberships still to be "
            "revoked are what `NG-S015` reports, and deleting the document would delete them "
            "too.",
            "Only public keys. A pasted private key is refused with an explanation "
            "(`NG-S002`), which is the point of checking the shape at all.",
        ),
    ),
    Section(
        GroupSpec,
        "`spec` of a `group` document",
        "A named set of identities. `members` may name a `user` or another `group`, which is "
        "what makes a hierarchy expressible.",
        notes=(
            "Membership is written on the group and nowhere else. A `user` does not list its "
            "groups: two spellings of one fact are how an inventory starts disagreeing with "
            "itself.",
            "A member is an ordinary element reference (§4.1), resolved outwards from the "
            "group's own namespace. It must resolve (`NG-S010`) and must be an identity "
            "(`NG-S011`).",
            "A group naming itself is refused by the model; a longer loop needs the whole tree "
            "and is `NG-S012`.",
        ),
    ),
    # Diagram geometry (§18). Listed after the elements because it describes the
    # *drawing* rather than the network, and because a reader looking for a field
    # of a device should not have to walk past a table of coordinates to reach it.
    Section(
        LayoutSpec,
        "`spec` of a `layout` document",
        "Where things are drawn, per view. A sidecar: it carries no network facts, and the "
        "elements it places know nothing about it. `netgraph layout` writes it.",
        notes=(
            "Coordinates are **points** (1/72 inch), `y` upwards, origin at the bottom left, "
            "and a `position` is the **centre** of what it places — Graphviz's system, so a "
            "stored arrangement can be handed straight back to it.",
            "A key is an address, resolved like any other reference. A node the inventory does "
            "not declare is keyed by its graph id instead: `subnet:10.0.0.0/24`, "
            "`tunnel:site/wg0`, `rack:hq/comms/r1`.",
            "A key naming something the inventory no longer has is `NG-Y001`, a warning; "
            "`netgraph layout --prune` drops it.",
        ),
    ),
    Section(
        ViewGeometry,
        "`spec.views.<view>`",
        "One view's arrangement. The view name is a layer netgraph draws — `physical`, `l1`, "
        "`l2`, `l3`, `overlay`, `routing`, `rack`, `power` — because the same device sits "
        "somewhere different in each.",
    ),
    Section(
        NodeGeometry,
        "`spec.views.<view>.nodes.<address>`",
        "Where one node is drawn.",
        notes=(
            "`size` is optional and is not seeded by `netgraph layout --write`: Graphviz "
            "derives the same box from the same label on every run. It is honoured on read, "
            "for an editor that lets somebody resize a box on purpose.",
        ),
    ),
    Section(
        EdgeGeometry,
        "`spec.views.<view>.edges.<address>`",
        "How one link is drawn: the bends it goes through, the style it is routed in and "
        "where its label sits. Bends are not seeded unless `netgraph layout --write "
        "--waypoints` is asked for — a computed spline is noise, a hand-placed bend is a "
        "decision — but a `routing` or a `label` is always written, neither being derivable.",
        notes=(
            "The waypoints are **interior** points: the two ends of a route are the nodes "
            "themselves, so dragging either endpoint carries the bends along instead of "
            "invalidating them.",
        ),
    ),
    Section(
        LabelGeometry,
        "`spec.views.<view>.edges.<address>.label`",
        "Where a link's annotation sits, as a position on the link rather than a coordinate "
        "— so nudging a VLAN label clear of a crossing cable survives both endpoints moving.",
    ),
    Section(
        GroupGeometry,
        "`spec.views.<view>.groups.<namespace>`",
        "The box one namespace cluster is drawn as. Unlike a node it carries a required "
        "`size`: nothing else decides how big a cluster is.",
    ),
    Section(
        Point,
        "A point",
        "Two numbers, in points. `{x: 240, y: 396}` or the shorthand `[240, 396]`; both mean "
        "the same thing and both are read.",
    ),
    Section(
        Size,
        "A size",
        "Two positive numbers, in points. `{width: 220, height: 90}` or `[220, 90]`.",
    ),
    # Assertions (§20). Last, for the same reason geometry is late: it describes
    # what somebody relies on rather than what is there, and a reader looking up
    # a field of a device should not have to walk past it.
    Section(
        TestSuiteSpec,
        "`spec` of a `testsuite` document",
        "Named claims about the network the other documents describe. `netgraph test` grades "
        "them and exits non-zero when one does not hold.",
        notes=(
            "A suite must assert something (`NG-K002`). A suite that checked nothing would "
            "report a green run, which is worse than having no suite at all.",
            "Assertions are graded in the order they are written, and a failure names the "
            "file and line of the assertion so an editor can jump to it.",
        ),
    ),
    Section(
        Assertion,
        "One assertion",
        "`assert` chooses the claim; every other key is read in its light. A key that belongs "
        "to a different assertion is rejected by name (`NG-K003`) rather than ignored.",
        notes=(
            "`reachable`, `not-reachable` and `path-shorter-than` take `from` and `to` in the "
            "spellings `netgraph path` accepts: an element, `element:interface`, an IP address, "
            "or a selector matching several of them.",
            "`same-vlan`, `distinct-vlan`, `within-prefix`, `has-interface`, "
            "`port-count-at-least`, `unique` and `count` take `select`, in `netgraph render`'s "
            "filter vocabulary.",
            "`no-single-point-of-failure` takes neither, and optionally narrows the candidates "
            "with `select` and the views with `layer`.",
        ),
    ),
    # Annotations (§21). After the assertions, for the same reason again: they
    # say how the network is *drawn*, not what it is, and nothing below reads
    # them except a renderer.
    Section(
        NoteSpec,
        "`spec` of a `note` document",
        "One callout on the diagram. Presentational throughout: a note cannot make `netgraph "
        "validate` fail, cannot move a hop in `netgraph path`, and never reaches an exported "
        "configuration.",
        notes=(
            "A note needs somewhere to be: either an `anchor`, or a `geometry` giving `x` and "
            "`y`. Both is the shape dragging an anchored note produces — the point places it, "
            "the anchor is what the leader line points at.",
            "`views` scopes the note to the drawings it makes sense in; empty means all of "
            "them. A note naming an element the inventory no longer has is `NG-G001`, a "
            "warning, exactly as a stale layout key is.",
        ),
    ),
    Section(
        NoteAnchor,
        "`spec.anchor`",
        "What a note is about: one element, or one link. Exactly one of the two, and what makes "
        "the note survive the diagram being laid out again.",
    ),
    Section(
        AnnotationGeometry,
        "`spec.geometry` of an annotation",
        "Where an annotation is drawn and how big it is. Flat rather than §18's nested "
        "`position`/`size`, because these four numbers are what a drag and a resize produce.",
        notes=(
            "Coordinates are §18's: points, `y` upwards, origin at the bottom left, and `x`/`y` "
            "is the **centre** of the box. Both or neither — half a position places nothing.",
        ),
    ),
    Section(
        AreaSpec,
        "`spec` of an `area` document",
        "A labelled box drawn behind the nodes. It says what it contains with `members`, a "
        "`selector` or an explicit `geometry`, and at least one of the three is required.",
        notes=(
            "`members` and `selector` box wherever the elements were drawn, so the zone follows "
            "them; `geometry` boxes a region of the canvas instead, for a zone that is about "
            "the paper rather than about devices.",
            "An area matching nothing is `NG-G004`, a warning: an empty box on a diagram reads "
            "as a claim that the zone is empty.",
        ),
    ),
    Section(
        AreaSelector,
        "`spec.selector`",
        "Which elements an area contains, said as a query rather than a list — the form that "
        "does not go stale when a rack gains a switch.",
        notes=(
            "Every clause given must match. A selector with no clause at all is refused: it "
            "would silently box the whole inventory.",
        ),
    ),
    Section(
        LegendSpec,
        "`spec` of a `legend` document",
        "A key: what the colours and the line styles of this drawing mean. Positioned by corner "
        "rather than by coordinate, because a key belongs at the edge of the paper and should "
        "stay there when the diagram is laid out again.",
        notes=(
            "`auto: layers` builds the entries from what the view actually drew — the node "
            "kinds present, the media of the links present — which is the only form of key that "
            "cannot disagree with the picture. It excludes `entries`, and one of the two is "
            "required.",
        ),
    ),
    Section(
        LegendEntry,
        "`spec.entries[]`",
        "One row of the key: a swatch, and what it means.",
    ),
    Section(
        Style,
        "`spec.style`",
        "How one element is drawn. Optional on every drawable kind and on cables and tunnels; "
        "every field is optional in turn, and an absent one inherits from the theme, then the "
        "icon set, then the built-in palette.",
        notes=(
            "The vocabulary is closed. A colour is a hex literal or one of the named colours, "
            "and every other field is a small enum or a bounded number, because these values "
            "end up inside Graphviz attributes and mxGraph style strings and a free-form "
            "pass-through would be an injection (`NG-Z001`).",
            "`shape` is ignored on a cable and a tunnel, which have no shape to set. `icon` "
            "names a picture inside the `--icons` theme and is ignored when no theme is in "
            "use; `icon: none` draws this one element as a plain shape.",
        ),
    ),
)

#: Enumerations rendered as value tables, with the note that explains them.
ENUMS: Final[tuple[tuple[type[enum.Enum], str, str], ...]] = (
    (
        InterfaceType,
        "`interfaces[].type`",
        "Only `ethernet`, `wifi` and `lag` can terminate a cable (`NG-C009`).",
    ),
    (VlanMode, "`vlan.mode`", "A netgraph abstraction; 802.1Q has no equivalent leaf."),
    (
        AcceptableFrames,
        "`vlan.acceptable_frames`",
        "The values are the 802.1Q identity names, unabbreviated.",
    ),
    (BridgeType, "`bridge.type`", "Decides the `dot1q:port-type` of every port on the device."),
    (Medium, "`cable.medium`", "`wireless` constrains the endpoint interface types."),
    (Duplex, "`cable.duplex`", ""),
    (
        UpstreamType,
        "`upstream.type`",
        "Only `usb` and `usb-c` have an IANA interface-type identity.",
    ),
    (
        TunnelType,
        "`tunnel.type`",
        "Each type fixes the layer carried, the outer transport and port, whether the payload "
        "is encrypted, and the encapsulation overhead. See §14.1 of the schema for the table.",
    ),
    (
        TunnelTransport,
        "`tunnel` outer transport",
        "Derived from `type`; `gre` and `esp` run directly over IP and carry no port.",
    ),
    (TunnelMode, "`tunnel.mode`", "IPsec only; every other type has a single mode."),
    (
        PoeStandard,
        "`poe.standard`",
        "Which IEEE 802.3 amendment the port implements, and therefore which classes exist: "
        "`802.3af` stops at class 3, `802.3at` adds 4, `802.3bt` adds 5 to 8.",
    ),
    (
        PowerSource,
        "`power.powered_by`",
        "`outlet` is the default. `poe` says the device takes power over its uplink and has no "
        "cord, so it declares no `inputs`.",
    ),
    (
        TunnelAuth,
        "`tunnel.auth`",
        "The authentication *method*. netgraph never stores key material (`NG-T010`).",
    ),
    (
        UserType,
        "`user.type`",
        "Decides which identity rules apply: only a `person` can depart (`NG-S015`), and only "
        "a `person` is expected to be in a group (`NG-S016`).",
    ),
    (
        UserStatus,
        "`user.status`",
        "`departed` is kept rather than deleted, so the memberships still to be revoked stay "
        "visible.",
    ),
)

#: The IANA identity each interface type exports as, taken from the model.
_IANA: Final = {member: member.iana_if_type for member in InterfaceType}


# --------------------------------------------------------------------------- #
# Type rendering
# --------------------------------------------------------------------------- #


def _unwrap(annotation: Any) -> tuple[Any, list[Any], bool]:
    """Strip ``Optional`` and ``Annotated``.

    Returns the bare annotation, every constraint object found on the way, and
    whether ``None`` was one of the union members.
    """
    constraints: list[Any] = []
    optional = False

    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            args = get_args(annotation)
            annotation = args[0]
            constraints.extend(args[1:])
            continue
        if origin in (Union, types.UnionType):
            members = [arg for arg in get_args(annotation) if arg is not type(None)]
            optional = optional or len(members) != len(get_args(annotation))
            if len(members) == 1:
                annotation = members[0]
                continue
        return annotation, constraints, optional


#: Named grammars, recognised by their pattern so the name cannot drift.
_NAMED_PATTERNS: Final[dict[str, str]] = {
    r"^[A-Za-z0-9._/-]+$": "interface name",
    r"^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$": "element name",
    r"^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*(?:/[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*)*$": (
        "element reference"
    ),
}

#: Named formats, recognised by the validator that normalises them.
_NAMED_VALIDATORS: Final[dict[str, str]] = {
    "normalise_mac": "MAC address",
    "parse_bitrate": "bit rate",
    "normalise_rd": "route distinguisher",
    "normalise_area": "OSPF area",
}


def _flatten(constraints: Sequence[Any]) -> list[Any]:
    """Constraints, with those nested inside a ``FieldInfo`` pulled up."""
    flat: list[Any] = []
    for constraint in constraints:
        if isinstance(constraint, FieldInfo):
            flat.extend(_flatten(constraint.metadata))
        else:
            flat.append(constraint)
    return flat


def _named_format(constraints: Sequence[Any]) -> str | None:
    """The name of the format these constraints describe, if it has one."""
    for constraint in constraints:
        function = getattr(constraint, "func", None)
        name = _NAMED_VALIDATORS.get(getattr(function, "__name__", ""))
        if name is not None:
            return name
        pattern = getattr(constraint, "pattern", None)
        if pattern in _NAMED_PATTERNS:
            return _NAMED_PATTERNS[pattern]
    return None


def _limits(constraints: Sequence[Any], *, unit: str = "character") -> str:
    """Render the numeric and length bounds carried by a field, if any."""
    low: tuple[str, Any] | None = None
    high: tuple[str, Any] | None = None
    min_len: int | None = None
    max_len: int | None = None

    for constraint in _flatten(constraints):
        if isinstance(constraint, Ge):
            low = ("≥", constraint.ge)
        elif isinstance(constraint, Gt):
            low = (">", constraint.gt)
        elif isinstance(constraint, Le):
            high = ("≤", constraint.le)
        elif isinstance(constraint, Lt):
            high = ("<", constraint.lt)
        elif isinstance(constraint, MinLen):
            min_len = constraint.min_length
        elif isinstance(constraint, MaxLen):
            max_len = constraint.max_length

    parts: list[str] = []
    if low is not None and high is not None and low[0] == "≥" and high[0] == "≤":
        parts.append(f"{low[1]}–{high[1]}")
    else:
        parts.extend(f"{bound[0]} {bound[1]}" for bound in (low, high) if bound is not None)
    if min_len is not None and max_len is not None:
        parts.append(f"{min_len}–{max_len} {_plural(max_len, unit)}")
    elif min_len:
        parts.append(f"≥ {min_len} {_plural(min_len, unit)}")
    elif max_len:
        parts.append(f"≤ {max_len} {_plural(max_len, unit)}")
    return ", ".join(parts)


def _plural(count: int, noun: str) -> str:
    if count == 1:
        return noun
    return f"{noun[:-1]}ies" if noun.endswith("y") else f"{noun}s"


def _anchor(title: str) -> str:
    """The GitHub anchor a heading of this title gets."""
    slug = "".join(
        character
        for character in title.lower().replace(" ", "-")
        if character.isalnum() or character in "-_"
    )
    return f"#{slug}"


_MODEL_ANCHORS: dict[type[BaseModel], str] = {}


def _scalar_name(annotation: Any) -> str:
    """A YAML-flavoured name for a leaf type."""
    return {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        ipaddress.IPv4Address: "IPv4 address",
        ipaddress.IPv6Address: "IPv6 address",
        ipaddress.IPv4Network: "IPv4 prefix",
        ipaddress.IPv6Network: "IPv6 prefix",
    }.get(annotation, getattr(annotation, "__name__", str(annotation)))


def render_type(field: FieldInfo) -> str:
    """A one-cell description of what may be written in a field."""
    annotation, constraints, _ = _unwrap(field.annotation)
    constraints = [*constraints, *field.metadata]
    origin = get_origin(annotation)

    if origin is Literal:
        return " \\| ".join(f"`{value}`" for value in get_args(annotation))

    if origin in (list, tuple):
        (item, *_rest) = get_args(annotation)
        item_type, item_constraints, _ = _unwrap(item)
        inner = _render_leaf(item_type, item_constraints)
        bounds = _limits(constraints, unit="entry")
        return f"{inner} list" + (f", {bounds}" if bounds else "")

    if origin is dict:
        key, value = get_args(annotation)
        return f"map {_scalar_name(key)} → {_scalar_name(value)}"

    return _render_leaf(annotation, constraints)


def _render_leaf(annotation: Any, constraints: Sequence[Any]) -> str:
    if get_origin(annotation) in (Union, types.UnionType):
        # A field that takes either address family, such as a route's ``prefix``.
        # Rendered member by member, with the pipe escaped: an unescaped one
        # would end the table cell it is written in.
        members = [member for member in get_args(annotation) if member is not type(None)]
        return " \\| ".join(_render_leaf(member, ()) for member in members)
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return " \\| ".join(f"`{member.value}`" for member in annotation)
    if annotation is VlanSet:
        return "VLAN set"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        anchor = _MODEL_ANCHORS.get(annotation)
        return f"[{annotation.__name__}]({anchor})" if anchor else annotation.__name__
    named = _named_format(_flatten(constraints))
    if named is not None:
        return named
    bounds = _limits(constraints)
    return _scalar_name(annotation) + (f", {bounds}" if bounds else "")


def render_default(field: FieldInfo) -> str:
    """The value used when the key is omitted."""
    if field.is_required():
        return "—"
    if field.default_factory is not None:
        produced = field.default_factory()  # type: ignore[call-arg]
        return f"`{'[]' if isinstance(produced, list) else '{}'}`"
    default = field.default
    if default is None:
        return "*unset*"
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    if isinstance(default, enum.Enum):
        return f"`{default.value}`"
    return f"`{default}`"


# --------------------------------------------------------------------------- #
# Document assembly
# --------------------------------------------------------------------------- #


def _check_coverage() -> None:
    """Fail loudly when :data:`FIELD_DOCS`, the models and :data:`SECTIONS` disagree.

    The table itself is checked by
    :func:`netgraph.models.fielddocs.check_coverage`; what is left to check here
    is that this document renders every model that table documents. A model
    added to ``DOCUMENTED_MODELS`` but never given a :class:`Section` would
    otherwise vanish from the reference while still appearing in the schema.
    """
    try:
        check_coverage()
    except RuntimeError as exc:
        raise SystemExit(f"tools/gen_schema_reference.py: {exc}") from exc

    rendered = {section.model for section in SECTIONS}
    documented = set(DOCUMENTED_MODELS)
    problems = []
    if missing := sorted(model.__name__ for model in documented - rendered):
        problems.append("no SECTIONS entry for: " + ", ".join(missing))
    if extra := sorted(model.__name__ for model in rendered - documented):
        problems.append("SECTIONS renders undocumented models: " + ", ".join(extra))
    if problems:
        raise SystemExit("tools/gen_schema_reference.py: " + "; ".join(problems))


def _field_rows(section: Section) -> Iterator[str]:
    for name, field in section.model.model_fields.items():
        doc = FIELD_DOCS[(section.model.__name__, name)]
        key = field.alias or name
        required = "**yes**" if field.is_required() else "no"
        yang = f"`{doc.yang}`" if doc.yang != NONE else NONE
        yield (
            f"| `{key}` | {render_type(field)} | {required} | {render_default(field)} "
            f"| {doc.description} | {yang} |"
        )


def _kind_table() -> Iterator[str]:
    models = ELEMENT_MODELS
    yield "| `kind` | `spec` model | Notes |"
    yield "|---|---|---|"
    for model in models:
        kind = model.model_fields["kind"].default
        spec = model.model_fields["spec"].annotation
        assert spec is not None
        anchor = _MODEL_ANCHORS.get(spec)
        link = f"[{spec.__name__}]({anchor})" if anchor else spec.__name__
        yield f"| `{kind}` | {link} | {KIND_NOTES[kind]} |"
    yield (
        f"| `{TEMPLATE_KIND}` | partial "
        f"[DeviceSpec]({_MODEL_ANCHORS[DeviceSpec]}) | {KIND_NOTES[TEMPLATE_KIND]} |"
    )
    yield (
        f"| `{LAYOUT_KIND}` | [LayoutSpec]({_MODEL_ANCHORS[LayoutSpec]}) | "
        f"{KIND_NOTES[LAYOUT_KIND]} |"
    )
    yield (
        f"| `{TEST_SUITE_KIND}` | [TestSuiteSpec]({_MODEL_ANCHORS[TestSuiteSpec]}) | "
        f"{KIND_NOTES[TEST_SUITE_KIND]} |"
    )
    for kind, spec_model in (
        (NOTE_KIND, NoteSpec),
        (AREA_KIND, AreaSpec),
        (LEGEND_KIND, LegendSpec),
    ):
        yield (
            f"| `{kind}` | [{spec_model.__name__}]({_MODEL_ANCHORS[spec_model]}) | "
            f"{KIND_NOTES[kind]} |"
        )


def _enum_tables() -> Iterator[str]:
    for enum_type, where, note in ENUMS:
        yield f"### {where}"
        yield ""
        if note:
            yield note
            yield ""
        if enum_type is InterfaceType:
            yield "| Value | `if:type` identity | Cableable |"
            yield "|---|---|---|"
            for interface_type in InterfaceType:
                cableable = "yes" if interface_type.is_cableable else "no"
                yield f"| `{interface_type.value}` | `{_IANA[interface_type]}` | {cableable} |"
        else:
            yield "| Value |"
            yield "|---|"
            for member in enum_type:
                yield f"| `{member.value}` |"
        yield ""


def build() -> str:
    """Render the whole reference."""
    _check_coverage()
    _MODEL_ANCHORS.clear()
    for section in SECTIONS:
        _MODEL_ANCHORS[section.model] = _anchor(section.title)

    lines: list[str] = [
        "<!--",
        "  Generated by tools/gen_schema_reference.py from the pydantic models.",
        "  Do not edit by hand: run `python tools/gen_schema_reference.py` instead.",
        "-->",
        "",
        "# Schema reference",
        "",
        "Every field netgraph accepts, read directly off the models in `src/netgraph/models/`.",
        "This is the lookup table; [`docs/schema.md`](schema.md) is the specification that",
        "explains the design, and [`docs/yang-mapping.md`](yang-mapping.md) explains how the",
        "YANG column relates to RFC 8343, RFC 8344 and IEEE 802.1Q.",
        "",
        "**Reading the tables.** *Required* means the key must be present in the document.",
        "*Default* is the value netgraph uses when it is absent — `—` for a required field,",
        "*unset* when the field simply has no value and nothing downstream supplies one. Several",
        "defaults are filled in at load time from elsewhere in the document (an interface's MTU",
        "becomes the address families' MTU, the device's `forwarding` becomes each family's",
        "`forwarding`); those are called out in the description. The YANG column names the node",
        "the field maps to, with `…` standing for",
        "`/if:interfaces/if:interface` and `—` meaning the field has no standards counterpart.",
        "",
        "## Element kinds",
        "",
    ]
    lines.extend(_kind_table())
    lines.append("")

    for section in SECTIONS:
        lines.append(f"## {section.title}")
        lines.append("")
        lines.append(section.lead)
        lines.append("")
        lines.append("| Field | Type | Required | Default | Description | YANG |")
        lines.append("|---|---|---|---|---|---|")
        lines.extend(_field_rows(section))
        lines.extend(section.extra_rows)
        lines.append("")
        for note in section.notes:
            lines.append(f"* {note}")
        if section.notes:
            lines.append("")

    lines.append("## Enumerations")
    lines.append("")
    lines.extend(_enum_tables())

    lines.extend(
        [
            "## Scalar formats",
            "",
            "Values that are normalised on load: what you write and what",
            "`netgraph show` prints back may differ.",
            "",
            "| Type | Accepted | Stored as |",
            "|---|---|---|",
            "| Element name | `^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$`, 1–253 characters | unchanged |",
            "| Interface name | `^[A-Za-z0-9._/-]+$`, 1–64 characters | unchanged |",
            "| MAC address | `00:1e:8c:00:10:01`, `00-1E-8C-00-10-01`, `001e.8c00.1001` "
            "| lower-case colon form |",
            "| Bit rate | an integer of bit/s, or `<number><unit>` with unit `bps`, `kbps`, "
            "`Mbps`, `Gbps`, `Tbps` | integer bit/s |",
            "| IPv4 address entry | `10.0.0.1/24`, or a mapping with `prefix_length` or `netmask` "
            "| `{ip, prefix_length}` |",
            "| IPv6 address entry | `2001:db8::1/64`, or a mapping with `prefix_length` "
            "| RFC 5952 compressed `{ip, prefix_length}` |",
            '| VLAN set | `10`, `[10, 20]`, `"10,20,100-110"`, `all`, `none` '
            '| sorted, coalesced `"10,20,100-110"` |',
            "| Boolean | `true` / `false` only | unchanged |",
            "",
            'Booleans are strict on purpose: a quoted `"true"` or a YAML 1.1 `yes` is an error,',
            "not a silently accepted truth value. A MAC address written unquoted can be parsed by",
            "YAML as a sexagesimal integer, which loses the original digits — netgraph detects the",
            "case and tells you to quote it.",
            "",
        ]
    )

    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 when the committed file is out of date.",
    )
    parser.add_argument("-o", "--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    content = build()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != content:
            print(
                f"{args.output} is out of date; run 'python tools/gen_schema_reference.py'",
                file=sys.stderr,
            )
            return 1
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
