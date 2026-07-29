"""Patch panels and physical placement: the model, the splice, and the views.

The other modules cover these where they touch what already existed — an
invalid fixture per rule, a golden per view, the completion list. What is new
and has no home elsewhere is here:

* the ``ports`` shorthand and the coupling it derives, which is the whole of
  what a panel document says;
* **splice equivalence**, the property the whole design rests on: the graph an
  inventory produces when a run crosses two panels must be the graph the same
  inventory produces when the two devices are cabled together directly. If that
  ever stops holding, modelling a panel starts changing the network;
* the four ways a run fails to arrive, which the graph layer has to survive
  because ``--force`` exists;
* what a trace says about a panel — named, but never a hop;
* the rack elevation, and the one output format that refuses to draw it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from netgraph.errors import RenderError, SchemaError
from netgraph.loader import Inventory, load_tree
from netgraph.models import (
    API_VERSION,
    PatchPanel,
    parse_document,
    parse_port_range,
)
from netgraph.render import render_text
from netgraph.render.graph import (
    RACK_ID_PREFIX,
    Edge,
    EdgeKind,
    FilterSpec,
    Graph,
    Layer,
    NodeType,
    build_graph,
    filter_graph,
    rack_elevations,
)
from netgraph.trace import trace
from netgraph.validate import validate

# --------------------------------------------------------------------------- #
# Building inventories
# --------------------------------------------------------------------------- #

_SWITCH = """
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata: {{name: {name}}}
spec:
  interfaces:
    - {{name: p1, type: ethernet, mtu: 1500, vlan: {{mode: access, access_vlan: 10}}}}
    - {{name: p2, type: ethernet, mtu: 1500, vlan: {{mode: access, access_vlan: 10}}}}
"""

_PANEL = """
apiVersion: netgraph.dev/v1alpha1
kind: patchpanel
metadata: {{name: {name}}}
spec: {{ports: {ports}}}
"""


def switch(name: str) -> str:
    return _SWITCH.format(name=name)


def panel(name: str, ports: str = "1-4") -> str:
    return _PANEL.format(name=name, ports=ports)


def cable(name: str, left: str, right: str, **spec: str) -> str:
    extra = "".join(f"\n  {key}: {value}" for key, value in spec.items())
    return f"""
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {{name: {name}}}
spec:
  endpoints: [{left}, {right}]
  medium: copper{extra}
"""


def inventory_of(root: Path, *documents: str) -> Inventory:
    """Write one file holding ``documents`` and load the tree."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "inventory.yaml").write_text("---\n".join(documents), encoding="utf-8")
    inventory = load_tree(root)
    assert inventory.errors == [], "\n".join(str(error) for error in inventory.errors)
    return inventory


def panel_document(name: str, **spec: object) -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "kind": "patchpanel",
        "metadata": {"name": name},
        "spec": spec,
    }


def parse_panel(name: str = "pp", **spec: object) -> PatchPanel:
    element = parse_document(panel_document(name, **spec))
    assert isinstance(element, PatchPanel)
    return element


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (1, ("1",)),
        (4, ("1", "2", "3", "4")),
        ("7", ("7",)),
        ("1-3", ("1", "2", "3")),
        ("1-2,5-6", ("1", "2", "5", "6")),
        # The width of the *low* bound pads every value it produces, exactly as
        # an interface range does (§6.2.5).
        ("01-03", ("01", "02", "03")),
    ],
)
def test_a_port_range_expands_the_way_an_interface_range_does(
    written: object, expected: tuple[str, ...]
) -> None:
    assert parse_port_range(written) == expected


@pytest.mark.parametrize(
    "written",
    [
        0,
        -1,
        "",
        "front/1",
        "3-1",  # inverted
        "1-2,2-3",  # a position declared twice
        "1-99999",  # past MAX_PANEL_PORTS
        [1, 2],
    ],
)
def test_a_malformed_port_range_is_refused(written: object) -> None:
    with pytest.raises(SchemaError) as caught:
        parse_document(panel_document("pp", ports=written))
    assert any(issue.rule == "NG-P006" for issue in caught.value.issues)


def test_a_panel_derives_two_interfaces_per_position() -> None:
    element = parse_panel(ports="1-3")
    assert [interface.name for interface in element.interfaces] == [
        "front/1",
        "front/2",
        "front/3",
        "rear/1",
        "rear/2",
        "rear/3",
    ]
    # A hole with a number: no address, no VLAN, no MAC.
    for interface in element.interfaces:
        assert interface.vlan is None
        assert interface.ipv4 is None and interface.ipv6 is None
        assert interface.mac is None


def test_a_panel_couples_front_to_rear_by_default() -> None:
    element = parse_panel(ports=4)
    assert element.opposite("front/3") == "rear/3"
    assert element.opposite("rear/3") == "front/3"
    assert element.opposite("front/9") is None


def test_couplers_cross_wire_a_panel_in_both_directions() -> None:
    element = parse_panel(ports="1-4", couplers={1: 4, 4: 1})
    assert element.opposite("front/1") == "rear/4"
    assert element.opposite("rear/4") == "front/1"
    # Untouched positions keep the identity mapping.
    assert element.opposite("front/2") == "rear/2"


@pytest.mark.parametrize(
    "couplers",
    [
        {"1": "9"},  # a rear position that does not exist
        {"9": "1"},  # a front position that does not exist
        {"1": "2", "3": "2"},  # two fronts into one rear
    ],
)
def test_a_coupler_naming_a_position_the_panel_lacks_is_refused(
    couplers: dict[str, str],
) -> None:
    with pytest.raises(SchemaError) as caught:
        parse_document(panel_document("pp", ports="1-4", couplers=couplers))
    assert any(issue.rule == "NG-P007" for issue in caught.value.issues)


def test_the_normalised_ports_value_is_one_spelling_for_both_forms() -> None:
    assert parse_panel(ports=24).spec.ports == parse_panel(ports="1-24").spec.ports


# --------------------------------------------------------------------------- #
# Splice equivalence
# --------------------------------------------------------------------------- #


def _topology(graph: Graph) -> tuple[frozenset[str], frozenset[tuple[str, ...]]]:
    """The graph as the pair of facts a splice must preserve.

    Not the edges themselves: a spliced run keeps the identity of its first
    segment, which a direct cable has no reason to share. What has to match is
    *who is joined to whom, by which ports, carrying which VLANs* — which is
    the whole of what any layer above the cabling record can observe.
    """
    nodes = frozenset(graph.nodes)
    links = frozenset(
        (
            *sorted((f"{edge.source}:{edge.source_port}", f"{edge.target}:{edge.target_port}")),
            edge.medium,
            ",".join(str(vlan) for vlan in sorted(edge.vlans)),
        )
        for edge in graph.edges
    )
    return nodes, links


@pytest.mark.parametrize("layer", [Layer.L1, Layer.L2, Layer.L3])
def test_a_spliced_run_is_the_graph_of_a_direct_cable(tmp_path: Path, layer: Layer) -> None:
    """The property the whole design rests on.

    Two inventories describing the same two switches: one patches them together
    through two panels, the other runs a cable straight between them. Above
    ``--layer physical`` the two must be indistinguishable, because on the
    network they are.
    """
    patched = inventory_of(
        tmp_path / "patched",
        switch("sw-a"),
        switch("sw-b"),
        panel("pp-a"),
        panel("pp-b"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
        cable("seg-2", "pp-a:rear/1", "pp-b:rear/1"),
        cable("seg-3", "pp-b:front/1", "sw-b:p1"),
    )
    direct = inventory_of(
        tmp_path / "direct",
        switch("sw-a"),
        switch("sw-b"),
        cable("seg-1", "sw-a:p1", "sw-b:p1"),
    )
    assert _topology(build_graph(patched, layer=layer)) == _topology(
        build_graph(direct, layer=layer)
    )


def test_the_physical_layer_keeps_the_panels_and_every_segment(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        switch("sw-b"),
        panel("pp-a"),
        panel("pp-b"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
        cable("seg-2", "pp-a:rear/1", "pp-b:rear/1"),
        cable("seg-3", "pp-b:front/1", "sw-b:p1"),
    )
    physical = build_graph(inventory, layer=Layer.PHYSICAL)
    assert set(physical.nodes) == {"sw-a", "sw-b", "pp-a", "pp-b"}
    assert [edge.id for edge in physical.edges] == ["seg-1", "seg-2", "seg-3"]
    assert all(edge.patch is None for edge in physical.edges)

    spliced = build_graph(inventory, layer=Layer.L1)
    assert set(spliced.nodes) == {"sw-a", "sw-b"}
    (edge,) = spliced.edges
    assert edge.patch is not None
    assert edge.patch.segments == ("seg-1", "seg-2", "seg-3")
    assert edge.patch.panels == ("pp-a", "pp-b")
    assert [(hop.panel, hop.ingress, hop.egress) for hop in edge.patch.hops] == [
        ("pp-a", "front/1", "rear/1"),
        ("pp-b", "rear/1", "front/1"),
    ]


def test_a_spliced_run_carries_the_properties_of_the_whole_run(tmp_path: Path) -> None:
    """The rate is the slowest leg and the length is the sum of them."""
    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        switch("sw-b"),
        panel("pp-a"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1", speed="10Gbps", length_m="2", label="P-1"),
        cable("seg-2", "pp-a:rear/1", "sw-b:p1", speed="1Gbps", length_m="30"),
    )
    (edge,) = build_graph(inventory, layer=Layer.L1).edges
    assert edge.speed == 1_000_000_000
    assert edge.length_m == 32
    assert edge.label == "P-1"
    assert edge.medium == "copper"


def test_a_cross_wired_panel_splices_through_its_own_coupling(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        switch("sw-b"),
        panel_yaml := """
apiVersion: netgraph.dev/v1alpha1
kind: patchpanel
metadata: {name: pp-a}
spec:
  ports: 1-4
  couplers: {1: 4}
""",
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
        cable("seg-2", "pp-a:rear/4", "sw-b:p1"),
    )
    assert "couplers" in panel_yaml
    (edge,) = build_graph(inventory, layer=Layer.L1).edges
    assert edge.patch is not None
    assert [(hop.ingress, hop.egress) for hop in edge.patch.hops] == [("front/1", "rear/4")]


def test_an_unpatched_panel_is_still_a_node_on_the_physical_layer(tmp_path: Path) -> None:
    """A panel nobody has cabled has no segments to splice, and must still go."""
    inventory = inventory_of(tmp_path, switch("sw-a"), panel("pp-a"))
    assert "pp-a" in build_graph(inventory, layer=Layer.PHYSICAL).nodes
    assert "pp-a" not in build_graph(inventory, layer=Layer.L1).nodes


# --------------------------------------------------------------------------- #
# Runs that do not arrive
# --------------------------------------------------------------------------- #


def test_a_run_that_stops_inside_the_panel_is_dropped_and_reported(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        panel("pp-a"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
    )
    graph = build_graph(inventory, layer=Layer.L1)
    assert graph.edges == ()
    assert any("nothing is patched into" in problem for problem in graph.dangling)
    # And the segment that does exist is still visible where it exists.
    assert [edge.id for edge in build_graph(inventory, layer=Layer.PHYSICAL).edges] == ["seg-1"]


def test_a_position_cabled_twice_splices_the_first_and_reports_the_second(
    tmp_path: Path,
) -> None:
    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        switch("sw-b"),
        panel("pp-a"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
        cable("seg-2", "sw-a:p2", "pp-a:front/1"),
        cable("seg-3", "pp-a:rear/1", "sw-b:p1"),
    )
    graph = build_graph(inventory, layer=Layer.L1)
    assert any("already terminates" in problem for problem in graph.dangling)
    assert len(graph.edges) == 1


def test_a_run_that_loops_between_two_panels_is_dropped(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        panel("pp-a"),
        panel("pp-b"),
        cable("front", "pp-a:front/1", "pp-b:front/1"),
        cable("rear", "pp-a:rear/1", "pp-b:rear/1"),
    )
    graph = build_graph(inventory, layer=Layer.L1)
    assert graph.edges == ()
    # A loop with nothing active on it has no end to start a walk from, so what
    # the graph layer can say about it is that it reaches nothing. ``E024`` is
    # the rule that names it as a loop.
    assert all("reaches nothing" in problem for problem in graph.dangling)
    assert [finding.rule for finding in validate(inventory)] == ["E024"]


def test_a_segment_between_two_panels_alone_reaches_nothing(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        panel("pp-a"),
        panel("pp-b"),
        cable("tie", "pp-a:rear/1", "pp-b:rear/1"),
    )
    graph = build_graph(inventory, layer=Layer.L1)
    assert graph.edges == ()
    assert any("reaches nothing" in problem for problem in graph.dangling)


# --------------------------------------------------------------------------- #
# Tracing through a panel
# --------------------------------------------------------------------------- #


def test_a_trace_crosses_a_panel_without_making_it_a_hop(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        switch("sw-b"),
        panel("pp-a"),
        panel("pp-b"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
        cable("seg-2", "pp-a:rear/1", "pp-b:rear/1"),
        cable("seg-3", "pp-b:front/1", "sw-b:p1"),
    )
    result = trace(inventory, "sw-a", "sw-b")
    path = result.shortest
    assert path is not None
    # One hop, two waypoints: the panels are not places traffic is handled.
    assert path.hops == 1
    assert path.elements == ("sw-a", "sw-b")
    assert path.panels == ("pp-a", "pp-b")

    (link,) = path.links
    assert link.is_pass_through
    assert link.patch is not None
    assert link.patch.describe() == "pp-a front/1-rear/1, pp-b rear/1-front/1"


def test_the_text_report_names_the_panels_on_the_link_line(tmp_path: Path) -> None:
    from netgraph.trace import render_trace

    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        switch("sw-b"),
        panel("pp-a"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
        cable("seg-2", "pp-a:rear/1", "sw-b:p1"),
    )
    text = render_trace(trace(inventory, "sw-a", "sw-b"), "text")
    assert "[via pp-a front/1-rear/1]" in text
    # The panel is on the link line, never on a numbered hop line of its own.
    assert not any(line.strip().startswith(("1  pp-a", "2  pp-a")) for line in text.splitlines())


# --------------------------------------------------------------------------- #
# Placement and the rack view
# --------------------------------------------------------------------------- #

_PLACED = """
apiVersion: netgraph.dev/v1alpha1
kind: {kind}
metadata:
  name: {name}
  location: {{site: hq, room: mdf, rack: {rack}, position: {position}, height: {height},
    rack_height: 12}}
spec:
  interfaces:
    - {{name: p1, type: ethernet, mtu: 1500, vlan: {{mode: access, access_vlan: 10}}}}
"""


def placed(name: str, *, rack: str, position: int, height: int = 1, kind: str = "switch") -> str:
    return _PLACED.format(kind=kind, name=name, rack=rack, position=position, height=height)


def test_an_elevation_lists_every_unit_from_the_bottom_up(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        placed("sw-a", rack="r1", position=1),
        placed("srv-a", rack="r1", position=5, height=2, kind="server"),
    )
    (view,) = rack_elevations(inventory)
    assert view.key == ("hq", "mdf", "r1")
    assert view.label == "hq / mdf / r1"
    assert view.height == 12
    assert view.used_units == 3
    assert [slot.element for slot in view.slots] == ["sw-a", "srv-a"]

    # Top down, because that is how a person in front of the cabinet reads it.
    elevation = view.elevation()
    assert [unit for unit, _ in elevation] == list(range(12, 0, -1))
    assert view.occupant(6) is not None and view.occupant(6).element == "srv-a"
    assert view.occupant(4) is None


def test_an_element_with_a_rack_but_no_position_is_not_on_the_elevation(
    tmp_path: Path,
) -> None:
    inventory = inventory_of(
        tmp_path,
        placed("sw-a", rack="r1", position=1),
        """
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-b
  location: {site: hq, room: mdf, rack: r1}
spec:
  interfaces:
    - {name: p1, type: ethernet, mtu: 1500, vlan: {mode: access, access_vlan: 10}}
""",
    )
    (view,) = rack_elevations(inventory)
    assert [slot.element for slot in view.slots] == ["sw-a"]


def test_a_rack_with_no_declared_height_is_sized_by_its_tallest_occupant(
    tmp_path: Path,
) -> None:
    inventory = inventory_of(
        tmp_path,
        """
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-a
  location: {site: hq, rack: r1, position: 3, height: 2}
spec:
  interfaces:
    - {name: p1, type: ethernet, mtu: 1500, vlan: {mode: access, access_vlan: 10}}
""",
    )
    (view,) = rack_elevations(inventory)
    assert view.height == 4
    assert view.inferred_height is True


def test_the_rack_layer_builds_one_node_per_rack_and_no_edges(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        placed("sw-a", rack="r1", position=1),
        placed("sw-b", rack="r2", position=1),
    )
    graph = build_graph(inventory, layer=Layer.RACK)
    assert set(graph.nodes) == {f"{RACK_ID_PREFIX}hq/mdf/r1", f"{RACK_ID_PREFIX}hq/mdf/r2"}
    assert graph.edges == ()
    for node in graph.nodes.values():
        assert node.type is NodeType.RACK
        assert node.is_rack and not node.is_element
        assert node.namespace == ""


def test_filtering_a_rack_graph_narrows_the_elevation(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        placed("sw-a", rack="r1", position=1),
        placed("sw-b", rack="r2", position=1),
    )
    graph = filter_graph(build_graph(inventory, layer=Layer.RACK), FilterSpec(names=("sw-a",)))
    # The rack the survivor is in is kept; the other one has nothing left in it.
    assert set(graph.nodes) == {f"{RACK_ID_PREFIX}hq/mdf/r1"}


def test_an_element_with_no_location_is_in_no_rack(tmp_path: Path) -> None:
    inventory = inventory_of(tmp_path, switch("sw-a"))
    assert rack_elevations(inventory) == ()
    assert build_graph(inventory, layer=Layer.RACK).is_empty


def test_position_without_a_rack_is_refused() -> None:
    with pytest.raises(SchemaError) as caught:
        parse_document(
            {
                "apiVersion": API_VERSION,
                "kind": "patchpanel",
                "metadata": {"name": "pp", "location": {"position": 4}},
                "spec": {"ports": 4},
            }
        )
    assert any(issue.rule == "NG-U004" for issue in caught.value.issues)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def rack_graph(tmp_path: Path) -> Graph:
    inventory = inventory_of(tmp_path, placed("sw-a", rack="r1", position=1))
    return build_graph(inventory, layer=Layer.RACK)


def test_the_dot_elevation_draws_a_row_per_unit_empty_ones_included(tmp_path: Path) -> None:
    source = render_text(rack_graph(tmp_path), "dot")
    for unit in range(1, 13):
        assert f">U{unit}<" in source, unit
    assert source.count("·") == 11  # every unit but the one the switch is in
    assert "sw-a" in source


def test_mermaid_refuses_an_elevation_and_names_the_formats_that_can(
    tmp_path: Path,
) -> None:
    with pytest.raises(RenderError) as caught:
        render_text(rack_graph(tmp_path), "mermaid")
    message = str(caught.value)
    assert "rack elevation" in message
    for name in ("dot", "svg", "html", "json"):
        assert name in message


def test_the_json_export_carries_the_elevation(tmp_path: Path) -> None:
    import json

    payload = json.loads(render_text(rack_graph(tmp_path), "json"))
    (node,) = payload["nodes"]
    assert node["type"] == "rack"
    assert node["rack"]["height"] == 12
    assert node["rack"]["slots"] == [
        {"element": "sw-a", "name": "sw-a", "kind": "switch", "position": 1, "height": 1}
    ]
    # Bottom up, and every unit is listed whether or not anything is in it.
    units = node["rack"]["units"]
    assert [entry["unit"] for entry in units] == list(range(1, 13))
    assert units[0]["element"] == "sw-a"
    assert units[1]["element"] is None


def test_the_json_export_carries_the_patch_record(tmp_path: Path) -> None:
    import json

    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        switch("sw-b"),
        panel("pp-a"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
        cable("seg-2", "pp-a:rear/1", "sw-b:p1"),
    )
    payload = json.loads(render_text(build_graph(inventory, layer=Layer.L1), "json"))
    (edge,) = payload["edges"]
    assert edge["patch"] == {
        "segments": ["seg-1", "seg-2"],
        "panels": [{"panel": "pp-a", "ingress": "front/1", "egress": "rear/1"}],
    }


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_a_clean_patch_run_produces_no_findings(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        switch("sw-a"),
        switch("sw-b"),
        panel("pp-a"),
        cable("seg-1", "sw-a:p1", "pp-a:front/1"),
        cable("seg-2", "pp-a:rear/1", "sw-b:p1"),
    )
    findings = [finding for finding in validate(inventory) if finding.rule.startswith(("E0", "W1"))]
    assert [finding.rule for finding in findings if finding.rule in {"E021", "E022", "W133"}] == []


def test_an_edge_reports_whether_it_was_patched() -> None:
    """``is_patched`` is what an exporter and a report branch on."""
    plain = Edge(id="c", kind=EdgeKind.CABLE, source="a", target="b")
    assert not plain.is_patched
    assert replace(plain, patch=None).is_patched is False
