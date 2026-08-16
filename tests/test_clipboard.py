"""Copy, cut, paste and duplicate: what a copy of an element actually is.

Four claims are asserted here and nowhere else, because each of them is a way a
plausible implementation gets it wrong:

**A copy gets a free name, in a series.** ``sw1`` → ``sw1-copy`` → ``sw1-copy-2``
— and copying a copy re-joins the series rather than nesting it. A tool that
produces ``sw1-copy-copy`` has stopped thinking.

**A copy drops what two elements cannot share.** A second document carrying the
first one's MAC address *loads*, and is wrong; the inventory only says so at
``validate`` time, on a line nobody typed. So the fields go on the way out, per
the table in :data:`~netviz.edit.clipboard.UNIQUE_FIELDS`, and everything else
— vendor, model, VLANs, comments — comes across.

**A copied set keeps its internal shape.** Both ends of a cable in the selection
means the cable is cloned and rewired to the clones. One end in it means the
cable is dropped *and named*: a cable joining a clone to an original is a claim
about the network nobody made, and silently making it is worse than not copying
it.

**A fragment survives leaving the process.** The clipboard payload is JSON, and
pasting it back produces the same elements — which is what makes copy-between-
windows work, and what the round-trip test over a whole site checks.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from netviz.edit import (
    UNIQUE_FIELDS,
    Batch,
    CopyElement,
    EditError,
    EditSession,
    OperationError,
    clipboard_payload,
    command_for,
    copy_plan,
    dedupe_name,
    paste_plan,
    strip_unique,
)
from netviz.edit.clipboard import CLIPBOARD_FORMAT
from netviz.edit.references import NameIndex
from netviz.loader import load_tree
from netviz.loader.inventory import namespace_of
from netviz.validate import validate

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
EXAMPLES: Final = REPO_ROOT / "examples"
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A writable copy of ``examples/home-lab``."""
    root = tmp_path / "home-lab"
    shutil.copytree(EXAMPLES / "home-lab", root)
    return root


@pytest.fixture()
def arranged(tmp_path: Path) -> Path:
    """A small tree that has a ``kind: layout`` document, so geometry is testable."""
    root = tmp_path / "drawio"
    shutil.copytree(FIXTURES / "drawio" / "inventory", root)
    return root


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    """A writable copy of ``tests/fixtures/site``: one site, and one thing outside it."""
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root)
    return root


def run(root: Path, plan: Any) -> EditSession:
    """Apply a plan against ``root`` and write it, as the CLI and the editor do."""
    session = EditSession(root=root)
    batch = Batch(session, label=plan.describe())
    batch.apply(plan.operations)
    batch.commit()
    return session


def document(root: Path, address: str) -> dict[str, Any]:
    """The raw document declaring ``address``, read back off the disk."""
    inventory = load_tree(root)
    source = inventory.sources[address]
    assert source.relative is not None
    text = (root / source.relative).read_text(encoding="utf-8")
    return list(yaml.safe_load_all(text))[source.index]


# --------------------------------------------------------------------------- #
# The name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("base", "taken", "expected"),
    [
        ("sw1", (), "sw1-copy"),
        ("sw1", ("sw1-copy",), "sw1-copy-2"),
        ("sw1", ("sw1-copy", "sw1-copy-2"), "sw1-copy-3"),
        # A copy of a copy re-joins the series rather than nesting it.
        ("sw1-copy", ("sw1", "sw1-copy"), "sw1-copy-2"),
        ("sw1-copy-2", ("sw1", "sw1-copy", "sw1-copy-2"), "sw1-copy-3"),
        # A gap in the middle is filled: the ladder looks for a free rung, not
        # for the highest one.
        ("sw1", ("sw1-copy", "sw1-copy-3"), "sw1-copy-2"),
    ],
)
def test_a_derived_name_climbs_a_ladder(base: str, taken: tuple[str, ...], expected: str) -> None:
    assert dedupe_name(base, taken) == expected


def test_the_suffix_is_configurable() -> None:
    assert dedupe_name("sw1", ()) == "sw1-copy"
    assert dedupe_name("sw1", (), suffix="b") == "sw1-b"
    assert dedupe_name("sw1-b", ("sw1-b",), suffix="b") == "sw1-b-2"


def test_a_suffix_that_would_not_make_a_name_is_refused() -> None:
    """§4.1 is the grammar; a suffix that breaks it must not reach the schema."""
    with pytest.raises(OperationError, match="not a usable copy suffix"):
        dedupe_name("sw1", (), suffix="a b")
    with pytest.raises(OperationError, match="not a usable copy suffix"):
        dedupe_name("sw1", (), suffix="")


# --------------------------------------------------------------------------- #
# The fields a copy cannot keep
# --------------------------------------------------------------------------- #


def test_every_unique_field_is_stripped_from_a_document() -> None:
    """One document carrying every row of the table, and none of them survives."""
    source: dict[str, Any] = {
        "metadata": {"name": "sw1", "location": {"rack": "r1", "position": 12, "height": 1}},
        "spec": {
            "vendor": "Cisco",
            "serial": "FOC1234X5YZ",
            "label": "D-001",
            "bridge": {"name": "br0", "address": "00:11:22:33:44:55"},
            "interfaces": [
                {
                    "name": "eth0",
                    "type": "ethernet",
                    "mtu": 1500,
                    "mac": "00:11:22:33:44:56",
                    "ipv4": {"addresses": ["10.0.0.1/24"], "gateway": "10.0.0.254"},
                    "ipv6": {"addresses": ["2001:db8::1/64"]},
                },
                {
                    "name": "wlan0",
                    "type": "wifi",
                    "wireless": {
                        "role": "ap",
                        "bss": [{"ssid": "lab", "bssid": "00:11:22:33:44:57"}],
                    },
                },
            ],
            # Not a shape any one kind has -- the identity fields belong to a
            # user or a group -- because the table is read against a *document*
            # and the point here is that every row of it fires.
            "login": "ana",
            "uid": 1000,
            "gid": 100,
            "power": {"inputs": [{"pdu": "pdu1", "outlet": "3"}], "redundant": False},
            "routing": {
                "ospf": {"router_id": "10.0.0.1", "interfaces": ["eth0"]},
                "bgp": {"asn": 65000, "router_id": "10.0.0.1"},
            },
        },
    }
    removed = strip_unique(source)

    assert set(removed) == {field.spelling for field in UNIQUE_FIELDS}
    # What must be gone.
    assert "position" not in source["metadata"]["location"]
    assert "serial" not in source["spec"]
    assert "label" not in source["spec"]
    assert "address" not in source["spec"]["bridge"]
    assert "mac" not in source["spec"]["interfaces"][0]
    assert "bssid" not in source["spec"]["interfaces"][1]["wireless"]["bss"][0]
    assert "power" not in source["spec"]
    assert not {"login", "uid", "gid"} & set(source["spec"])
    assert "router_id" not in source["spec"]["routing"]["ospf"]
    assert "router_id" not in source["spec"]["routing"]["bgp"]
    # What must survive: a copy that lost these would not be a copy.
    assert source["metadata"]["location"]["rack"] == "r1"
    assert source["spec"]["vendor"] == "Cisco"
    assert source["spec"]["bridge"]["name"] == "br0"
    assert source["spec"]["interfaces"][0]["mtu"] == 1500
    assert source["spec"]["routing"]["ospf"]["interfaces"] == ["eth0"]


def test_stripping_tidies_what_it_leaves_behind() -> None:
    """A block that says nothing on its own goes with the field that gave it meaning."""
    source: dict[str, Any] = {
        "metadata": {"name": "sw1"},
        "spec": {
            "interfaces": [
                # Nothing but an address: the whole family block is what the
                # address was, so it goes rather than being left as `enabled`.
                {"name": "eth0", "type": "ethernet", "ipv4": {"addresses": ["10.0.0.1/24"]}},
                # A forwarding setting is a real decision and stays.
                {
                    "name": "eth1",
                    "type": "ethernet",
                    "ipv4": {"addresses": ["10.0.1.1/24"], "forwarding": True},
                },
            ],
            # `redundant` claims the device survives losing a feed; with the
            # feeds gone it is an assertion about nothing (NV-E002).
            "power": {"inputs": [{"pdu": "p", "outlet": "1"}], "redundant": True},
        },
    }
    strip_unique(source)
    assert "ipv4" not in source["spec"]["interfaces"][0]
    assert source["spec"]["interfaces"][1]["ipv4"] == {"forwarding": True}
    assert "power" not in source["spec"]


def test_a_copy_drops_the_unique_fields_and_keeps_everything_else(home: Path) -> None:
    """End to end: the switch's management address and bridge MAC do not travel."""
    original = document(home, "switches/sw-home")
    session = EditSession(root=home)
    run(home, copy_plan(session.inventory, ["sw-home"]))

    copy = document(home, "switches/sw-home-copy")
    assert copy["metadata"]["name"] == "sw-home-copy"
    assert copy["metadata"]["description"] == original["metadata"]["description"]
    assert copy["spec"]["model"] == original["spec"]["model"]
    assert copy["spec"]["vlans"] == original["spec"]["vlans"]
    assert "address" not in copy["spec"]["bridge"]
    management = next(one for one in copy["spec"]["interfaces"] if one["name"] == "Vlan10")
    assert "ipv4" not in management
    # And the copy is a document the tree accepts.
    assert not [finding for finding in validate(load_tree(home)) if finding.severity.is_fatal]


def test_keep_unique_leaves_the_table_alone(home: Path) -> None:
    """The escape hatch, and the honest consequence of using it."""
    session = EditSession(root=home)
    plan = copy_plan(session.inventory, ["sw-home"], keep_unique=True)
    inner = EditSession(root=home)
    Batch(inner).apply(plan.operations)

    copy = next(iter(yaml.safe_load_all(inner.changes["switches/sw-home-copy.yaml"] or "")))
    assert copy["spec"]["bridge"]["address"] == "00:22:07:aa:00:00"
    # ...and the gate says why this is not the default.
    assert [problem.rule for problem in inner.check()]


def test_a_copy_keeps_the_original_comments(home: Path) -> None:
    """The write path's whole promise, applied to a document it invents."""
    session = EditSession(root=home)
    plan = copy_plan(session.inventory, ["rtr-home"])
    inner = EditSession(root=home)
    Batch(inner).apply(plan.operations)
    written = inner.changes["routers/rtr-home-copy.yaml"] or ""
    original = (home / "routers" / "rtr-home.yaml").read_text(encoding="utf-8")

    comments = [line.strip() for line in original.splitlines() if line.strip().startswith("#")]
    assert comments, "the fixture is meant to have comments to preserve"
    for comment in comments:
        assert comment in written


# --------------------------------------------------------------------------- #
# The links
# --------------------------------------------------------------------------- #


def test_a_copied_set_rewires_its_internal_cables(arranged: Path) -> None:
    session = EditSession(root=arranged)
    plan = copy_plan(session.inventory, ["rtr-core", "sw-access", "cbl-core-access"], view="l1")
    assert plan.mapping == {
        "devices/rtr-core": "devices/rtr-core-copy",
        "devices/sw-access": "devices/sw-access-copy",
        "cables/cbl-core-access": "cables/cbl-core-access-copy",
    }
    run(arranged, plan)

    cable = document(arranged, "cables/cbl-core-access-copy")
    assert sorted(cable["spec"]["endpoints"]) == ["rtr-core-copy:lan0", "sw-access-copy:port1"]
    # The original is untouched, which is the other half of "rewired".
    assert sorted(document(arranged, "cables/cbl-core-access")["spec"]["endpoints"]) == [
        "rtr-core:lan0",
        "sw-access:port1",
    ]
    assert not [finding for finding in validate(load_tree(arranged)) if finding.severity.is_fatal]


def test_a_cable_with_one_end_outside_the_selection_is_dropped_and_named(
    arranged: Path,
) -> None:
    session = EditSession(root=arranged)
    plan = copy_plan(session.inventory, ["sw-access", "cbl-access-app"])

    assert "cables/cbl-access-app" not in plan.mapping
    assert [entry.address for entry in plan.dropped] == ["cables/cbl-access-app"]
    assert "hosts/srv-app" in plan.dropped[0].reason


def test_copying_a_cable_on_its_own_is_refused(arranged: Path) -> None:
    """Two cables on one interface is NV-C001, and the message says what to do."""
    session = EditSession(root=arranged)
    with pytest.raises(EditError, match="NV-C001"):
        session.apply(CopyElement(address="cbl-core-access"))


def test_copying_a_namespace_copies_its_subtree(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sites" / "hq" / "access").mkdir(parents=True)
    (root / "sites" / "hq" / "access" / "kit.yaml").write_text(
        _two_switches_and_a_cable(), encoding="utf-8", newline="\n"
    )
    session = EditSession(root=root)
    plan = copy_plan(session.inventory, ["sites/hq"], namespace="sites/dr")

    assert plan.mapping == {
        "sites/hq/access/sw-a": "sites/dr/access/sw-a",
        "sites/hq/access/sw-b": "sites/dr/access/sw-b",
        "sites/hq/access/cbl-a-b": "sites/dr/access/cbl-a-b",
    }
    run(root, plan)
    # The subtree keeps its own shape below the new root, names included.
    assert (root / "sites" / "dr" / "access").is_dir()
    cable = document(root, "sites/dr/access/cbl-a-b")
    assert sorted(cable["spec"]["endpoints"]) == ["sw-a:eth0", "sw-b:eth0"]
    assert not [finding for finding in validate(load_tree(root)) if finding.severity.is_fatal]


def _two_switches_and_a_cable() -> str:
    return (
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n  name: sw-a\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
        "---\n"
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n  name: sw-b\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
        "---\n"
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata:\n  name: cbl-a-b\n"
        "spec:\n  endpoints:\n    - sw-a:eth0\n    - sw-b:eth0\n  medium: copper\n"
    )


# --------------------------------------------------------------------------- #
# Names and namespaces
# --------------------------------------------------------------------------- #


def test_a_copy_into_another_namespace_keeps_its_name(home: Path) -> None:
    """ "The same switch, in the lab folder" is what copying to a folder means."""
    session = EditSession(root=home)
    plan = copy_plan(session.inventory, ["sw-home"], namespace="lab")
    assert plan.mapping == {"switches/sw-home": "lab/sw-home"}


def test_an_explicit_name_is_used_and_a_taken_one_refused(home: Path) -> None:
    session = EditSession(root=home)
    assert copy_plan(session.inventory, ["sw-home"], name="sw-hall").mapping == {
        "switches/sw-home": "switches/sw-hall"
    }
    with pytest.raises(EditError, match="already exists"):
        copy_plan(session.inventory, ["rtr-home"], namespace="switches", name="sw-home")


def test_a_name_cannot_be_given_to_more_than_one_copy(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(OperationError, match="one copy one name"):
        copy_plan(session.inventory, ["sw-home", "rtr-home"], name="both")


def test_copying_the_same_set_twice_produces_two_sets(home: Path) -> None:
    """The names allocated by the first copy count as taken by the second."""
    session = EditSession(root=home)
    run(home, copy_plan(session.inventory, ["sw-home"]))
    session = EditSession(root=home)
    run(home, copy_plan(session.inventory, ["sw-home"]))
    inventory = load_tree(home)
    assert "switches/sw-home-copy" in inventory.elements
    assert "switches/sw-home-copy-2" in inventory.elements


def test_copying_nothing_and_copying_nonsense_are_both_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="nothing was named"):
        copy_plan(session.inventory, [])
    with pytest.raises(EditError, match="no element or namespace"):
        copy_plan(session.inventory, ["not-a-thing"])


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def test_a_copy_is_placed_beside_the_original(arranged: Path) -> None:
    before = _positions(arranged)
    session = EditSession(root=arranged)
    run(arranged, copy_plan(session.inventory, ["sw-access"], view="l1"))
    after = _positions(arranged)

    assert set(before) < set(after), "the originals must keep their entries"
    for key, point in before.items():
        assert after[key] == point
    # Right and down: `y` grows upwards in netviz's coordinates.
    original = before["devices/sw-access"]
    assert after["devices/sw-access-copy"] == (original[0] + 20, original[1] - 20)


def test_a_paste_anchor_centres_the_fragment_on_the_point(arranged: Path) -> None:
    session = EditSession(root=arranged)
    payload = clipboard_payload(session.inventory, ["rtr-core", "sw-access"], view="l1")
    run(arranged, paste_plan(session.inventory, payload, view="l1", at=(1000.0, 1000.0)))

    after = _positions(arranged)
    copies = [after["devices/rtr-core-copy"], after["devices/sw-access-copy"]]
    middle = (
        (min(x for x, _ in copies) + max(x for x, _ in copies)) / 2,
        (min(y for _, y in copies) + max(y for _, y in copies)) / 2,
    )
    assert middle == (1000.0, 1000.0)


def test_no_view_writes_no_geometry(arranged: Path) -> None:
    """A scripted copy has no diagram to place into, and must not invent one."""
    before = (arranged / "layout.yaml").read_bytes()
    session = EditSession(root=arranged)
    run(arranged, copy_plan(session.inventory, ["sw-access"]))
    assert (arranged / "layout.yaml").read_bytes() == before


def _positions(root: Path, view: str = "l1") -> dict[str, tuple[float, float]]:
    document_ = yaml.safe_load((root / "layout.yaml").read_text(encoding="utf-8"))
    found: dict[str, tuple[float, float]] = {}
    for key, entry in document_["spec"]["views"][view]["nodes"].items():
        point = entry["position"]
        pair = point if isinstance(point, list) else (point["x"], point["y"])
        found[key] = (float(pair[0]), float(pair[1]))
    return found


# --------------------------------------------------------------------------- #
# The serialised clipboard
# --------------------------------------------------------------------------- #


def test_a_fragment_is_json_and_says_what_it_is(arranged: Path) -> None:
    payload = clipboard_payload(
        load_tree(arranged), ["rtr-core", "sw-access", "cbl-core-access"], view="l1"
    )
    # It has to survive a trip through the system clipboard, which is text.
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["format"] == CLIPBOARD_FORMAT
    assert [entry["address"] for entry in round_tripped["documents"]] == [
        "devices/rtr-core",
        "devices/sw-access",
        "cables/cbl-core-access",
    ]
    assert round_tripped["geometry"]["devices/rtr-core"]["position"] == {"x": 148.0, "y": 232.0}
    # The documents are documents, not a private encoding of them.
    first = round_tripped["documents"][0]["document"]
    assert first["kind"] == "router"
    assert first["metadata"]["name"] == "rtr-core"


def test_a_fragment_pastes_into_a_different_inventory(arranged: Path, home: Path) -> None:
    """The between-windows case, which is the whole reason the payload exists."""
    payload = clipboard_payload(load_tree(arranged), ["rtr-core", "sw-access", "cbl-core-access"])
    plan = paste_plan(load_tree(home), json.loads(json.dumps(payload)), namespace="imported")
    run(home, plan)

    inventory = load_tree(home)
    assert "imported/devices/rtr-core" in inventory.elements
    assert "imported/cables/cbl-core-access" in inventory.elements
    cable = document(home, "imported/cables/cbl-core-access")
    assert sorted(cable["spec"]["endpoints"]) == ["rtr-core:lan0", "sw-access:port1"]
    assert not [finding for finding in validate(inventory) if finding.severity.is_fatal]


def test_pasting_a_fragment_into_the_tree_it_came_from_renames_it(arranged: Path) -> None:
    payload = clipboard_payload(load_tree(arranged), ["rtr-core", "sw-access", "cbl-core-access"])
    plan = paste_plan(load_tree(arranged), payload)
    run(arranged, plan)

    cable = document(arranged, "cables/cbl-core-access-copy")
    assert sorted(cable["spec"]["endpoints"]) == ["rtr-core-copy:lan0", "sw-access-copy:port1"]


def test_a_clipboard_that_is_not_a_fragment_is_refused_by_name(home: Path) -> None:
    inventory = load_tree(home)
    with pytest.raises(EditError, match="not a netviz fragment"):
        paste_plan(inventory, {"format": "application/x-something", "documents": []})
    with pytest.raises(EditError, match="no 'documents' list"):
        paste_plan(inventory, {"format": CLIPBOARD_FORMAT})
    with pytest.raises(EditError, match="holds no netviz documents"):
        paste_plan(inventory, {"format": CLIPBOARD_FORMAT, "documents": []})


# --------------------------------------------------------------------------- #
# Round trip: a whole site
# --------------------------------------------------------------------------- #


def test_copying_a_whole_site_produces_a_tree_that_still_validates(site: Path) -> None:
    """The strong form: `sites/hq` becomes `sites/dr`, and the tree still loads.

    ``tests/fixtures/site`` is a site with a namespace under it, three cables,
    references written both short and qualified, and one switch deliberately
    *outside* the site. So this exercises the four things a subtree copy has to
    get right at once — the shape of the subtree, the two reference spellings,
    the boundary, and the unique fields — and then asks the two questions that
    matter: does it load, and does it introduce a problem the tree did not have.
    """
    session = EditSession(root=site)
    plan = copy_plan(session.inventory, ["sites/hq"], namespace="sites/dr")

    assert not plan.dropped, "every cable in the site has both ends in the site"
    assert plan.mapping == {
        f"sites/hq/{rest}": f"sites/dr/{rest}"
        for rest in (
            "cbl-rtr-sw",
            "cbl-sw-srv",
            "cbl-sw-rack",
            "rtr-hq",
            "sw-hq",
            "srv-hq",
            "racks/r1/sw-rack",
        )
    }
    inner = EditSession(root=site)
    Batch(inner).apply(plan.operations)
    assert inner.check() == (), "copying a site must introduce no new problem"
    inner.commit()

    after = load_tree(site)
    assert not after.errors
    # The boundary held: nothing outside the site moved or was copied.
    assert "sw-shared" in after.elements
    assert not [
        fqn for fqn in after.elements if fqn.startswith("sites/dr/") is False and "dr" in fqn
    ]

    # The subtree kept its shape, and both reference spellings still mean what
    # they meant -- now about the copies.
    assert "sites/dr/racks/r1/sw-rack" in after.elements
    assert sorted(document(site, "sites/dr/cbl-rtr-sw")["spec"]["endpoints"]) == [
        "rtr-hq:lan0",
        "sw-hq:port1",
    ]
    assert sorted(document(site, "sites/dr/cbl-sw-rack")["spec"]["endpoints"]) == [
        "racks/r1/sw-rack:uplink",
        "sw-hq:port3",
    ]
    # ...and the copies point at the copies rather than back at the originals.
    index = NameIndex(after.elements)
    for fqn, element in after.elements.items():
        if not fqn.startswith("sites/dr/") or not hasattr(element.spec, "endpoints"):
            continue
        for endpoint in element.spec.endpoints:
            reached = index.lookup(endpoint.device, namespace_of(fqn))
            assert reached is not None and reached.startswith("sites/dr/"), (
                f"{fqn} reaches {endpoint.device!r}, which is not in the copy"
            )

    # The unique fields went, and the shared ones came.
    copied = document(site, "sites/dr/rtr-hq")
    assert "serial" not in copied["spec"]
    assert copied["spec"]["model"] == "CCR2004"
    assert "position" not in document(site, "sites/dr/racks/r1/sw-rack")["metadata"]["location"]
    assert not [finding for finding in validate(after) if finding.severity.is_fatal]


# --------------------------------------------------------------------------- #
# The operation, on its own terms
# --------------------------------------------------------------------------- #


def test_a_copy_is_undone_by_deleting_it(home: Path) -> None:
    """The inverse is exact by construction: the document did not exist before."""
    before = (home / "switches" / "sw-home.yaml").read_bytes()
    session = EditSession(root=home)
    applied = session.apply(CopyElement(address="sw-home"))
    assert [operation.to_dict()["op"] for operation in applied.inverse] == ["delete"]
    session.apply_all(applied.inverse)
    assert not session.changes
    assert (home / "switches" / "sw-home.yaml").read_bytes() == before


def test_the_operation_round_trips_through_its_json_form() -> None:
    from netviz.edit import operation_from_dict

    operation = CopyElement(
        address="sites/hq/sw1",
        name="sw2",
        namespace="sites/dr",
        suffix="b",
        keep_unique=True,
        rewrite={"sites/hq/sw0": "sites/dr/sw0"},
    )
    assert operation_from_dict(operation.to_dict()) == operation


def test_the_equivalent_command_is_copy_or_duplicate() -> None:
    """What the changes drawer hands somebody who wants to replay it."""
    assert "edit duplicate sites/hq/sw1" in command_for(CopyElement(address="sites/hq/sw1"))
    line = command_for(CopyElement(address="sw1", namespace="lab", name="sw2"))
    assert "edit copy sw1 --to lab --name sw2" in line
    # A rewrite map has no flag, so it goes through apply rather than being lost.
    rewired = command_for(CopyElement(address="cbl", rewrite={"a": "b"}))
    assert "edit apply -f -" in rewired
