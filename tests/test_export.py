"""``netgraph export`` — the artefacts, their escaping, and their manifests.

Four properties are asserted here, in this order of importance:

**Byte stability.** Every format has a committed golden over the published
example inventories (``tests/fixtures/export/``), regenerated with::

    pytest tests/test_export.py --regen-golden

An artefact that is not byte-stable is not worth committing, and committing
these files is the whole point: a diff in an exported zone or pull list should
mean the network changed.

**Correct escaping.** Each target format has its own grammar, and an inventory
can carry a quote, a comma, a newline, a pipe, a unicode name or a 300-character
label in every free-text field the artefacts print — descriptions, vendors,
cable labels, site names, and the directory names that become namespaces. These
mirror the DOT and Mermaid escaping tests in ``tests/test_render.py``: hostile
input in, parseable output out, verified by *parsing the output back* rather
than by eyeballing the escape.

**A complete manifest.** Nothing is dropped without a record. The tests assert
the reason token, not just that something was recorded, because the token is the
machine-readable half a pipeline branches on.

**One selection.** ``--filter``-style narrowing goes through the same
:class:`~netgraph.render.graph.FilterSpec` a render uses, so an export scoped to
a namespace holds exactly the elements a diagram scoped the same way would.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.export import (
    CONFIG_FORMATS,
    EXPORTERS,
    FORMATS,
    ExportContext,
    ExportOptions,
    ExportResult,
    export,
    is_assignable_label,
    is_domain_name,
    is_label_name,
)
from netgraph.export.context import (
    Address,
    NameRegistry,
    element_addresses,
    management_address,
    reverse_zone_of,
)
from netgraph.export.header import GENERATOR
from netgraph.export.manifest import Manifest, Reason, Recorder
from netgraph.export.names import (
    MAX_DNS_LABEL,
    ansible_identifier,
    csv_cell,
    dns_labels,
    domain_name,
    is_host_label,
    markdown_cell,
    sanitise_label,
    transliterate,
)
from netgraph.fsio import write_text
from netgraph.loader import Inventory, load_tree
from netgraph.render import build_graph, filter_graph
from netgraph.render.graph import FilterSpec, Layer

from platform_marks import ON_WINDOWS  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "export"

#: The origin every dns-zone case is exported under. A documentation domain
#: (RFC 2606) so nothing here can be mistaken for a real zone.
ORIGIN = "example.com."


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def run_export(
    inventory: Inventory,
    export_format: str,
    *,
    options: ExportOptions | None = None,
    spec: FilterSpec | None = None,
) -> ExportResult:
    """Export ``inventory``, building exactly the layers the format declares.

    The same sequence :func:`netgraph.cli.export_command` runs, minus the
    validation gate: build, filter, emit. Sharing it here is deliberate — a test
    that built the graph differently from the CLI would be asserting about a
    pipeline nobody runs.
    """
    exporter = EXPORTERS[export_format]
    narrowing = spec or FilterSpec()
    graphs = {
        layer: filter_graph(build_graph(inventory, layer=layer), narrowing)
        for layer in exporter.layers
    }
    return export(
        export_format,
        lambda recorder: ExportContext(
            inventory=inventory,
            graphs=graphs,
            options=options or ExportOptions(origin=ORIGIN),
            recorder=recorder,
        ),
    )


def write_inventory(root: Path, name: str, text: str) -> None:
    """Write one YAML document into ``root``, creating the namespace folders."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def inventories() -> dict[str, Inventory]:
    """Every example tree, loaded once for the whole module."""
    return {
        name: load_tree(EXAMPLES / name)
        for name in ("home-lab", "campus", "patch-room", "overlay", "quickstart")
    }


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def test_every_registered_format_declares_what_it_needs_and_what_it_drops() -> None:
    """The registry is what the CLI, the docs and the goldens are driven from."""
    for name, exporter in EXPORTERS.items():
        assert exporter.name == name
        assert exporter.layers, f"{name} declares no layer to build"
        assert exporter.description and not exporter.description.endswith(".")
        assert exporter.lossy, f"{name} does not say what it drops"
        assert exporter.suffix.startswith(".")


def test_the_format_tuple_matches_the_registry() -> None:
    assert tuple(EXPORTERS) == FORMATS


# --------------------------------------------------------------------------- #
# Goldens
# --------------------------------------------------------------------------- #

#: ``(stem, example, format, options)``. Chosen so every format is covered over
#: at least two inventories, and so each format-specific option that changes the
#: output is exercised by one case.
CASES = (
    ("home-lab-hosts", "home-lab", "hosts", ExportOptions()),
    ("campus-hosts", "campus", "hosts", ExportOptions()),
    ("home-lab-zone", "home-lab", "dns-zone", ExportOptions(origin=ORIGIN)),
    ("campus-zone-forward", "campus", "dns-zone", ExportOptions(origin=ORIGIN, zones="forward")),
    (
        "campus-zone-reverse",
        "campus",
        "dns-zone",
        ExportOptions(
            origin=ORIGIN,
            zones="reverse",
            ttl=600,
            soa_mname="ns1.example.net.",
            soa_rname="net-team.example.net.",
            soa_serial=2026072901,
            nameservers=("ns1.example.net.", "ns2.example.net."),
        ),
    ),
    ("home-lab-ansible", "home-lab", "ansible-inventory", ExportOptions()),
    ("campus-ansible", "campus", "ansible-inventory", ExportOptions()),
    ("home-lab-prometheus", "home-lab", "prometheus-sd", ExportOptions()),
    (
        "campus-prometheus",
        "campus",
        "prometheus-sd",
        ExportOptions(port=9100, labels={"env": "prod"}),
    ),
    ("patch-room-cables", "patch-room", "cable-list", ExportOptions()),
    (
        "patch-room-cables-markdown",
        "patch-room",
        "cable-list",
        ExportOptions(table_format="markdown"),
    ),
    ("campus-cables", "campus", "cable-list", ExportOptions()),
)


def golden_path(stem: str, export_format: str) -> Path:
    suffix = ".md" if stem.endswith("markdown") else EXPORTERS[export_format].suffix
    return GOLDEN_DIR / f"{stem}{suffix}"


#: The version netgraph stamps into a provenance banner, wherever it appears.
_VERSION = re.compile(re.escape(GENERATOR))
_VERSION_PLACEHOLDER = "netgraph <version>"


def normalise(payload: str) -> str:
    """``payload`` with the generator's version replaced by a placeholder.

    The version is genuinely useful *in* an artefact — "which netgraph wrote
    this" is the first question about a stale file — and genuinely bad in a
    golden. It is not derived from the inventory, so a version bump, or a
    checkout that is not installed as a distribution and reports
    ``0.0.0.dev0``, would fail eight snapshots with a diff that says the export
    "drifted". That invites a blind ``--regen-golden`` which would absorb a real
    regression in the same commit.

    So the goldens are stored and compared with the version elided, and
    :func:`test_the_banner_carries_the_running_version` asserts separately that
    the real thing is stamped in.
    """
    return _VERSION.sub(_VERSION_PLACEHOLDER, payload)


@pytest.mark.parametrize(
    ("stem", "example", "export_format", "options"), CASES, ids=[case[0] for case in CASES]
)
def test_the_artefact_matches_its_golden_file(
    stem: str,
    example: str,
    export_format: str,
    options: ExportOptions,
    inventories: dict[str, Inventory],
    regen_golden: bool,
) -> None:
    actual = normalise(run_export(inventories[example], export_format, options=options).payload)
    golden = golden_path(stem, export_format)

    if regen_golden:
        golden.parent.mkdir(parents=True, exist_ok=True)
        # ``netgraph.fsio.write_text`` rather than ``Path.write_text``: a golden
        # is a byte-for-byte artefact, and regenerating one on Windows through
        # Python's text mode would rewrite every line ending in the file. See
        # ``.gitattributes``, which keeps the committed copy at LF for the same
        # reason.
        write_text(golden, actual)
        pytest.skip(f"regenerated {golden.name}")

    assert golden.exists(), (
        f"missing golden {golden.relative_to(REPO_ROOT)}; "
        f"create it with 'pytest tests/test_export.py --regen-golden'"
    )
    assert actual == golden.read_text(encoding="utf-8"), (
        f"the {export_format} export of {example} drifted from its golden file. "
        f"If the change is intended, rerun with --regen-golden and review the diff."
    )


@pytest.mark.parametrize(
    ("stem", "example", "export_format", "options"), CASES, ids=[case[0] for case in CASES]
)
def test_the_artefact_is_reproducible(
    stem: str,
    example: str,
    export_format: str,
    options: ExportOptions,
    inventories: dict[str, Inventory],
) -> None:
    """Two exports of one inventory are byte-identical, manifest included."""
    first = run_export(inventories[example], export_format, options=options)
    second = run_export(inventories[example], export_format, options=options)
    assert first.payload == second.payload
    assert first.manifest.to_json() == second.manifest.to_json()


@pytest.mark.parametrize("export_format", FORMATS)
def test_a_reloaded_inventory_exports_identically(export_format: str) -> None:
    """The loader's directory traversal must not reach the output.

    Loading the same tree twice can hand back elements in a different order on
    a filesystem that does not guarantee one. Every collection in an export is
    sorted by an explicit key precisely so that cannot show, and this is the
    test that would catch a sort that was forgotten.
    """
    options = ExportOptions(origin=ORIGIN)
    first = run_export(load_tree(EXAMPLES / "campus"), export_format, options=options)
    second = run_export(load_tree(EXAMPLES / "campus"), export_format, options=options)
    assert first.payload == second.payload


# --------------------------------------------------------------------------- #
# hosts
# --------------------------------------------------------------------------- #


def hosts_entries(payload: str) -> dict[str, list[str]]:
    """Parse a hosts fragment back into ``{address: [names]}``."""
    entries: dict[str, list[str]] = {}
    for line in payload.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        address, *names = stripped.split()
        entries[address] = names
    return entries


def test_hosts_publishes_every_routable_address_under_both_names(
    inventories: dict[str, Inventory],
) -> None:
    entries = hosts_entries(run_export(inventories["home-lab"], "hosts").payload)
    assert entries["192.168.10.10"] == ["srv-nas.hosts", "srv-nas"]
    # A router with several addresses gets a line for each of them.
    assert entries["192.0.2.1"] == ["rtr-home.routers", "rtr-home"]
    assert entries["2001:db8::1"] == ["rtr-home.routers", "rtr-home"]


def test_hosts_leaves_out_loopback_and_link_local_addresses(
    inventories: dict[str, Inventory],
) -> None:
    """``127.0.0.1 my-laptop`` is wrong on every machine that is not that one."""
    payload = run_export(inventories["home-lab"], "hosts").payload
    published = [ipaddress.ip_address(text) for text in hosts_entries(payload)]
    assert published, "the fragment is empty, so it proves nothing"
    assert not any(address.is_loopback or address.is_link_local for address in published)


def test_hosts_orders_by_family_then_numeric_value(inventories: dict[str, Inventory]) -> None:
    """Not lexicographic: ``10.1.2.10`` must follow ``10.1.2.9``."""
    addresses = list(hosts_entries(run_export(inventories["campus"], "hosts").payload))
    keys = [
        (ipaddress.ip_address(text).version, int(ipaddress.ip_address(text))) for text in addresses
    ]
    assert keys == sorted(keys)


def test_hosts_carries_a_generated_by_header(inventories: dict[str, Inventory]) -> None:
    payload = run_export(inventories["home-lab"], "hosts").payload
    assert payload.startswith("# Generated by 'netgraph export hosts'")
    assert "netgraph " in payload.splitlines()[1]


def test_hosts_records_an_element_with_no_routable_address(
    inventories: dict[str, Inventory],
) -> None:
    manifest = run_export(inventories["home-lab"], "hosts").manifest
    reasons = {skip.reason for skip in manifest.skipped}
    assert Reason.NOT_ROUTABLE in reasons
    assert all(skip.detail for skip in manifest.skipped), "every skip must explain itself"


# --------------------------------------------------------------------------- #
# dns-zone
# --------------------------------------------------------------------------- #

#: ``owner  IN  TYPE  data`` — enough of RFC 1035 to check what we emit.
_RECORD = re.compile(r"^(\S+)\s+IN\s+(A|AAAA|PTR|NS)\s+(\S+)$")


def zone_records(payload: str) -> list[tuple[str, str, str, str]]:
    """``(origin, owner, type, data)`` for every record, comments removed."""
    origin = ""
    records: list[tuple[str, str, str, str]] = []
    for line in payload.splitlines():
        text = line.split(";", 1)[0].rstrip()
        if not text:
            continue
        if text.startswith("$ORIGIN"):
            origin = text.split()[1]
            continue
        if text.startswith("$TTL"):
            continue
        match = _RECORD.match(text.strip())
        if match:
            owner, kind, data = match.group(1), match.group(2), match.group(3)
            records.append((origin, owner, kind, data))
    return records


def test_the_forward_zone_holds_an_address_record_per_address(
    inventories: dict[str, Inventory],
) -> None:
    payload = run_export(
        inventories["home-lab"], "dns-zone", options=ExportOptions(origin=ORIGIN, zones="forward")
    ).payload
    records = {(owner, kind, data) for _, owner, kind, data in zone_records(payload)}
    assert ("srv-nas.hosts", "A", "192.168.10.10") in records
    assert ("srv-nas.hosts", "AAAA", "2001:db8:10::10") in records


def test_every_forward_record_has_a_matching_pointer(inventories: dict[str, Inventory]) -> None:
    """The failure this export exists to prevent: forward and reverse disagreeing."""
    payload = run_export(inventories["campus"], "dns-zone").payload
    forward: set[tuple[str, str]] = set()
    pointers: set[tuple[str, str]] = set()
    for origin, owner, kind, data in zone_records(payload):
        if kind in {"A", "AAAA"}:
            forward.add((f"{owner}.{origin}", data))
        elif kind == "PTR":
            address = ipaddress.ip_address(
                _address_of(f"{owner}.{origin}" if owner != "@" else origin)
            )
            pointers.add((data, str(address)))
    assert forward == pointers


def _address_of(pointer: str) -> str:
    """``11.0.10.10.in-addr.arpa.`` back to ``10.10.0.11``."""
    labels = pointer.rstrip(".").split(".")
    if labels[-2:] == ["in-addr", "arpa"]:
        return ".".join(reversed(labels[:-2]))
    nibbles = "".join(reversed(labels[:-2]))
    packed = ":".join(nibbles[index : index + 4] for index in range(0, len(nibbles), 4))
    return str(ipaddress.IPv6Address(packed))


def test_every_zone_opens_with_an_soa_and_at_least_one_ns(
    inventories: dict[str, Inventory],
) -> None:
    """RFC 1035 §6.1: a zone without them is one no nameserver will load."""
    payload = run_export(inventories["campus"], "dns-zone").payload
    sections = payload.split(";; zone:")[1:]
    assert sections, "the document holds no zone at all"
    for section in sections:
        assert "IN  SOA" in section
        assert "IN  NS" in section


def test_the_soa_serial_is_fixed_rather_than_derived_from_the_clock(
    inventories: dict[str, Inventory],
) -> None:
    """A date-based serial would make every export a diff."""
    options = ExportOptions(origin=ORIGIN, soa_serial=7)
    payload = run_export(inventories["home-lab"], "dns-zone", options=options).payload
    assert "7   ; serial" in payload or re.search(r"\b7\s+;\s+serial", payload)


def test_the_nameservers_asked_for_replace_the_default(
    inventories: dict[str, Inventory],
) -> None:
    options = ExportOptions(origin=ORIGIN, nameservers=("a.example.net.", "b.example.net."))
    payload = run_export(inventories["home-lab"], "dns-zone", options=options).payload
    nameservers = {data for _, _, kind, data in zone_records(payload) if kind == "NS"}
    assert nameservers == {"a.example.net.", "b.example.net."}


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("10.0.0.0/24", "0.0.10.in-addr.arpa."),
        ("10.0.0.0/22", "0.10.in-addr.arpa."),
        ("10.1.2.0/30", "2.1.10.in-addr.arpa."),
        # A host route still needs an owner name *inside* a zone, so the last
        # unit is never spent on the zone name.
        ("192.0.2.1/32", "2.0.192.in-addr.arpa."),
        ("2001:db8::/48", "0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa."),
        (
            "2001:db8::1/128",
            "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa.",
        ),
        ("0.0.0.0/0", "in-addr.arpa."),
        ("::/0", "ip6.arpa."),
    ],
)
def test_the_reverse_zone_sits_on_a_delegation_boundary(prefix: str, expected: str) -> None:
    assert reverse_zone_of(ipaddress.ip_network(prefix)) == expected


def test_zones_forward_and_reverse_together_are_the_whole_of_zones_all(
    inventories: dict[str, Inventory],
) -> None:
    """``--zones all`` is a concatenation, not a third rendering."""
    inventory = inventories["home-lab"]
    everything = run_export(inventory, "dns-zone").payload
    forward = run_export(
        inventory, "dns-zone", options=ExportOptions(origin=ORIGIN, zones="forward")
    ).payload
    reverse = run_export(
        inventory, "dns-zone", options=ExportOptions(origin=ORIGIN, zones="reverse")
    ).payload
    assert zone_records(everything) == zone_records(forward) + zone_records(reverse)


# --------------------------------------------------------------------------- #
# ansible-inventory
# --------------------------------------------------------------------------- #


def test_the_ansible_document_is_the_schema_ansible_reads(
    inventories: dict[str, Inventory],
) -> None:
    document = json.loads(run_export(inventories["campus"], "ansible-inventory").payload)
    assert set(document["_meta"]) == {"hostvars"}
    assert "children" in document["all"]
    for group, entry in document.items():
        if group in {"_meta", "all"}:
            continue
        assert set(entry) <= {"hosts", "children", "vars"}
        for host in entry["hosts"]:
            assert host in document["_meta"]["hostvars"], f"{group} names an unknown host"


def test_every_ansible_group_name_is_a_legal_identifier(
    inventories: dict[str, Inventory],
) -> None:
    document = json.loads(run_export(inventories["campus"], "ansible-inventory").payload)
    for group in document:
        if group == "_meta":
            continue
        assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", group), group


def test_ansible_groups_cover_namespace_kind_vendor_and_role(
    inventories: dict[str, Inventory],
) -> None:
    document = json.loads(run_export(inventories["campus"], "ansible-inventory").payload)
    assert "ns_sites_north_access" in document
    assert "kind_switch" in document
    assert "vendor_cisco" in document
    assert "role_core" in document


def test_a_namespace_group_is_a_child_of_its_parent(inventories: dict[str, Inventory]) -> None:
    """``group_vars/ns_sites_north.yml`` must reach the whole site."""
    document = json.loads(run_export(inventories["campus"], "ansible-inventory").payload)
    assert "ns_sites_north_access" in document["ns_sites_north"]["children"]
    assert "ns_sites_north" in document["ns_sites"]["children"]
    assert "ns_sites" in document["all"]["children"]
    # A nested group hangs off its parent, never off ``all`` as well.
    assert "ns_sites_north" not in document["all"]["children"]


def test_ansible_host_is_the_management_address(inventories: dict[str, Inventory]) -> None:
    """A loopback router ID beats a transit interface; both beat IPv6."""
    document = json.loads(run_export(inventories["home-lab"], "ansible-inventory").payload)
    hostvars = document["_meta"]["hostvars"]
    assert hostvars["rtr-home.routers"]["ansible_host"] == "192.0.2.1"


def test_host_variables_carry_the_interfaces_and_vlans_a_template_needs(
    inventories: dict[str, Inventory],
) -> None:
    document = json.loads(run_export(inventories["campus"], "ansible-inventory").payload)
    switch = document["_meta"]["hostvars"]["sw-north-acc-01.access.north.sites"]
    names = [interface["name"] for interface in switch["netgraph_interfaces"]]
    assert names, "a switch with no interfaces reached the inventory"
    assert switch["netgraph_vlan_ids"] == sorted(switch["netgraph_vlan_ids"])
    assert any("vlans" in interface for interface in switch["netgraph_interfaces"])
    assert [entry["id"] for entry in switch["netgraph_vlans"]] == sorted(
        entry["id"] for entry in switch["netgraph_vlans"]
    )


def test_interfaces_keep_declaration_order(inventories: dict[str, Inventory]) -> None:
    """The set of hosts is sorted; one host's interface list is its own."""
    document = json.loads(run_export(inventories["home-lab"], "ansible-inventory").payload)
    interfaces = document["_meta"]["hostvars"]["pc-desk.hosts"]["netgraph_interfaces"]
    assert [entry["name"] for entry in interfaces] == ["lo", "eno1", "wlp1s0"]


# --------------------------------------------------------------------------- #
# prometheus-sd
# --------------------------------------------------------------------------- #


def test_the_prometheus_document_is_a_list_of_target_groups(
    inventories: dict[str, Inventory],
) -> None:
    groups = json.loads(run_export(inventories["campus"], "prometheus-sd").payload)
    assert isinstance(groups, list) and groups
    for group in groups:
        assert set(group) == {"targets", "labels"}
        assert len(group["targets"]) == 1
        for name, value in group["labels"].items():
            assert is_label_name(name), name
            assert value, "an empty label value is indistinguishable from an absent one"


def test_prometheus_labels_carry_namespace_kind_vendor_and_site(
    inventories: dict[str, Inventory],
) -> None:
    groups = json.loads(run_export(inventories["patch-room"], "prometheus-sd").payload)
    labels = next(
        group["labels"] for group in groups if group["labels"]["instance"].startswith("sw-core-01")
    )
    assert labels["netgraph_namespace"] == "network"
    assert labels["netgraph_kind"] == "switch"
    assert labels["netgraph_site"] == "hq"
    assert labels["netgraph_rack"] == "r1"


def test_a_port_brackets_an_ipv6_target(tmp_path: Path) -> None:
    """``2001:db8::1:9100`` would be unparseable; RFC 3986 brackets it."""
    write_inventory(
        tmp_path,
        "v6.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata: {name: srv-v6}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eno1\n"
        "      type: ethernet\n"
        "      ipv6: {addresses: ['2001:db8::1/64']}\n",
    )
    payload = run_export(
        load_tree(tmp_path), "prometheus-sd", options=ExportOptions(port=9100)
    ).payload
    assert json.loads(payload)[0]["targets"] == ["[2001:db8::1]:9100"]


def test_a_static_label_is_merged_into_every_target(inventories: dict[str, Inventory]) -> None:
    options = ExportOptions(labels={"env": "prod", "team": "net"})
    groups = json.loads(
        run_export(inventories["home-lab"], "prometheus-sd", options=options).payload
    )
    assert all(group["labels"]["env"] == "prod" for group in groups)
    assert all(group["labels"]["team"] == "net" for group in groups)


def test_the_netgraph_version_is_not_a_prometheus_label(
    inventories: dict[str, Inventory],
) -> None:
    """It would end every time series on upgrade; the docs record provenance."""
    groups = json.loads(run_export(inventories["home-lab"], "prometheus-sd").payload)
    for group in groups:
        assert not any("generated" in name or "version" in name for name in group["labels"])


@pytest.mark.parametrize("name", ["job", "_x", "a1"])
def test_a_usable_prometheus_label_name_is_accepted(name: str) -> None:
    assert is_label_name(name)


@pytest.mark.parametrize("name", ["__name__", "1a", "a-b", "", "a b"])
def test_a_reserved_or_malformed_prometheus_label_name_is_refused(name: str) -> None:
    assert not is_label_name(name)


# --------------------------------------------------------------------------- #
# cable-list
# --------------------------------------------------------------------------- #


def csv_rows(payload: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload)))


def test_the_pull_list_has_one_row_per_cable_document(
    inventories: dict[str, Inventory],
) -> None:
    """A run through two panels is three rows, because it is three cables."""
    inventory = inventories["patch-room"]
    rows = csv_rows(run_export(inventory, "cable-list").payload)
    assert len(rows) == len(inventory.cables)
    assert {row["CABLE"] for row in rows} == set(inventory.cables)


def test_a_patched_run_names_the_link_it_belongs_to(inventories: dict[str, Inventory]) -> None:
    rows = {
        row["CABLE"]: row
        for row in csv_rows(run_export(inventories["patch-room"], "cable-list").payload)
    }
    segment = rows["cables/cbl-tie-07"]
    assert segment["SEGMENT"] == "2 of 3"
    assert "sw-core-01" in segment["RUN"] and "srv-app-01" in segment["RUN"]
    # A direct cable is its own run and leaves the columns blank.
    assert rows["cables/cbl-rtr-sw"]["RUN"] == ""
    assert rows["cables/cbl-rtr-sw"]["SEGMENT"] == ""


def test_both_ends_are_located_by_rack_and_unit(inventories: dict[str, Inventory]) -> None:
    rows = {
        row["CABLE"]: row
        for row in csv_rows(run_export(inventories["patch-room"], "cable-list").payload)
    }
    row = rows["cables/cbl-pp-app07"]
    assert (row["A_SITE"], row["A_ROOM"], row["A_RACK"], row["A_UNIT"]) == ("hq", "mdf", "r2", "42")
    assert row["A_PANEL_PORT"] == "front/7", "a patch panel endpoint names its panel position"
    assert row["B_PANEL_PORT"] == "", "a server endpoint is not a panel position"


def test_an_undeclared_length_is_blank_rather_than_zero(tmp_path: Path) -> None:
    """A pull list is read as a shopping list; ``0`` would be summed."""
    write_inventory(
        tmp_path,
        "pair.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-a}\n"
        "spec: {interfaces: [{name: e1, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata: {name: srv-b}\n"
        "spec: {interfaces: [{name: e1, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: c1}\n"
        "spec:\n"
        "  medium: copper\n"
        "  endpoints:\n"
        "    - {device: sw-a, interface: e1}\n"
        "    - {device: srv-b, interface: e1}\n",
    )
    rows = csv_rows(run_export(load_tree(tmp_path), "cable-list").payload)
    assert rows[0]["LENGTH_M"] == ""


def test_the_markdown_table_holds_the_same_rows_as_the_csv(
    inventories: dict[str, Inventory],
) -> None:
    """Neither style is the lossy one."""
    inventory = inventories["patch-room"]
    rows = csv_rows(run_export(inventory, "cable-list").payload)
    markdown = run_export(
        inventory, "cable-list", options=ExportOptions(table_format="markdown")
    ).payload
    body = [line for line in markdown.splitlines() if line.startswith("| ")]
    assert len(body) == len(rows) + 1  # the header row, plus one per cable
    for row in rows:
        assert row["CABLE"] in markdown


def test_a_cable_with_one_end_outside_the_selection_is_recorded(
    inventories: dict[str, Inventory],
) -> None:
    result = run_export(
        inventories["campus"], "cable-list", spec=FilterSpec(namespaces=("sites/north/access",))
    )
    dropped = result.manifest.of_reason(Reason.HALF_SELECTED)
    assert dropped, "an uplink out of the selection must not vanish silently"
    assert all(skip.subject in inventories["campus"].cables for skip in dropped)


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("export_format", ["hosts", "ansible-inventory", "prometheus-sd"])
def test_a_namespace_filter_scopes_an_export_as_it_scopes_a_render(
    export_format: str, inventories: dict[str, Inventory]
) -> None:
    inventory = inventories["campus"]
    spec = FilterSpec(namespaces=("sites/north",))
    payload = run_export(inventory, export_format, spec=spec).payload
    assert "north" in payload
    assert "sw-south-acc-01" not in payload
    assert "sw-west-acc-01" not in payload


def test_a_kind_filter_narrows_the_ansible_inventory(inventories: dict[str, Inventory]) -> None:
    document = json.loads(
        run_export(
            inventories["campus"], "ansible-inventory", spec=FilterSpec(kinds=("router",))
        ).payload
    )
    assert set(document) >= {"kind_router"}
    assert "kind_switch" not in document


# --------------------------------------------------------------------------- #
# Names, folding and escaping
# --------------------------------------------------------------------------- #

#: Every free-text field an artefact prints, carrying the characters that end a
#: field, a record or a line in one of the five grammars: a quote, a comma, a
#: newline, a pipe, a backslash, a semicolon and a non-ASCII letter.
HOSTILE = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-1
  description: "a \\"quoted\\", comma'd; semicolon\\nsecond line | pipe \\\\ backslash"
  labels:
    role: "Core, \\"primary\\""
  location:
    site: "hq, \\"main\\""
    room: "rüm 1"
    rack: "r|1"
    position: 12
    rack_height: 42
spec:
  vendor: "Ünicorn Networks, \\"Inc\\"; GmbH"
  interfaces:
    - name: mgmt0
      type: ethernet
      description: "port | with, everything \\"in\\" it"
      ipv4: {addresses: [10.9.0.2/24]}
---
apiVersion: netgraph.dev/v1alpha1
kind: server
metadata:
  name: srv-1
  location: {site: "hq, \\"main\\"", room: "rüm 1", rack: "r|1", position: 4}
spec:
  interfaces:
    - name: eno1
      type: ethernet
      ipv4: {addresses: [10.9.0.3/24]}
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: c-1
spec:
  medium: copper
  label: "L-\\"1\\", a\\nnewline | pipe \\\\ backslash ; semicolon"
  length_m: 3
  endpoints:
    - {device: sw-1, interface: mgmt0}
    - {device: srv-1, interface: eno1}
"""

#: A namespace is a directory name, which no grammar in the schema constrains.
#:
#: The quote is dropped on Windows, where it is one of the nine characters a file
#: name may not contain: ``mkdir`` fails with ERROR_INVALID_NAME and the fixture
#: never gets as far as an export. Substituted rather than skipped, because what
#: this namespace is here to prove is that a *directory* name reaches the five
#: grammars at all — carrying a comma, a space and a non-ASCII letter, all of
#: which survive. Quoting itself is exercised by the quotes in :data:`HOSTILE`
#: above, which are element content and travel to the same exporters.
HOSTILE_NAMESPACE = "Building A, 'main'/rüm 1" if ON_WINDOWS else 'Building A, "main"/rüm 1'


@pytest.fixture(scope="module")
def hostile(tmp_path_factory: pytest.TempPathFactory) -> Inventory:
    """An inventory whose every free-text field is adversarial."""
    root = tmp_path_factory.mktemp("hostile")
    write_inventory(root, f"{HOSTILE_NAMESPACE}/things.yaml", HOSTILE)
    return load_tree(root)


@pytest.mark.parametrize("export_format", FORMATS)
def test_a_hostile_inventory_exports_without_raising(
    export_format: str, hostile: Inventory
) -> None:
    result = run_export(hostile, export_format)
    # An empty payload is only legitimate for a configuration dialect that
    # selected nothing — ``frr`` over an inventory with no routing, ``wireguard``
    # over one with no tunnel. It then has to say so, which is the assertion that
    # keeps "nothing to configure" apart from "the emitter fell over".
    assert result.payload or (result.bundle is not None and result.manifest.skipped)
    # The manifest is itself a document a consumer parses, so it has to survive
    # the same input.
    assert json.loads(result.manifest.to_json())["kind"] == "ExportManifest"


def test_hostile_names_reach_the_hosts_file_as_legal_host_labels(hostile: Inventory) -> None:
    for names in hosts_entries(run_export(hostile, "hosts").payload).values():
        for name in names:
            for label in name.split("."):
                assert is_host_label(label), f"{label!r} is not a legal host label"


def test_hostile_names_reach_the_zone_as_legal_owner_names(hostile: Inventory) -> None:
    payload = run_export(hostile, "dns-zone").payload
    for _, owner, _, _ in zone_records(payload):
        if owner in {"@"}:
            continue
        for label in owner.rstrip(".").split("."):
            assert is_host_label(label), f"{label!r} is not a legal owner label"


def test_the_zone_never_carries_a_bare_semicolon_in_a_record(hostile: Inventory) -> None:
    """A semicolon opens a comment; one inside a record would truncate it."""
    payload = run_export(hostile, "dns-zone").payload
    for line in payload.splitlines():
        if line.startswith(";") or not line.strip():
            continue
        assert ";" not in line or line.split(";", 1)[0].strip(), line


def test_hostile_free_text_round_trips_through_the_ansible_json(hostile: Inventory) -> None:
    """JSON escaping is json's job; the test is that nothing is mangled first."""
    document = json.loads(run_export(hostile, "ansible-inventory").payload)
    host = next(
        variables
        for name, variables in document["_meta"]["hostvars"].items()
        if name.startswith("sw-1")
    )
    assert '"quoted"' in host["netgraph_description"]
    assert "\n" in host["netgraph_description"]
    assert host["netgraph_vendor"].startswith("Ünicorn")


def test_hostile_free_text_round_trips_through_the_prometheus_json(hostile: Inventory) -> None:
    groups = json.loads(run_export(hostile, "prometheus-sd").payload)
    sites = {group["labels"]["netgraph_site"] for group in groups}
    assert sites == {'hq, "main"'}


def test_hostile_free_text_round_trips_through_the_csv(hostile: Inventory) -> None:
    """RFC 4180 quoting, checked by parsing the output back with :mod:`csv`."""
    payload = run_export(hostile, "cable-list").payload
    rows = csv_rows(payload)
    assert len(rows) == 1
    label = rows[0]["LABEL"]
    assert '"1"' in label and "\n" in label and "\\ backslash" in label
    assert rows[0]["A_RACK"] == "r|1"


def test_hostile_free_text_cannot_break_a_markdown_row(hostile: Inventory) -> None:
    """A pipe would end the cell and a newline would end the row."""
    payload = run_export(
        hostile, "cable-list", options=ExportOptions(table_format="markdown")
    ).payload
    body = [line for line in payload.splitlines() if line.startswith("| ")]
    assert len(body) == 2, "the header row and exactly one cable"
    for line in body:
        # Escaped pipes do not count as cell boundaries.
        assert len(re.findall(r"(?<!\\)\|", line)) == len(_MARKDOWN_COLUMNS) + 1


_MARKDOWN_COLUMNS = tuple(range(23))


def test_a_very_long_name_is_truncated_to_a_legal_label(tmp_path: Path) -> None:
    long_name = "a" * 200
    write_inventory(
        tmp_path,
        f"{long_name}/x.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        f"metadata: {{name: {'s' * 200}}}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eno1\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.9.0.3/24]}\n",
    )
    result = run_export(load_tree(tmp_path), "hosts")
    for names in hosts_entries(result.payload).values():
        for label in names[0].split("."):
            assert len(label) <= MAX_DNS_LABEL
    assert result.manifest.rewritten, "a truncation must be reported"


def test_a_name_that_folds_away_entirely_is_dropped_from_the_domain(tmp_path: Path) -> None:
    """A namespace with no ASCII fold shortens the name; it does not empty it."""
    write_inventory(
        tmp_path,
        "日本語/x.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata: {name: srv-1}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eno1\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.9.0.3/24]}\n",
    )
    entries = hosts_entries(run_export(load_tree(tmp_path), "hosts").payload)
    assert entries["10.9.0.3"] == ["srv-1"]


def test_two_elements_that_fold_to_one_name_do_not_both_claim_it(tmp_path: Path) -> None:
    for namespace in ("north", "south"):
        write_inventory(
            tmp_path,
            f"{namespace}/s.yaml",
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: switch\n"
            "metadata: {name: sw-01}\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: mgmt0\n"
            "      type: ethernet\n"
            f"      ipv4: {{addresses: [10.9.{1 if namespace == 'north' else 2}.2/24]}}\n",
        )
    result = run_export(load_tree(tmp_path), "hosts")
    entries = hosts_entries(result.payload)
    published = [name for names in entries.values() for name in names]
    assert published.count("sw-01") == 1, "the bare alias must resolve to one element"
    assert result.manifest.of_reason(Reason.NAME_COLLISION)


# --------------------------------------------------------------------------- #
# The name folds themselves
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sw-01", "sw-01"),
        ("SW-01", "sw-01"),
        ("Building A", "building-a"),
        ("münchen", "munchen"),
        ("a  b", "a-b"),
        ("--edge--", "edge"),
        ("日本語", ""),
        ("x" * 100, "x" * MAX_DNS_LABEL),
    ],
)
def test_sanitise_label(text: str, expected: str) -> None:
    assert sanitise_label(text) == expected


def test_a_folded_label_is_always_legal() -> None:
    for text in ("a-", "-a-", "1", "..", "ü", "a" * 64, "a b, c"):
        folded = sanitise_label(text)
        assert folded == "" or is_host_label(folded)


def test_dns_labels_reverse_the_namespace() -> None:
    assert dns_labels("sites/north/access/sw-01") == ("sw-01", "access", "north", "sites")
    assert dns_labels("sw-01") == ("sw-01",)
    # A dot in a segment already separates labels and is kept as one.
    assert dns_labels("a.b/sw.01") == ("sw", "01", "a", "b")
    assert dns_labels("") == ()
    assert dns_labels("日本語") == ()


def test_transliterate_keeps_the_letter_rather_than_eating_it() -> None:
    assert transliterate("Ünicorn") == "Unicorn"
    assert transliterate("café") == "cafe"
    assert transliterate("日本語") == ""


@pytest.mark.parametrize(
    ("text", "prefix", "expected"),
    [
        ("switch", "kind_", "kind_switch"),
        ("Cisco", "vendor_", "vendor_cisco"),
        ("Ubiquiti Networks", "vendor_", "vendor_ubiquiti_networks"),
        ("Ünicorn", "vendor_", "vendor_unicorn"),
        ("2nd floor", "ns_", "ns_2nd_floor"),
        ("2nd floor", "", "nd_floor"),
        ("日本語", "ns_", "ns__"),
    ],
)
def test_ansible_identifier(text: str, prefix: str, expected: str) -> None:
    assert ansible_identifier(text, prefix=prefix) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ("a|b", r"a\|b"),
        ("a\\b", r"a\\b"),
        ("a\nb", "a<br>b"),
        ("a\r\nb", "a<br>b"),
    ],
)
def test_markdown_cell(text: str, expected: str) -> None:
    assert markdown_cell(text) == expected


@pytest.mark.parametrize(
    "text", ["example.com", "example.com.", "a.b.c.example.org", "_tcp.example.com", "x"]
)
def test_a_domain_name_is_accepted_and_made_absolute(text: str) -> None:
    assert domain_name(text).endswith(".")
    assert is_domain_name(text)


@pytest.mark.parametrize(
    "text", ["", "example .com", "-bad.example.com", "a" * 64 + ".com", "a..b"]
)
def test_a_malformed_domain_name_is_refused(text: str) -> None:
    assert not is_domain_name(text)
    with pytest.raises(ValueError, match=r"domain name|labels"):
        domain_name(text)


def test_a_domain_name_longer_than_the_rfc_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="255"):
        domain_name(".".join("a" * 60 for _ in range(6)))


# --------------------------------------------------------------------------- #
# Address selection
# --------------------------------------------------------------------------- #


def build_node(inventory: Inventory, fqn: str) -> object:
    return build_graph(inventory, layer=Layer.L1).nodes[fqn]


def test_a_management_interface_wins_over_everything_else(tmp_path: Path) -> None:
    write_inventory(
        tmp_path,
        "d.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-1}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: Vlan10\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.0.10.2/24]}\n"
        "    - name: lo0\n"
        "      type: loopback\n"
        "      ipv4: {addresses: [192.0.2.9/32]}\n"
        "    - name: eth9\n"
        "      type: ethernet\n"
        "      description: out-of-band management port\n"
        "      ipv4: {addresses: [10.99.0.2/24]}\n",
    )
    node = build_node(load_tree(tmp_path), "sw-1")
    chosen = management_address(node)  # type: ignore[arg-type]
    assert chosen is not None and chosen.ip == "10.99.0.2"


def test_ipv4_is_preferred_over_ipv6_at_the_same_rank(tmp_path: Path) -> None:
    write_inventory(
        tmp_path,
        "d.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata: {name: srv-1}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eno1\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.0.0.5/24]}\n"
        "      ipv6: {addresses: ['2001:db8::5/64']}\n",
    )
    node = build_node(load_tree(tmp_path), "srv-1")
    chosen = management_address(node)  # type: ignore[arg-type]
    assert chosen is not None and chosen.version == 4


def test_an_element_with_only_loopback_addresses_has_no_management_address(
    tmp_path: Path,
) -> None:
    write_inventory(
        tmp_path,
        "d.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-1}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: lo\n"
        "      type: loopback\n"
        "      ipv4: {addresses: [127.0.0.1/8]}\n",
    )
    node = build_node(load_tree(tmp_path), "pc-1")
    assert management_address(node) is None  # type: ignore[arg-type]
    assert element_addresses(node) == ()  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #


def test_the_manifest_is_a_versioned_document() -> None:
    manifest = Recorder().sealed("hosts")
    record = json.loads(manifest.to_json())
    assert record["apiVersion"] == "netgraph.dev/v1alpha1"
    assert record["kind"] == "ExportManifest"
    assert record["format"] == "hosts"
    assert record["skipped"] == [] and record["rewritten"] == []
    assert manifest.is_clean


def test_a_clean_export_still_produces_a_manifest(inventories: dict[str, Inventory]) -> None:
    """A consumer must not have to tell "nothing skipped" from "nothing said"."""
    result = run_export(inventories["quickstart"], "cable-list")
    assert json.loads(result.manifest.to_json())["counts"]["skipped"] == 0


def test_the_manifest_is_sorted_canonically() -> None:
    recorder = Recorder()
    recorder.skip("z/b", Reason.NO_ADDRESS, "later")
    recorder.skip("a/b", Reason.NOT_ROUTABLE, "earlier")
    recorder.rewrite("z/b", field="hostname", original="Z B", rewritten="z-b")
    recorder.rewrite("a/b", field="hostname", original="A B", rewritten="a-b")
    manifest = recorder.sealed("hosts")
    assert [entry.subject for entry in manifest.skipped] == ["a/b", "z/b"]
    assert [entry.subject for entry in manifest.rewritten] == ["a/b", "z/b"]


def test_a_rewrite_that_changes_nothing_is_not_recorded() -> None:
    recorder = Recorder()
    recorder.rewrite("a", field="hostname", original="a", rewritten="a")
    assert recorder.sealed("hosts").rewritten == ()


def test_the_summary_names_the_reasons() -> None:
    recorder = Recorder()
    recorder.considered = 3
    recorder.emitted = 1
    recorder.skip("a", Reason.NO_ADDRESS)
    recorder.skip("b", Reason.NO_ADDRESS)
    summary = recorder.sealed("hosts").summary()
    assert "1 of 3 emitted" in summary
    assert "no-address 2" in summary


def test_dangling_links_reach_the_manifest(tmp_path: Path) -> None:
    """Only ``--force`` gets this far, and it must not get there silently."""
    write_inventory(
        tmp_path,
        "broken.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-a}\n"
        "spec: {interfaces: [{name: e1, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: c1}\n"
        "spec:\n"
        "  medium: copper\n"
        "  endpoints:\n"
        "    - {device: sw-a, interface: e1}\n"
        "    - {device: nowhere, interface: e1}\n",
    )
    result = run_export(load_tree(tmp_path), "cable-list")
    unresolved = result.manifest.of_reason(Reason.UNRESOLVED)
    assert unresolved and unresolved[0].subject == "c1"


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


#: The example and the element that give each configuration dialect something to
#: write. ``frr`` needs a device that declares routing and ``wireguard`` one that
#: terminates a tunnel; the home lab has neither, and a dialect with nothing to
#: say correctly writes nothing — which is asserted separately.
_CONFIG_SUBJECTS: Final[dict[str, tuple[str, str]]] = {
    "frr": ("campus", "sw-north-acc-01"),
    "nftables": ("campus", "rtr-west-core-01"),
    "wireguard": ("overlay", "rtr-hq"),
}


@pytest.mark.parametrize("export_format", FORMATS)
def test_the_cli_writes_the_artefact_to_stdout_and_the_manifest_to_stderr(
    export_format: str, runner: CliRunner
) -> None:
    """``netgraph export ... | consumer`` must not be polluted by commentary."""
    example, name = _CONFIG_SUBJECTS.get(export_format, ("home-lab", "rtr-home"))
    arguments = ["-i", str(EXAMPLES / example), "export", export_format]
    if export_format == "dns-zone":
        arguments += ["--origin", "example.com"]
    if export_format in CONFIG_FORMATS:
        # A configuration dialect's artefact is a tree, and stdout holds one
        # device's; the multi-device form is ``--out DIR``.
        arguments += ["--name", name]
    result = runner.invoke(cli, arguments, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert result.stdout
    assert "ExportManifest" not in result.stdout
    assert json.loads(_manifest_of(result.stderr))["kind"] == "ExportManifest"


def _manifest_of(stderr: str) -> str:
    """The JSON object in a stderr stream that also carries commentary lines."""
    start = stderr.index("{")
    end = stderr.rindex("}")
    return stderr[start : end + 1]


def test_the_cli_writes_to_the_named_file(runner: CliRunner, tmp_path: Path) -> None:
    output = tmp_path / "nested" / "hosts.txt"
    manifest = tmp_path / "manifest.json"
    result = runner.invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "home-lab"),
            "export",
            "hosts",
            "-o",
            str(output),
            "--manifest",
            str(manifest),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert output.read_text(encoding="utf-8").startswith("# Generated by")
    assert json.loads(manifest.read_text(encoding="utf-8"))["format"] == "hosts"
    assert result.stdout == ""


def test_dns_zone_without_an_origin_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["-i", str(EXAMPLES / "home-lab"), "export", "dns-zone"])
    assert result.exit_code == 2
    assert "--origin" in result.output


def test_a_malformed_origin_is_refused_before_the_inventory_is_read(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["-i", str(EXAMPLES / "home-lab"), "export", "dns-zone", "--origin", "example .com"]
    )
    assert result.exit_code == 2
    assert "domain name" in result.output


@pytest.mark.parametrize(
    ("option", "value", "export_format"),
    [
        ("--origin", "example.com", "hosts"),
        ("--ttl", "60", "cable-list"),
        ("--port", "9100", "hosts"),
        ("--label", "a=b", "cable-list"),
        ("--table-format", "markdown", "hosts"),
        ("--zones", "forward", "prometheus-sd"),
    ],
)
def test_an_option_the_format_cannot_use_is_a_usage_error(
    option: str, value: str, export_format: str, runner: CliRunner
) -> None:
    """A silently ignored flag is worse than an error: the user believes it worked."""
    result = runner.invoke(
        cli, ["-i", str(EXAMPLES / "home-lab"), "export", export_format, option, value]
    )
    assert result.exit_code == 2
    assert option in result.output and export_format in result.output


def test_a_malformed_label_is_refused(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["-i", str(EXAMPLES / "home-lab"), "export", "prometheus-sd", "--label", "nope"]
    )
    assert result.exit_code == 2
    assert "KEY=VALUE" in result.output


def test_a_reserved_label_is_refused(runner: CliRunner) -> None:
    result = runner.invoke(
        cli,
        ["-i", str(EXAMPLES / "home-lab"), "export", "prometheus-sd", "--label", "__meta_x=1"],
    )
    assert result.exit_code == 2
    assert "reserved" in result.output


def test_an_invalid_inventory_refuses_to_export(runner: CliRunner, tmp_path: Path) -> None:
    write_inventory(
        tmp_path,
        "broken.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: c1}\n"
        "spec:\n"
        "  medium: copper\n"
        "  endpoints:\n"
        "    - {device: nowhere, interface: e1}\n"
        "    - {device: elsewhere, interface: e1}\n",
    )
    result = runner.invoke(cli, ["-i", str(tmp_path), "export", "hosts"])
    assert result.exit_code == 1
    assert "--force" in result.output


def test_force_exports_an_invalid_inventory_with_a_warning(
    runner: CliRunner, tmp_path: Path
) -> None:
    write_inventory(
        tmp_path,
        "broken.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: c1}\n"
        "spec:\n"
        "  medium: copper\n"
        "  endpoints:\n"
        "    - {device: nowhere, interface: e1}\n"
        "    - {device: elsewhere, interface: e1}\n",
    )
    result = runner.invoke(
        cli, ["-i", str(tmp_path), "export", "cable-list", "--force"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "may not match the network" in result.stderr


def test_the_cli_filters_reuse_the_render_selection(runner: CliRunner) -> None:
    result = runner.invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "campus"),
            "export",
            "hosts",
            "--namespace",
            "sites/north",
            "--kind",
            "router",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "rtr-north-core-01" in result.stdout
    assert "sw-north-acc-01" not in result.stdout


def test_quiet_silences_the_manifest_but_not_the_artefact(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["-q", "-i", str(EXAMPLES / "home-lab"), "export", "hosts"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert result.stdout.startswith("# Generated by")
    assert "ExportManifest" not in result.stderr


def test_the_help_lists_every_registered_format(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["export", "--help"], catch_exceptions=False)
    for name in FORMATS:
        assert name in result.output


def test_completion_offers_every_format() -> None:
    from netgraph.completion import complete_export_format

    items = complete_export_format(None, None, "")  # type: ignore[arg-type]
    assert {item.value for item in items} == set(FORMATS)
    assert all(item.help for item in items)


# --------------------------------------------------------------------------- #
# Coverage of the corners the CLI cannot reach
# --------------------------------------------------------------------------- #


def test_an_unknown_format_raises() -> None:
    with pytest.raises(KeyError):
        export("no-such-format", lambda recorder: _empty_context(recorder))


def _empty_context(recorder: Recorder) -> ExportContext:  # pragma: no cover - never reached
    raise AssertionError("the registry lookup happens first")


def test_asking_for_a_layer_the_format_did_not_declare_raises(
    inventories: dict[str, Inventory],
) -> None:
    context = ExportContext(
        inventory=inventories["home-lab"],
        graphs={},
        options=ExportOptions(),
        recorder=Recorder(),
    )
    with pytest.raises(KeyError):
        context.at(Layer.L1)


def test_the_name_registry_reports_a_name_that_folds_away() -> None:
    """Unreachable through the loader — an element name is grammar-checked."""

    class _Fake:
        fqn = "日本語/日本語"
        kind = "server"

    recorder = Recorder()
    registry = NameRegistry(recorder)
    assert registry.register(_Fake()) is None  # type: ignore[arg-type]
    assert recorder.sealed("hosts").of_reason(Reason.NOT_REPRESENTABLE)


def test_an_address_sorts_by_family_then_value() -> None:
    def make(text: str) -> Address:
        interface = ipaddress.ip_interface(text)
        return Address(
            element="a",
            interface="e1",
            index=0,
            ip=str(interface.ip),
            cidr=text,
            network=interface.network,
        )

    ordered = sorted(
        [make("2001:db8::1/64"), make("10.0.0.9/24"), make("10.0.0.10/24")], key=lambda a: a.packed
    )
    assert [address.ip for address in ordered] == ["10.0.0.9", "10.0.0.10", "2001:db8::1"]
    assert make("2001:db8::1/64").target == "[2001:db8::1]"
    assert make("10.0.0.9/24").record_type == "A"
    assert make("2001:db8::1/64").record_type == "AAAA"


def test_every_golden_is_covered_by_a_case() -> None:
    """A golden nobody asserts on is a file that rots."""
    expected = {golden_path(stem, export_format).name for stem, _, export_format, _ in CASES}
    present = {path.name for path in GOLDEN_DIR.glob("*") if path.is_file()}
    assert present == expected


def iter_reasons() -> Iterator[Reason]:
    yield from Reason


@pytest.mark.parametrize("reason", list(iter_reasons()), ids=lambda reason: reason.value)
def test_every_reason_is_a_stable_token(reason: Reason) -> None:
    assert str(reason) == reason.value
    assert re.match(r"^[a-z][a-z-]*[a-z]$", reason.value)


def test_a_name_too_long_for_the_origin_is_skipped_rather_than_emitted(tmp_path: Path) -> None:
    """RFC 1035 §2.3.4 bounds the whole name, not only each label.

    Four maximal labels plus an origin exceeds 255 octets, and a nameserver
    refuses the zone rather than the record — so one over-long element would
    cost the operator every other record in the file.
    """
    namespace = "/".join("n" * MAX_DNS_LABEL for _ in range(4))
    write_inventory(
        tmp_path,
        f"{namespace}/x.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata: {name: srv-1}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eno1\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.9.0.3/24]}\n",
    )
    result = run_export(load_tree(tmp_path), "dns-zone")
    addresses = [record for record in zone_records(result.payload) if record[2] in {"A", "AAAA"}]
    assert addresses == [], "no address record may be written for it"
    skipped = result.manifest.of_reason(Reason.NOT_REPRESENTABLE)
    assert skipped and "255" in skipped[0].detail


def test_an_element_with_no_address_at_all_is_reported_as_such(tmp_path: Path) -> None:
    """An unnumbered access switch is normal; the manifest still has to say so."""
    write_inventory(
        tmp_path,
        "s.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-dumb}\n"
        "spec: {interfaces: [{name: e1, type: ethernet}, {name: e2, type: ethernet}]}\n",
    )
    manifest = run_export(load_tree(tmp_path), "hosts").manifest
    skipped = manifest.of_reason(Reason.NO_ADDRESS)
    assert [skip.subject for skip in skipped] == ["sw-dumb"]
    assert "2 interface(s), none with an address" in skipped[0].detail


def _two_sites_named_alike(root: Path) -> Inventory:
    """One ``sw-01`` in each of two namespaces — ordinary, and ambiguous."""
    for namespace in ("a", "b"):
        write_inventory(
            root,
            f"{namespace}/s.yaml",
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: switch\n"
            "metadata: {name: sw-01}\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: mgmt0\n"
            "      type: ethernet\n"
            f"      ipv4: {{addresses: [10.9.{1 if namespace == 'a' else 2}.2/24]}}\n",
        )
    return load_tree(root)


@pytest.mark.parametrize("export_format", ["dns-zone", "ansible-inventory", "prometheus-sd"])
def test_a_short_name_shared_by_two_namespaces_is_not_a_problem_for_a_format_with_no_aliases(
    export_format: str, tmp_path: Path
) -> None:
    """Only ``hosts`` publishes a short alias, so only it can lose one.

    Reporting an alias collision from a zone file — which the module docstring
    says publishes one name per element — would fill the manifest with skips
    describing something the format never emits, and would make
    ``counts.skipped == 0`` unusable as a CI gate over any two-site inventory.
    """
    result = run_export(_two_sites_named_alike(tmp_path), export_format)
    assert result.manifest.of_reason(Reason.NAME_COLLISION) == ()
    assert result.manifest.emitted == 2


def test_hosts_gives_a_shared_short_alias_to_exactly_one_element(tmp_path: Path) -> None:
    result = run_export(_two_sites_named_alike(tmp_path), "hosts")
    published = [name for names in hosts_entries(result.payload).values() for name in names]
    assert published.count("sw-01") == 1
    assert result.manifest.of_reason(Reason.NAME_COLLISION)


def test_a_reverse_export_of_an_unaddressed_inventory_writes_no_zone(tmp_path: Path) -> None:
    """An empty zone file would claim a delegation that holds nothing."""
    write_inventory(
        tmp_path,
        "s.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-01}\n"
        "spec: {interfaces: [{name: e1, type: ethernet}]}\n",
    )
    payload = run_export(
        load_tree(tmp_path), "dns-zone", options=ExportOptions(origin=ORIGIN, zones="reverse")
    ).payload
    assert ";; zone:" not in payload


def test_a_filter_that_empties_a_prefix_drops_its_reverse_zone(
    inventories: dict[str, Inventory],
) -> None:
    payload = run_export(
        inventories["campus"],
        "dns-zone",
        options=ExportOptions(origin=ORIGIN, zones="reverse"),
        spec=FilterSpec(namespaces=("sites/north",)),
    ).payload
    zones = {origin for origin, _, kind, _ in zone_records(payload) if kind == "PTR"}
    assert zones and all(zone.endswith(".arpa.") for zone in zones)
    assert "2.1.10.in-addr.arpa." not in zones, "a southern prefix must not appear"


def test_the_registry_reports_the_conventional_suffix_of_each_format() -> None:
    from netgraph.export import suffix_for

    assert suffix_for("cable-list") == ".csv"
    assert suffix_for("hosts") == ".hosts"


def test_the_manifest_type_is_importable_for_a_consumer() -> None:
    """The public name a caller annotates against."""
    assert Manifest("hosts").export_format == "hosts"


# --------------------------------------------------------------------------- #
# Regressions
# --------------------------------------------------------------------------- #


def test_a_management_interface_is_matched_however_it_is_capitalised(tmp_path: Path) -> None:
    """Vendors write ``Mgmt1`` and ``Management1``; operators write ``iLO``.

    The ranking is worthless if it only fires on the lower-case spelling, and
    the failure is silent: a target list quietly full of data-plane addresses.
    """
    write_inventory(
        tmp_path,
        "d.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-1}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eth0\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.0.0.9/24]}\n"
        "    - name: Vlan99\n"
        "      type: vlan\n"
        "      parent: eth0\n"
        "      vlan: {mode: access, access_vlan: 99}\n"
        "      description: Out-of-band Management gateway\n"
        "      ipv4: {addresses: [192.168.99.5/24]}\n",
    )
    node = build_node(load_tree(tmp_path), "sw-1")
    chosen = management_address(node)  # type: ignore[arg-type]
    assert chosen is not None and chosen.ip == "192.168.99.5"


def test_an_element_whose_name_holds_dots_keeps_all_of_them_in_its_alias(
    tmp_path: Path,
) -> None:
    """``core.example.com`` is not called ``core``, and must not claim it.

    ``metadata.name`` may hold dots under §4.1, which are labels in DNS. Taking
    only the first would publish an alias belonging to a different element and
    point ``ping core`` at the wrong machine.
    """
    for name, address in (("core.example.com", "10.9.0.1"), ("core", "10.9.0.2")):
        write_inventory(
            tmp_path,
            f"ns/{name}.yaml",
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: server\n"
            f"metadata: {{name: {name}}}\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: eno1\n"
            "      type: ethernet\n"
            f"      ipv4: {{addresses: [{address}/24]}}\n",
        )
    result = run_export(load_tree(tmp_path), "hosts")
    entries = hosts_entries(result.payload)
    assert entries["10.9.0.1"] == ["core.example.com.ns", "core.example.com"]
    assert entries["10.9.0.2"] == ["core.ns", "core"]
    assert result.manifest.of_reason(Reason.NAME_COLLISION) == ()


def test_a_root_level_element_cannot_share_a_name_with_another_ones_alias(
    tmp_path: Path,
) -> None:
    """Qualified names and aliases live in one namespace in the artefact.

    A root-level ``sw-01`` and the alias of ``sites/sw-01`` are the same string
    to a resolver, so issuing both would map one name to two machines.
    """
    write_inventory(
        tmp_path,
        "sw-01.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-01}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: e1\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.9.0.1/24]}\n",
    )
    write_inventory(
        tmp_path,
        "sites/sw-01.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-01}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: e1\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.9.0.2/24]}\n",
    )
    result = run_export(load_tree(tmp_path), "hosts")
    published = [name for names in hosts_entries(result.payload).values() for name in names]
    assert len(published) == len(set(published)), f"a name was issued twice: {published}"
    assert result.manifest.of_reason(Reason.NAME_COLLISION)


def test_a_namespace_segment_starting_with_a_digit_keeps_it(tmp_path: Path) -> None:
    """``1-north`` and ``2-north`` are two sites, not one group.

    Dropping the leading digit of each *segment* merged them, and a playbook
    targeting the group would have reached both fleets.
    """
    for index, address in ((1, "10.9.1.2"), (2, "10.9.2.2")):
        write_inventory(
            tmp_path,
            f"sites/{index}-north/s.yaml",
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: switch\n"
            f"metadata: {{name: sw-{index}}}\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: mgmt0\n"
            "      type: ethernet\n"
            f"      ipv4: {{addresses: [{address}/24]}}\n",
        )
    document = json.loads(run_export(load_tree(tmp_path), "ansible-inventory").payload)
    assert "ns_sites_1_north" in document
    assert "ns_sites_2_north" in document
    assert document["ns_sites_1_north"]["hosts"] != document["ns_sites_2_north"]["hosts"]


def test_two_namespaces_that_fold_to_one_group_are_reported(tmp_path: Path) -> None:
    """``a/b`` and ``a b`` are different namespaces and one identifier."""
    for namespace, name, address in (("a/b", "deep", "10.9.0.1"), ("a b", "flat", "10.9.0.2")):
        write_inventory(
            tmp_path,
            f"{namespace}/s.yaml",
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: server\n"
            f"metadata: {{name: {name}}}\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: eno1\n"
            "      type: ethernet\n"
            f"      ipv4: {{addresses: [{address}/24]}}\n",
        )
    result = run_export(load_tree(tmp_path), "ansible-inventory")
    merged = result.manifest.of_reason(Reason.NAME_COLLISION)
    assert merged, "a silent group merge would reach more hosts than a playbook meant"
    assert "ns_a_b" in merged[0].detail


def test_a_group_rename_is_reported_once_not_once_per_host(
    inventories: dict[str, Inventory],
) -> None:
    manifest = run_export(inventories["campus"], "ansible-inventory").manifest
    subjects = [entry.subject for entry in manifest.rewritten if entry.field == "group"]
    assert len(subjects) == len(set(subjects))


@pytest.mark.parametrize("name", ["instance", "netgraph_kind", "netgraph_anything"])
def test_a_label_the_emitter_computes_cannot_be_set_statically(name: str) -> None:
    """A static ``instance`` would give every target in the estate one identity."""
    assert is_label_name(name)
    assert not is_assignable_label(name)


def test_the_cli_refuses_a_label_that_would_overwrite_an_identity(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["-i", str(EXAMPLES / "home-lab"), "export", "prometheus-sd", "--label", "instance=x"]
    )
    assert result.exit_code == 2
    assert "instance" in result.output


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ('=HYPERLINK("http://x")', '\'=HYPERLINK("http://x")'),
        ("+1", "'+1"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("-2+3", "'-2+3"),
        # A genuine negative number is a value, not a formula.
        ("-3", "-3"),
        ("-3.5", "-3.5"),
        (None, ""),
    ],
)
def test_csv_cell_neutralises_a_formula_without_mangling_a_number(
    text: object, expected: str
) -> None:
    assert csv_cell(text) == expected


def test_a_cable_label_cannot_become_a_spreadsheet_formula(tmp_path: Path) -> None:
    """The pull list is the one artefact certain to be opened in a spreadsheet."""
    write_inventory(
        tmp_path,
        "net.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-01}\n"
        "spec: {interfaces: [{name: e1, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata: {name: srv-01}\n"
        "spec: {interfaces: [{name: e1, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: c-evil}\n"
        "spec:\n"
        '  label: \'=HYPERLINK("http://evil","click")\'\n'
        "  medium: copper\n"
        "  endpoints:\n"
        "    - {device: sw-01, interface: e1}\n"
        "    - {device: srv-01, interface: e1}\n",
    )
    rows = csv_rows(run_export(load_tree(tmp_path), "cable-list").payload)
    assert rows[0]["LABEL"].startswith("'="), rows[0]["LABEL"]


def test_a_vlan_filtered_cable_is_not_blamed_on_a_missing_endpoint(tmp_path: Path) -> None:
    """Both devices are in the VLAN and the cable is not; the reasons differ."""
    write_inventory(
        tmp_path,
        "net.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-a}\n"
        "spec:\n"
        "  vlans: [{id: 10}, {id: 20}]\n"
        "  interfaces:\n"
        "    - {name: e1, type: ethernet, vlan: {mode: access, access_vlan: 10}}\n"
        "    - {name: e2, type: ethernet, vlan: {mode: access, access_vlan: 20}}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-b}\n"
        "spec:\n"
        "  vlans: [{id: 10}, {id: 20}]\n"
        "  interfaces:\n"
        "    - {name: e1, type: ethernet, vlan: {mode: access, access_vlan: 10}}\n"
        "    - {name: e2, type: ethernet, vlan: {mode: access, access_vlan: 20}}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: c-10}\n"
        "spec:\n"
        "  medium: copper\n"
        "  endpoints: [{device: sw-a, interface: e1}, {device: sw-b, interface: e1}]\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: c-20}\n"
        "spec:\n"
        "  medium: copper\n"
        "  endpoints: [{device: sw-a, interface: e2}, {device: sw-b, interface: e2}]\n",
    )
    result = run_export(load_tree(tmp_path), "cable-list", spec=FilterSpec(vlans=frozenset({10})))
    assert [row["CABLE"] for row in csv_rows(result.payload)] == ["c-10"]
    dropped = result.manifest.of_reason(Reason.NOT_SELECTED)
    assert [skip.subject for skip in dropped] == ["c-20"]
    assert result.manifest.of_reason(Reason.HALF_SELECTED) == ()


def test_one_address_on_two_interfaces_is_published_once(tmp_path: Path) -> None:
    """Two identical A records are one RRSet with a duplicate in it (RFC 2181 §5)."""
    write_inventory(
        tmp_path,
        "d.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: dup}\n"
        "spec:\n"
        "  vlans: [{id: 10}, {id: 20}]\n"
        "  interfaces:\n"
        "    - {name: e1, type: ethernet}\n"
        "    - {name: br0, type: bridge, members: [e1]}\n"
        "    - name: Vlan10\n"
        "      type: vlan\n"
        "      parent: br0\n"
        "      ipv4: {addresses: [10.0.0.1/24]}\n"
        "      vlan: {mode: access, access_vlan: 10}\n"
        "    - name: Vlan20\n"
        "      type: vlan\n"
        "      parent: br0\n"
        "      ipv4: {addresses: [10.0.0.1/24]}\n"
        "      vlan: {mode: access, access_vlan: 20}\n",
    )
    inventory = load_tree(tmp_path)
    records = [
        record
        for record in zone_records(run_export(inventory, "dns-zone").payload)
        if record[2] in {"A", "PTR"}
    ]
    assert len(records) == len(set(records)), records
    document = json.loads(run_export(inventory, "ansible-inventory").payload)
    addresses = document["_meta"]["hostvars"]["dup"]["netgraph_addresses"]
    assert addresses == ["10.0.0.1/24"]


def test_quiet_still_writes_a_named_manifest_file(runner: CliRunner, tmp_path: Path) -> None:
    """``--quiet`` silences the commentary copy, never the file that was asked for."""
    manifest = tmp_path / "m.json"
    result = runner.invoke(
        cli,
        [
            "-q",
            "-i",
            str(EXAMPLES / "home-lab"),
            "export",
            "hosts",
            "-o",
            str(tmp_path / "out"),
            "--manifest",
            str(manifest),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert json.loads(manifest.read_text(encoding="utf-8"))["kind"] == "ExportManifest"


@pytest.mark.parametrize("export_format", ["hosts", "dns-zone", "ansible-inventory"])
def test_the_banner_carries_the_running_version(
    export_format: str, inventories: dict[str, Inventory]
) -> None:
    """The goldens elide it; something still has to assert it is there.

    ``prometheus-sd`` and the CSV form of ``cable-list`` are absent on purpose:
    neither format has anywhere safe to put provenance. See
    :mod:`netgraph.export.header`.
    """
    payload = run_export(inventories["home-lab"], export_format).payload
    assert GENERATOR in payload
    assert _VERSION_PLACEHOLDER not in payload
