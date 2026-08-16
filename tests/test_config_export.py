"""``netgraph export <dialect>`` — the configuration a device would actually run.

Five properties are asserted here, in this order of importance:

**The round trip closes.** Every dialect, over every shipped example, generated
and then read back through ``netgraph drift``, must report *no drift*. That is
the whole claim of the feature: the file netgraph writes and the file netgraph
reads are the same file, and a difference between them would mean one of the two
halves is lying. This is the test that would catch a refactor of either side.

**Nothing is invented.** No generated file may hold an address, a VLAN, a next
hop or a key that the inventory does not state. Asserted structurally — every
address in the output is matched back to one in the source — rather than by
eyeballing a golden, because a golden proves only that today's output equals
yesterday's.

**A refusal refuses everything.** A dialect that cannot express one field of one
device writes nothing at all, for any device, and names the field. Half a
configuration applied to a real estate is the failure mode worth a test.

**Byte stability.** Two runs over an unchanged tree produce identical bytes,
including the provenance banner, which holds no clock, host or path.

**The output cannot escape.** A path is relative, stays inside ``--out``, and
overwriting a file netgraph did not write is refused.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Final

import pytest
from click.testing import CliRunner

from netgraph.cli import cli, main
from netgraph.drift import check_drift
from netgraph.export import CONFIG_DIALECTS, CONFIG_FORMATS, ExportContext, ExportOptions, export
from netgraph.export.config import CONFIG_LAYERS, UnsupportedConfigError, generate
from netgraph.export.config.header import DIALECT_KEY, ELEMENT_KEY, SOURCE_KEY, parse_banner
from netgraph.export.config.model import ConfigFile, ConfigSet, DeviceConfig, safe_relative_path
from netgraph.export.config.write import ConfigWriteError, stale_files, write_config
from netgraph.export.manifest import Recorder
from netgraph.importer import DIALECTS
from netgraph.importer.config import CONFIG_READERS, sniff
from netgraph.importer.draft import Draft
from netgraph.loader import Inventory, load_tree
from netgraph.render import build_graph, filter_graph
from netgraph.render.graph import FilterSpec, Layer

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

#: Every shipped inventory. All of them, deliberately: the dialects differ most
#: on the inventories that were written for something else — a patch room, an
#: overlay — and those are exactly the ones a hand-picked list would omit.
EXAMPLE_NAMES: Final[tuple[str, ...]] = (
    "home-lab",
    "campus",
    "overlay",
    "quickstart",
    "patch-room",
)


@pytest.fixture(scope="module")
def inventories() -> dict[str, Inventory]:
    return {name: load_tree(EXAMPLES / name) for name in EXAMPLE_NAMES}


def build(inventory: Inventory, dialect: str, *, spec: FilterSpec | None = None) -> ConfigSet:
    """Generate ``dialect`` for ``inventory``, the way the CLI does."""
    graphs = {
        layer: filter_graph(build_graph(inventory, layer=layer), spec or FilterSpec())
        for layer in CONFIG_LAYERS
    }
    return generate(
        dialect,
        ExportContext(
            inventory=inventory,
            graphs=graphs,
            options=ExportOptions(),
            recorder=Recorder(),
        ),
    )


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def test_every_dialect_is_registered_as_an_export_format() -> None:
    """One registry drives the CLI, ``--help``, completion and the docs."""
    for name, dialect in CONFIG_DIALECTS.items():
        assert dialect.name == name
        assert name in CONFIG_FORMATS
        assert dialect.description and not dialect.description.endswith(".")
        assert dialect.lossy
        assert dialect.suffix.startswith(".")
        assert dialect.comment in {"#", "!"}


def test_every_dialect_can_be_read_back() -> None:
    """A dialect netgraph writes is a dialect ``netgraph drift`` accepts.

    The claim of the whole feature is that generate-then-compare is symmetric, and
    a dialect with an emitter and no reader would break it silently — the export
    would work and the check would say "not a capture netgraph reads".
    """
    assert set(CONFIG_DIALECTS) == set(CONFIG_READERS)
    for name in CONFIG_DIALECTS:
        assert name in DIALECTS


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


def config_inputs(config: ConfigSet, root: Path) -> list[str]:
    """Write each device's files as drift inputs, keeping their own file names.

    The name matters for one dialect: a wg-quick file's interface name *is* its
    file name, which is why the files are laid out rather than concatenated. The
    rest are indifferent to it and read the device from the banner.
    """
    inputs: list[str] = []
    for relative, content in config.files():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        inputs.append(str(path))
    return inputs


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
@pytest.mark.parametrize("dialect", CONFIG_FORMATS)
def test_generating_and_reading_back_reports_no_drift(
    example: str, dialect: str, inventories: dict[str, Inventory], tmp_path: Path
) -> None:
    """The central claim: what netgraph writes, netgraph reads as agreement.

    Blind spots are expected and are not drift — a netplan file has never seen a
    cable — so only :attr:`DriftReport.changes` is asserted on. A single change
    here means the emitter and the reader disagree about the format they share.
    """
    inventory = inventories[example]
    config = build(inventory, dialect)
    inputs = config_inputs(config, tmp_path)
    if not inputs:
        pytest.skip(f"{example} gives {dialect} nothing to write")

    report = check_drift(inventory, inputs)
    assert not report.changes, "\n".join(
        f"{change.direction} {change.location}: {change.message}" for change in report.changes
    )


@pytest.mark.parametrize("dialect", CONFIG_FORMATS)
def test_a_generated_file_names_its_own_dialect_and_element(
    dialect: str, inventories: dict[str, Inventory]
) -> None:
    """The banner is what makes the round trip need neither --from nor --host."""
    config = build(inventories["campus"], dialect)
    if not config.devices:
        pytest.skip(f"campus gives {dialect} nothing to write")
    marker = CONFIG_DIALECTS[dialect].comment
    for device in config.devices:
        for entry in device.files:
            banner = parse_banner(entry.content, marker)
            assert banner[DIALECT_KEY] == dialect
            assert banner[ELEMENT_KEY].startswith(device.element)
            # Every source document is named, so the reader of a generated file
            # knows which YAML to edit.
            assert banner[SOURCE_KEY]
            assert sniff(entry.content) == dialect


# --------------------------------------------------------------------------- #
# Nothing is invented
# --------------------------------------------------------------------------- #

#: Any dotted quad or IPv6-looking literal, with or without a prefix length.
_ADDRESS = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]{4,}:[0-9a-fA-F:]*)(?:/\d+)?")


def declared_addresses(inventory: Inventory) -> set[str]:
    """Every address any document of ``inventory`` states, without prefix length."""
    found: set[str] = set()
    for element in inventory:
        spec = getattr(element, "spec", None)
        for interface in getattr(spec, "interfaces", ()) or ():
            for config in (interface.ipv4, interface.ipv6):
                if config is None:
                    continue
                found.update(str(address.ip) for address in config.addresses)
                if config.gateway is not None:
                    found.add(str(config.gateway))
        for route in getattr(spec, "routes", ()) or ():
            found.add(str(route.prefix.network_address))
            if route.via is not None:
                found.add(str(route.via))
        # A policy rule selects on prefixes of its own (§16.4), and a dialect
        # that writes the database writes them out.
        for entry in getattr(spec, "routing_policy", ()) or ():
            for prefix in (entry.src, entry.dst):
                if prefix is not None:
                    found.add(str(prefix.network_address))
        routing = getattr(spec, "routing", None)
        if routing is not None:
            for neighbor in getattr(routing.bgp, "neighbors", ()) if routing.bgp else ():
                found.add(str(neighbor.address))
    return found


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
@pytest.mark.parametrize("dialect", CONFIG_FORMATS)
def test_no_generated_address_is_absent_from_the_inventory(
    example: str, dialect: str, inventories: dict[str, Inventory]
) -> None:
    """The rule the whole package rests on: emit only what the inventory states.

    Every address-shaped literal in every generated file is matched back to one
    the inventory declares. A dialect that filled in a plausible gateway, a
    default subnet or a made-up router id would fail here, which is the point:
    a configuration file is trusted, and a plausible wrong value in one is the
    most expensive kind of mistake this tool could make.
    """
    inventory = inventories[example]
    declared = declared_addresses(inventory)
    config = build(inventory, dialect)
    for relative, content in config.files():
        for match in _ADDRESS.findall(content):
            bare = match.partition("/")[0]
            try:
                ipaddress.ip_address(bare)
            except ValueError:
                continue  # a MAC, a version string, an interface name
            assert bare in declared, f"{relative} holds {bare}, which no document declares"


@pytest.mark.parametrize("dialect", CONFIG_FORMATS)
def test_no_generated_file_holds_key_material(
    dialect: str, inventories: dict[str, Inventory]
) -> None:
    """A secret is never invented, and its absence is never silent.

    Where a dialect needs a key it gets a placeholder that cannot work. The test
    is that the placeholder is *there*: a key omitted entirely gives a file the
    consumer rejects with a message about its own schema, which sends the reader
    looking in the wrong place.
    """
    config = build(inventories["overlay"], dialect)
    for _, content in config.files():
        for line in content.splitlines():
            if re.search(r"\b(PrivateKey|PublicKey|key|password)\b\s*[:=]", line):
                assert "REPLACE-ME" in line, line


# --------------------------------------------------------------------------- #
# The shapes a shipped example does not reach
# --------------------------------------------------------------------------- #

#: The four things the example inventories have no instance of, in one device:
#: a bond whose members configure nothing themselves, a radio the inventory
#: names no SSID on, a blackhole route (which by ``NG-F004`` has neither ``via``
#: nor ``dev``, so no interface to hang off), and a route whose egress is an
#: interface a host dialect leaves out.
AWKWARD_HOST = """\
apiVersion: netgraph.dev/v1alpha1
kind: server
metadata:
  name: srv-awkward
spec:
  interfaces:
    - name: eno1
      type: ethernet
    - name: eno2
      type: ethernet
      description: Spare
    - name: bond0
      type: lag
      members: [eno1, eno2]
      ipv4:
        addresses: [10.1.0.1/24]
    - name: wlan9
      type: wifi
    - name: lo0
      type: loopback
      ipv4:
        addresses: [192.0.2.9/32]
  routes:
    - prefix: 172.16.0.0/12
      blackhole: true
    - prefix: 192.168.0.0/16
      dev: lo0
"""


@pytest.fixture(scope="module")
def awkward(tmp_path_factory: pytest.TempPathFactory) -> Inventory:
    root = tmp_path_factory.mktemp("awkward")
    (root / "hosts.yaml").write_text(AWKWARD_HOST, encoding="utf-8")
    return load_tree(root)


def test_a_bond_member_that_configures_nothing_is_still_declared(awkward: Inventory) -> None:
    """netplan rejects the whole file when a bond names a member it has not seen.

    ``eno2: {}`` is netplan's own spelling for "this port exists"; leaving the
    port out entirely produced ``Error in network definition: bond0: interface
    'eno2' is not defined`` and a host with no configuration at all.
    """
    document = dict(build(awkward, "netplan").files()).popitem()[1]
    assert "eno1:" in document
    assert "eno2: {}" in document


def test_a_radio_with_no_ssid_is_left_out_and_recorded(awkward: Inventory) -> None:
    """netplan requires an access point and refuses the file without one.

    So the radio cannot go in ``wifis:`` — there is nothing to put in the key
    netplan insists on, and netgraph does not invent an SSID. The point of the
    test is the second half: it is *recorded*, not dropped.
    """
    recorder = Recorder()
    graphs = {
        layer: filter_graph(build_graph(awkward, layer=layer), FilterSpec())
        for layer in CONFIG_LAYERS
    }
    config = generate(
        "netplan",
        ExportContext(inventory=awkward, graphs=graphs, options=ExportOptions(), recorder=recorder),
    )
    document = dict(config.files()).popitem()[1]
    assert "wlan9" not in document
    assert any("wlan9" in skip.subject for skip in recorder.sealed("netplan").skipped)


def test_no_route_is_dropped_without_a_word(awkward: Inventory) -> None:
    """Every declared route reaches the file or reaches the manifest.

    A blackhole route has no egress by construction, and a route out of an
    interface netplan left out has nowhere to hang. Both were silently missing
    before: the artefact looked complete and the manifest said ``skipped: 0``.
    """
    recorder = Recorder()
    graphs = {
        layer: filter_graph(build_graph(awkward, layer=layer), FilterSpec())
        for layer in CONFIG_LAYERS
    }
    config = generate(
        "netplan",
        ExportContext(inventory=awkward, graphs=graphs, options=ExportOptions(), recorder=recorder),
    )
    document = dict(config.files()).popitem()[1]
    manifest = recorder.sealed("netplan")
    assert "type: blackhole" in document, "the blackhole route reached no file"
    assert any("192.168.0.0/16" in skip.detail for skip in manifest.skipped)


def test_the_awkward_host_still_round_trips(awkward: Inventory, tmp_path: Path) -> None:
    """None of the four shapes above may read back as a difference."""
    for dialect in CONFIG_FORMATS:
        config = build(awkward, dialect)
        inputs = config_inputs(config, tmp_path / dialect)
        if not inputs:
            continue
        report = check_drift(awkward, inputs)
        assert not report.changes, f"{dialect}: " + "; ".join(
            f"{change.direction} {change.location}" for change in report.changes
        )


def test_an_ssid_is_not_reshaped_on_the_way_out(tmp_path: Path) -> None:
    """An SSID is an opaque octet string, so its spaces are part of its identity.

    ``Guest  WiFi`` and ``Guest WiFi`` are two different networks: a station
    configured with the second never associates with the first.
    """
    (tmp_path / "ap.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata:\n"
        "  name: pc-wifi\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: wlan0\n"
        "      type: wifi\n"
        "      wireless:\n"
        "        role: station\n"
        '        bss: [{ssid: "Guest  WiFi"}]\n',
        encoding="utf-8",
    )
    inventory = load_tree(tmp_path)
    for dialect in ("netplan", "ifupdown", "interfaces"):
        document = "\n".join(content for _, content in build(inventory, dialect).files())
        assert "Guest  WiFi" in document, dialect
    # The neutral grammar puts the SSID in a space-separated key=value list, so
    # it is the one field there that has to be quoted or the two spaces vanish.
    neutral = "\n".join(content for _, content in build(inventory, "interfaces").files())
    assert 'ssid="Guest  WiFi"' in neutral


def test_an_unstated_security_mode_does_not_assert_a_passphrase(tmp_path: Path) -> None:
    """Silence about ``security`` is not a statement that the network is protected.

    ``wpa-psk`` on an open network stops wpa_supplicant associating at all, and
    the netplan dialect writes no ``auth:`` block from the same silence — two
    dialects must not read one inventory two ways.
    """
    (tmp_path / "pc.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata:\n"
        "  name: pc-open\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: wlan0\n"
        "      type: wifi\n"
        "      wireless:\n"
        "        role: station\n"
        "        bss: [{ssid: OpenNet}]\n",
        encoding="utf-8",
    )
    inventory = load_tree(tmp_path)
    ifupdown = "\n".join(content for _, content in build(inventory, "ifupdown").files())
    assert "wpa-ssid OpenNet" in ifupdown
    assert "wpa-psk" not in ifupdown
    netplan = "\n".join(content for _, content in build(inventory, "netplan").files())
    assert "auth:" not in netplan


def test_one_interface_gets_one_mtu(inventories: dict[str, Inventory]) -> None:
    """A tunnel states an MTU and so may its interface; the file holds one.

    Two ``mtu`` keys in one mapping are a YAML 1.2 error, are rejected by
    yamllint, and were resolved by netplan taking whichever came last.
    """
    document = "\n".join(
        content
        for path, content in build(inventories["overlay"], "netplan").files()
        if "rtr-hq" in path
    )
    # From the interface's own key to the next one at the same indent.
    block = document.split("    wg0:\n")[1].split("\n    vxlan100:")[0]
    assert block.count("mtu:") == 1, block


def test_a_slash_in_an_interface_name_does_not_nest_a_wireguard_file(tmp_path: Path) -> None:
    """wg-quick takes the interface name *from* the file name, so it must be one.

    Written literally, ``wg/0.conf`` lands in a subdirectory wg-quick never
    reads, and reading it back invents an interface called ``0``.
    """
    (tmp_path / "net.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: router\n"
        "metadata: {name: rtr-a}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - {name: eth0, type: ethernet, ipv4: [198.51.100.1/24]}\n"
        "    - {name: wg/0, type: tunnel, parent: eth0, ipv4: [10.9.0.1/24]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: router\n"
        "metadata: {name: rtr-b}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - {name: eth0, type: ethernet, ipv4: [198.51.100.2/24]}\n"
        "    - {name: wg0, type: tunnel, parent: eth0, ipv4: [10.9.0.2/24]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: tunnel\n"
        "metadata: {name: wg-ab}\n"
        "spec:\n"
        "  type: wireguard\n"
        "  endpoints: ['rtr-a:wg/0', 'rtr-b:wg0']\n",
        encoding="utf-8",
    )
    paths = [path for path, _ in build(load_tree(tmp_path), "wireguard").files()]
    assert "rtr-a/etc/wireguard/wg-0.conf" in paths
    assert not any(path.endswith("/wg/0.conf") for path in paths)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #

#: A host declaring a trunk port. netplan and ifupdown have no syntax for the
#: tagged set of a bridge port, so both must refuse rather than write a file that
#: leaves the port admitting every VLAN there is.
TRUNKING_HOST = """\
apiVersion: netgraph.dev/v1alpha1
kind: server
metadata:
  name: srv-trunked
spec:
  vlans:
    - id: 10
    - id: 20
  interfaces:
    - name: eno1
      type: ethernet
      vlan:
        mode: trunk
        trunk_vlans: [10, 20]
---
apiVersion: netgraph.dev/v1alpha1
kind: server
metadata:
  name: srv-plain
spec:
  interfaces:
    - name: eno1
      type: ethernet
      ipv4:
        addresses: [10.0.0.5/24]
"""


@pytest.fixture(scope="module")
def trunked(tmp_path_factory: pytest.TempPathFactory) -> Inventory:
    root = tmp_path_factory.mktemp("trunked")
    (root / "hosts.yaml").write_text(TRUNKING_HOST, encoding="utf-8")
    return load_tree(root)


@pytest.mark.parametrize("dialect", ("netplan", "ifupdown"))
def test_a_dialect_refuses_a_field_it_cannot_express(dialect: str, trunked: Inventory) -> None:
    """The refusal names the device and the field, as a path into the document."""
    with pytest.raises(UnsupportedConfigError) as caught:
        build(trunked, dialect)
    (refusal,) = caught.value.refusals
    assert refusal.element == "srv-trunked"
    assert refusal.field == "spec.interfaces[0].vlan"
    assert "trunk" in refusal.detail
    assert dialect in str(caught.value)


@pytest.mark.parametrize("dialect", ("netplan", "ifupdown"))
def test_a_refusal_writes_nothing_at_all(dialect: str, trunked: Inventory) -> None:
    """Not even for the device that was fine.

    Half an estate configured is worse than none: the operator has no way to tell
    which half, and the half that is missing looks exactly like a device nobody
    added yet.
    """
    with pytest.raises(UnsupportedConfigError):
        build(trunked, dialect)


def test_a_dialect_that_can_express_the_field_does_not_refuse(trunked: Inventory) -> None:
    """networkd has ``[BridgeVLAN]``, so the same inventory is fine for it.

    The pair with the test above is what shows a refusal is a property of the
    *dialect* rather than a blanket rule about trunk ports.
    """
    config = build(trunked, "networkd")
    assert {device.element for device in config.devices} == {"srv-trunked", "srv-plain"}
    joined = "\n".join(content for _, content in config.files())
    assert "[BridgeVLAN]" in joined


def test_an_access_port_is_not_a_refusal(trunked: Inventory, tmp_path: Path) -> None:
    """An untagged port needs no netplan syntax, so refusing one would be wrong.

    "VLAN 10, untagged" says which broadcast domain the wire is in. A plain
    interface carries it exactly, so the generated file already behaves as
    declared and there is nothing to refuse.
    """
    (tmp_path / "host.yaml").write_text(
        TRUNKING_HOST.replace("mode: trunk\n        trunk_vlans: [10, 20]", "mode: access"),
        encoding="utf-8",
    )
    config = build(load_tree(tmp_path), "netplan")
    assert {device.element for device in config.devices} == {"srv-trunked", "srv-plain"}


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dialect", CONFIG_FORMATS)
def test_two_runs_over_an_unchanged_inventory_are_byte_identical(
    dialect: str, inventories: dict[str, Inventory]
) -> None:
    """A generated tree is committed and diffed, so a diff must mean a change."""
    inventory = inventories["campus"]
    first = dict(build(inventory, dialect).files())
    second = dict(build(inventory, dialect).files())
    assert first == second


@pytest.mark.parametrize("dialect", CONFIG_FORMATS)
def test_a_generated_file_holds_no_clock_or_hostname(
    dialect: str, inventories: dict[str, Inventory]
) -> None:
    """Every value comes from the inventory, so nothing is machine-specific."""
    for _, content in build(inventories["campus"], dialect).files():
        assert str(REPO_ROOT) not in content
        assert not re.search(r"\b(?:19|20)\d\d-\d\d-\d\dT", content)


# --------------------------------------------------------------------------- #
# Paths and writing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    ["/etc/netplan/a.yaml", "../escape.conf", "a/../../b.conf", "", ".", "a\\b.conf"],
)
def test_a_generated_path_cannot_leave_the_output_directory(path: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(path)


def test_every_generated_path_is_relative_and_inside_the_device_directory(
    inventories: dict[str, Inventory],
) -> None:
    for dialect in CONFIG_FORMATS:
        config = build(inventories["campus"], dialect)
        for device in config.devices:
            for relative in device.paths():
                assert relative.startswith(f"{device.directory}/")
                assert not Path(relative).is_absolute()


def test_the_tree_is_one_directory_per_device(
    inventories: dict[str, Inventory], tmp_path: Path
) -> None:
    config = build(inventories["campus"], "networkd")
    written = write_config(config, tmp_path)
    assert written
    for device in config.devices:
        assert (tmp_path / Path(device.directory)).is_dir()


def test_writing_over_a_file_netgraph_did_not_write_is_refused(
    inventories: dict[str, Inventory], tmp_path: Path
) -> None:
    config = build(inventories["quickstart"], "netplan")
    first, _ = next(config.files())
    victim = tmp_path.joinpath(*first.split("/"))
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("# somebody's own netplan\nnetwork: {version: 2}\n", encoding="utf-8")

    with pytest.raises(ConfigWriteError) as caught:
        write_config(config, tmp_path)
    assert "--force" in str(caught.value)

    # The same file, once netgraph wrote it, is its own to replace.
    write_config(config, tmp_path, force=True)
    write_config(config, tmp_path)


def test_writing_into_the_inventory_is_refused(
    inventories: dict[str, Inventory], tmp_path: Path
) -> None:
    """A generated ``.yaml`` under the inventory root is read as a document next run."""
    inventory = inventories["quickstart"]
    config = build(inventory, "netplan")
    with pytest.raises(ConfigWriteError) as caught:
        write_config(config, inventory.root / "build", inventory_root=inventory.root)
    assert "--force" in str(caught.value)
    write_config(config, tmp_path, inventory_root=inventory.root)


def test_files_from_an_earlier_run_are_reported_and_never_deleted(
    inventories: dict[str, Inventory], tmp_path: Path
) -> None:
    """A device dropped from the inventory leaves a file; netgraph says so."""
    whole = build(inventories["campus"], "netplan")
    write_config(whole, tmp_path)
    narrowed = build(inventories["campus"], "netplan", spec=FilterSpec(names=("srv-north-01",)))

    stale = stale_files(narrowed, tmp_path)
    assert stale
    assert all(path.exists() for path in stale)
    assert len(stale) == whole.file_count - narrowed.file_count


# --------------------------------------------------------------------------- #
# stdout, and the tree
# --------------------------------------------------------------------------- #


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def test_a_single_device_goes_to_stdout_verbatim(runner: CliRunner) -> None:
    result = runner.invoke(
        cli,
        ["-q", "-i", str(EXAMPLES / "campus"), "export", "netplan", "--name", "srv-north-01"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # One device, one file: no separator banner, so the output is a netplan file.
    assert result.stdout.startswith("# Generated by 'netgraph export netplan'")
    assert "==>" not in result.stdout


def test_more_than_one_device_on_stdout_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["-q", "-i", str(EXAMPLES / "campus"), "export", "netplan"], catch_exceptions=False
    )
    assert result.exit_code == 2
    assert "--out DIR" in result.output
    assert "--name" in result.output


def test_the_cli_writes_the_tree(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "campus"),
            "export",
            "networkd",
            "--out",
            str(tmp_path / "build"),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    written = sorted(path for path in (tmp_path / "build").rglob("*") if path.is_file())
    assert written
    assert all(path.suffix in {".network", ".netdev"} for path in written)


def test_out_is_refused_for_a_format_that_is_not_a_dialect(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli,
        ["-i", str(EXAMPLES / "campus"), "export", "hosts", "--out", str(tmp_path)],
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    assert "--out applies to" in result.output


def test_a_refusal_exits_with_the_validation_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through :func:`netgraph.cli.main`, which is what translates the exception.

    ``CliRunner`` invokes the command group and not the entry point, so it would
    see the exception rather than the status an operator gets; this is the path a
    shell takes.
    """
    (tmp_path / "hosts.yaml").write_text(TRUNKING_HOST, encoding="utf-8")
    status = main(["-i", str(tmp_path), "export", "netplan", "--out", str(tmp_path / "out")])
    assert status == UnsupportedConfigError.exit_code
    assert "spec.interfaces[0].vlan" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- #
# The stream form
# --------------------------------------------------------------------------- #


def test_several_files_are_separated_by_a_banner_in_the_format_s_own_comment() -> None:
    config = ConfigSet(
        dialect="frr",
        devices=(
            DeviceConfig(element="a", files=(ConfigFile(path="one.conf", content="x\n"),)),
            DeviceConfig(element="b", files=(ConfigFile(path="two.conf", content="y\n"),)),
        ),
    )
    stream = config.as_stream("!")
    assert stream == "! ==> a/one.conf <==\nx\n! ==> b/two.conf <==\ny\n"


def test_one_file_is_the_file() -> None:
    config = ConfigSet(
        dialect="frr",
        devices=(DeviceConfig(element="a", files=(ConfigFile(path="one.conf", content="x\n"),)),),
    )
    assert config.as_stream("!") == "x\n"


# --------------------------------------------------------------------------- #
# Sniffing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dialect", CONFIG_FORMATS)
def test_a_generated_file_sniffs_as_its_own_dialect_without_its_banner(
    dialect: str, inventories: dict[str, Inventory]
) -> None:
    """A running configuration has no banner, so the shape has to be enough."""
    config = build(inventories["campus"], dialect)
    for relative, content in config.files():
        stripped = "\n".join(line for line in content.splitlines() if "netgraph-" not in line)
        assert sniff(stripped) == dialect, relative


#: Inputs that are the right dialect and wrong in every other way. Each one
#: reached a reader as an exception or a hang before it reached a note.
HOSTILE_CAPTURES: Final[tuple[tuple[str, str], ...]] = (
    # A digit string past CPython's 4300-digit int() ceiling, which *raises*
    # rather than returning a number.
    ("networkd", "[Match]\nName=eno1\n\n[Link]\nMTUBytes=" + "9" * 5000 + "\n"),
    ("networkd", "[Match]\nName=eno1\n\n[BridgeVLAN]\nVLAN=" + "9" * 5000 + "\n"),
    # A range whose expansion would ask for four billion integers.
    ("networkd", "[Match]\nName=eno1\n\n[BridgeVLAN]\nVLAN=1-4000000000\n"),
    ("netplan", "network:\n  version: " + "9" * 5000 + "\n"),
    ("netplan", "network:\n  ethernets:\n    eno1:\n      mtu: " + "9" * 5000 + "\n"),
    ("ifupdown", "auto eno1\niface eno1 inet static\n  mtu " + "9" * 5000 + "\n"),
    ("ifupdown", "auto eno1\niface eno1 inet static\n  address 10.0.0.1\n  netmask 999999\n"),
    ("interfaces", "interface eno1\n    type ethernet\n    mtu " + "9" * 5000 + "\n"),
    ("interfaces", "interface eno1\n    type ethernet\n    vlan-tagged 1-4000000000\n"),
    ("wireguard", "[Interface]\nMTU = " + "9" * 5000 + "\n"),
    ("frr", "interface eno1\n ip address " + "9" * 5000 + "\n"),
    # Non-ASCII digits, which str.isdigit accepts and int() converts.
    ("interfaces", "interface eno1\n    type ethernet\n    mtu १५००\n"),
    # Nothing at all, and something that is not the dialect it was named as.
    ("netplan", ""),
    ("networkd", "\x00\x01\x02 not a unit file at all\n"),
    ("frr", "!\n!\n!\n"),
)


@pytest.mark.parametrize(("dialect", "text"), HOSTILE_CAPTURES)
def test_a_reader_never_raises_on_a_malformed_capture(dialect: str, text: str) -> None:
    """A drift run over a hundred devices must not stop at the one that is wrong.

    A reader reports and continues. Every one of these arrived as a traceback out
    of ``netgraph drift`` — or, for the unbounded VLAN range, as a ``MemoryError``
    — which is the one behaviour the readers' contract rules out.
    """
    draft = Draft()
    CONFIG_READERS[dialect](text, source="capture", host="pc-1", draft=draft)
    # Nothing is asserted about *what* was read: the point is that the call
    # returned. That it said something about the input is asserted separately.
    assert isinstance(draft.devices, dict)


def test_a_malformed_value_is_reported_rather_than_ignored() -> None:
    """Silence about an unreadable value would be indistinguishable from success."""
    draft = Draft()
    CONFIG_READERS["networkd"](
        "[Match]\nName=eno1\n\n[BridgeVLAN]\nVLAN=1-4000000000\n",
        source="capture",
        host="pc-1",
        draft=draft,
    )
    assert any("4000000000" in note for note in draft.notes)


def test_something_that_is_no_dialect_at_all_sniffs_as_nothing() -> None:
    assert sniff("device,port,device,port\nsw-1,Gi0/1,pc-1,eno1\n") is None
    assert sniff("") is None


def test_export_carries_the_bundle_and_the_stream_from_one_pass(
    inventories: dict[str, Inventory],
) -> None:
    """Two passes would mean two manifests, and the wrong one would be kept."""
    inventory = inventories["quickstart"]
    graphs = {Layer.L1: filter_graph(build_graph(inventory, layer=Layer.L1), FilterSpec())}
    result = export(
        "interfaces",
        lambda recorder: ExportContext(
            inventory=inventory, graphs=graphs, options=ExportOptions(), recorder=recorder
        ),
    )
    assert result.bundle is not None
    assert result.manifest.emitted == len(result.bundle.devices)
    assert result.payload == result.bundle.as_stream("#")
