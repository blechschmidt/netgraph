"""``netviz web``: the stream pipeline, the info-box records, and the server.

The properties asserted here are the ones the interface promises and the ones a
user would otherwise discover the hard way:

* **A stream is loaded exactly as a folder is.** Same parser, same schema, same
  rules — so what the page reports and what ``netviz validate`` reports about
  the same text cannot differ.
* **Broken text still draws.** ``netviz render`` refuses an inventory with
  errors; this one draws what resolved and says what did not, because text
  being edited is wrong most of the time.
* **Every drawn element is addressable.** Each ``<g>`` in the SVG carries an id
  that the records are keyed by; if those two ever disagree, a hover shows the
  wrong device, which is worse than showing nothing.
* **The embedded SVG cannot execute or navigate.**
* **The server stays on this machine**, answers a fixed set of routes, and
  refuses a body it will not render rather than trying.
"""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from netviz.cli import cli
from netviz.errors import RenderError
from netviz.loader import load_stream
from netviz.render import Layer, build_graph, graph_to_dict
from netviz.render.icons import CISCO, icon_theme
from netviz.render.ids import element_ids
from netviz.watch import Status
from netviz.web import (
    ASSETS,
    BINDINGS,
    BINDINGS_PATH,
    MAX_SOURCE_BYTES,
    RENDER_PATH,
    SECTIONS,
    SOURCE_PATH,
    Preview,
    RequestError,
    ViewOptions,
    WebServer,
    asset,
    build_details,
    prepare,
    render_source,
)
from netviz.web.bindings import (
    MENU_TARGETS,
    MENUS,
    markdown_menus,
    markdown_table,
)
from netviz.web.bindings import payload as bindings_payload
from netviz.web.preview import graph_digest, icon_choices

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_LAB = REPO_ROOT / "examples" / "home-lab"


def stream_of(root: Path) -> str:
    """Every document under ``root``, concatenated into one stream."""
    return "\n---\n".join(path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.yaml")))


@pytest.fixture(scope="module")
def home_lab() -> str:
    return stream_of(HOME_LAB)


TWO_HOSTS = """\
apiVersion: netviz.dev/v1alpha1
kind: computer
metadata:
  name: pc-a
spec:
  interfaces:
    - name: eth0
      type: ethernet
      mtu: 1500
      mac: "00:11:22:33:44:01"
      ipv4:
        addresses: [10.0.0.1/30]
---
apiVersion: netviz.dev/v1alpha1
kind: computer
metadata:
  name: pc-b
spec:
  interfaces:
    - name: eth0
      type: ethernet
      mtu: 1500
      ipv4:
        addresses: [10.0.0.2/30]
---
apiVersion: netviz.dev/v1alpha1
kind: cable
metadata:
  name: cbl-a-b
spec:
  endpoints:
    - pc-a:eth0
    - pc-b:eth0
  medium: copper
  speed: 1Gbps
"""


# --------------------------------------------------------------------------- #
# Loading a stream
# --------------------------------------------------------------------------- #


def test_a_stream_loads_like_a_folder(home_lab: str) -> None:
    inventory = load_stream(home_lab)
    assert not inventory.errors
    assert len(inventory.devices) == 7
    assert len(inventory.cables) == 6
    assert len(inventory.adapters) == 1


def test_every_element_of_a_stream_lands_in_the_root_namespace() -> None:
    inventory = load_stream(TWO_HOSTS)
    assert sorted(inventory.elements) == ["cbl-a-b", "pc-a", "pc-b"]


def test_a_syntax_error_is_recorded_with_its_line() -> None:
    inventory = load_stream("apiVersion: netviz.dev/v1alpha1\nkind: [computer\nmtu: 3\n")
    assert len(inventory.errors) == 1
    # The line the parser gave up on, which is where the unclosed sequence ran
    # into something that cannot be in it -- not where it was opened.
    assert inventory.errors[0].line == 3
    assert inventory.errors[0].location.startswith("stream.yaml:3")


def test_a_rejected_document_does_not_stop_the_ones_after_it() -> None:
    inventory = load_stream(f"kind: nonsense\n---\n{TWO_HOSTS}")
    assert [error.rule for error in inventory.errors] == ["NV-D003"]
    assert sorted(inventory.elements) == ["cbl-a-b", "pc-a", "pc-b"]


def test_a_duplicate_name_in_one_stream_is_reported() -> None:
    inventory = load_stream(f"{TWO_HOSTS}\n---\n{TWO_HOSTS}")
    assert any(error.rule == "NV-N002" for error in inventory.errors)


def test_an_empty_stream_loads_to_nothing() -> None:
    assert not load_stream("").elements


# --------------------------------------------------------------------------- #
# One render pass
# --------------------------------------------------------------------------- #


@requires_dot
def test_a_valid_stream_renders(home_lab: str) -> None:
    preview = render_source(home_lab)
    assert preview.status is Status.OK
    assert preview.nodes == 8
    assert preview.edges == 7
    assert not preview.problems
    assert preview.svg is not None
    assert preview.svg.startswith("<svg")
    assert preview.duration >= 0


@requires_dot
def test_a_stream_with_errors_is_still_drawn() -> None:
    dangling = TWO_HOSTS + (
        "---\n"
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata:\n"
        "  name: cbl-nowhere\n"
        "spec:\n"
        "  endpoints:\n"
        "    - pc-a:eth9\n"
        "    - ghost:eth0\n"
        "  medium: copper\n"
    )
    preview = render_source(dangling)
    assert preview.status is Status.INVALID
    assert preview.svg is not None, "a rejected stream must still show what resolved"
    assert preview.nodes == 2
    assert preview.error_count >= 1
    assert preview.dangling


@requires_dot
def test_an_empty_stream_reports_that_there_is_nothing_to_draw() -> None:
    preview = render_source("")
    assert preview.status is Status.OK
    assert "no elements" in preview.message


@requires_dot
def test_strict_promotes_warnings_to_errors() -> None:
    # A single-member prefix is W105, a warning, and pc-a's spare port is I002.
    lax = render_source(TWO_HOSTS + LONE_HOST)
    strict = render_source(TWO_HOSTS + LONE_HOST, ViewOptions(strict=True))
    assert lax.status is Status.OK
    assert strict.status is Status.INVALID
    assert strict.error_count == lax.warning_count


LONE_HOST = """\
---
apiVersion: netviz.dev/v1alpha1
kind: computer
metadata:
  name: pc-lonely
spec:
  interfaces:
    - name: eth0
      type: ethernet
      mtu: 1500
      ipv4:
        addresses: [192.168.9.1/24]
"""


@requires_dot
def test_the_layer_decides_which_graph_is_drawn(home_lab: str) -> None:
    l1 = render_source(home_lab, ViewOptions(layer=Layer.L1))
    l3 = render_source(home_lab, ViewOptions(layer=Layer.L3))
    assert l1.nodes != l3.nodes
    assert any(record.get("subnet") for record in l3.details.values())


@requires_dot
def test_a_vlan_filter_narrows_the_graph(home_lab: str) -> None:
    everything = render_source(home_lab)
    filtered = render_source(home_lab, ViewOptions(vlans=frozenset({4000})))
    assert everything.nodes > 0
    assert filtered.nodes == 0
    assert "filters" in filtered.message


@requires_dot
def test_a_kind_filter_keeps_only_that_kind(home_lab: str) -> None:
    preview = render_source(home_lab, ViewOptions(kinds=("switch",)))
    kinds = {record["kind"] for record in preview.details.values() if record["type"] != "edge"}
    assert kinds == {"switch"}


def test_a_failure_that_is_not_a_sentence_is_made_into_one(monkeypatch: Any) -> None:
    def explode(*args: Any, **kwargs: Any) -> bytes:
        raise OSError(2, "No such file or directory", "/themes/router.svg")

    monkeypatch.setattr("netviz.web.preview.to_image", explode)
    preview = render_source(TWO_HOSTS)
    assert preview.status is Status.FAILED
    assert preview.message == "No such file or directory: /themes/router.svg"


def test_a_missing_graphviz_is_reported_rather_than_raised(monkeypatch: Any) -> None:
    monkeypatch.setattr("netviz.render.dot.find_dot", lambda: None)
    preview = render_source(TWO_HOSTS)
    assert preview.status is Status.FAILED
    assert "Graphviz" in preview.message
    assert preview.svg is None


# --------------------------------------------------------------------------- #
# Annotations (§21)
# --------------------------------------------------------------------------- #

#: Two hosts and something written on the drawing about them: a note pinned to a
#: point, a zone that follows its members, and a generated key.
ANNOTATED = """\
---
apiVersion: netviz.dev/v1alpha1
kind: note
metadata:
  name: why-here
spec:
  text: |
    **Two** of them, on purpose.
  geometry: {x: 40, y: 90, width: 200, height: 60}
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata:
  name: the-pair
spec:
  label: The pair
  members: [pc-a, pc-b]
---
apiVersion: netviz.dev/v1alpha1
kind: legend
metadata:
  name: key
spec:
  title: Key
  auto: layers
"""


@requires_dot
def test_the_preview_publishes_the_annotations_it_drew() -> None:
    """The browser cannot read them off the SVG, so the answer carries them.

    An area in an arranged drawing is a rectangle in the graph's background with
    no id on it at all, so a canvas that hit-tested the DOM would find notes and
    silently nothing else. This payload is the same one
    ``netviz render -f json`` publishes: id, document, geometry, members.
    """
    preview = render_source(TWO_HOSTS + ANNOTATED)
    payload = preview.to_dict()["annotations"]
    assert [note["id"] for note in payload["notes"]] == ["note-why-here"]
    assert payload["notes"][0]["fqn"] == "why-here"
    assert payload["notes"][0]["layout"] == {
        "position": {"x": 40.0, "y": 90.0},
        "size": {"width": 200.0, "height": 60.0},
    }
    assert payload["areas"][0]["members"] == ["pc-a", "pc-b"]
    # The zone follows its members, so it pins no rectangle -- which is exactly
    # what tells the canvas to refuse to drag it rather than to offer handles.
    assert "layout" not in payload["areas"][0]
    assert payload["legends"][0]["entries"], "a generated key arrives generated"


@requires_dot
def test_an_inventory_with_nothing_written_on_it_publishes_nothing() -> None:
    assert render_source(TWO_HOSTS).to_dict()["annotations"] is None


@requires_dot
def test_the_annotation_toggle_takes_them_out_of_the_picture_and_the_payload() -> None:
    """And moves the fingerprint, so the browser's cache cannot serve the wrong one.

    The other view toggles behave this way and the cache is keyed on the request,
    so a toggle that did not reach the digest would show the annotated drawing
    under an unannotated request until something else moved.
    """
    on = render_source(TWO_HOSTS + ANNOTATED)
    off = render_source(TWO_HOSTS + ANNOTATED, ViewOptions(annotations=False))
    assert on.to_dict()["annotations"] is not None
    assert off.to_dict()["annotations"] is None
    assert on.graph_hash != off.graph_hash
    assert on.svg is not None and off.svg is not None
    assert "note-why-here" in on.svg
    assert "note-why-here" not in off.svg
    # It is commentary, never topology: the same graph is drawn either way.
    assert (on.nodes, on.edges) == (off.nodes, off.edges)


def test_the_page_offers_the_annotation_toggle() -> None:
    """A rendering knob the server takes and the page cannot ask for is a knob
    nobody finds."""
    page = asset("index.html").decode("utf-8")
    assert 'id="show-annotations"' in page


# --------------------------------------------------------------------------- #
# The info-box records
# --------------------------------------------------------------------------- #


def test_every_record_is_the_json_export_of_its_element(home_lab: str) -> None:
    graph = build_graph(load_stream(home_lab), layer=Layer.L2)
    exported = graph_to_dict(graph)
    details = build_details(graph)
    ids = element_ids(graph)

    for node in exported["nodes"]:
        record = details[ids.nodes[node["id"]]]
        assert {key: record[key] for key in node} == node
    for index, edge in enumerate(exported["edges"]):
        record = details[ids.edges[index]]
        # ``endpoints`` gains the id of the node each end is drawn as.
        for key in edge:
            if key != "endpoints":
                assert record[key] == edge[key]
        assert [end["node"] for end in record["endpoints"]] == [
            end["node"] for end in edge["endpoints"]
        ]


def test_a_record_lists_the_links_that_terminate_on_it() -> None:
    graph = build_graph(load_stream(TWO_HOSTS), layer=Layer.L1)
    details = build_details(graph)
    node = next(record for record in details.values() if record.get("name") == "pc-a")
    assert len(node["links"]) == 1
    link = node["links"][0]
    assert link["peer"] == "pc-b"
    assert link["interface"] == "eth0"
    assert link["peerInterface"] == "eth0"
    assert link["speedText"] == "1Gbps"
    assert details[link["element"]]["type"] == "edge"
    assert details[link["peerElement"]]["name"] == "pc-b"


def test_a_record_carries_the_detail_the_diagram_leaves_out() -> None:
    graph = build_graph(load_stream(TWO_HOSTS), layer=Layer.L1)
    node = next(record for record in build_details(graph).values() if record.get("name") == "pc-a")
    port = node["interfaces"][0]
    assert port["mac"] == "00:11:22:33:44:01"
    assert port["mtu"] == 1500
    assert port["addresses"] == ["10.0.0.1/30"]


@requires_dot
def test_every_drawn_element_has_a_record_and_every_record_is_drawn(home_lab: str) -> None:
    preview = render_source(home_lab)
    assert preview.svg is not None
    drawn = set(re.findall(r'<g id="((?:node|edge)-[^"]+)" class="(?:node|edge)"', preview.svg))
    assert drawn == set(preview.details)


# --------------------------------------------------------------------------- #
# Preparing the SVG for a live page
# --------------------------------------------------------------------------- #

MINIMAL_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
    b'width="10pt" height="20pt" viewBox="0 0 10 20">'
    b"<g id='n0' class='node' onclick='steal()'><title>pc-a</title>"
    b'<a xlink:title="tooltip" xlink:href="https://example.invalid/">'
    b'<ellipse fill="#fff"/></a></g>'
    b'<image xlink:href="data:image/png;base64,AAAA"/>'
    b'<script type="text/javascript">alert(1)</script>'
    b"</svg>"
)


def test_the_prepared_svg_scales_with_its_box() -> None:
    prepared = prepare(MINIMAL_SVG)
    # On the *root element*, which is what decides how big the picture is
    # drawn. Further down there may well be a width: a hoisted icon symbol
    # fills the box its <use> gives it with one.
    root = prepared[: prepared.index(">") + 1]
    assert "width=" not in root
    assert "height=" not in root
    assert 'viewBox="0 0 10 20"' in root
    assert "preserveAspectRatio" in root


def test_the_prepared_svg_cannot_execute_or_navigate() -> None:
    prepared = prepare(MINIMAL_SVG)
    assert "script" not in prepared
    assert "onclick" not in prepared
    assert "example.invalid" not in prepared


def test_the_prepared_svg_carries_no_native_tooltips() -> None:
    prepared = prepare(MINIMAL_SVG)
    assert "tooltip" not in prepared
    assert "<title>" not in prepared


def test_the_prepared_svg_keeps_an_inline_icon() -> None:
    assert "data:image/png;base64,AAAA" in prepare(MINIMAL_SVG)


def test_the_prepared_svg_keeps_the_element_ids() -> None:
    assert 'id="n0"' in prepare(MINIMAL_SVG)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"<svg", "could not be parsed"),
        (b"<html></html>", "expected an SVG"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "viewBox"),
    ],
)
def test_something_that_is_not_a_usable_svg_is_refused(payload: bytes, reason: str) -> None:
    with pytest.raises(RenderError, match=reason):
        prepare(payload)


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #


def test_a_request_may_set_every_option_the_page_offers() -> None:
    view = ViewOptions.from_request(
        {
            "layer": "l3",
            "vlans": [10, 20, 10],
            "kinds": ["switch"],
            "show_ips": False,
            "show_vlans": False,
            "annotations": False,
            "group_by_namespace": True,
            "strict": True,
            "title": "office",
        }
    )
    assert view.layer is Layer.L3
    assert view.vlans == frozenset({10, 20})
    assert view.kinds == ("switch",)
    assert not view.show_ips
    assert not view.show_vlans
    assert not view.annotations
    assert not view.render_options.annotations
    assert view.group_by_namespace
    assert view.strict
    assert view.title == "office"


def test_an_empty_request_is_the_default_view() -> None:
    assert ViewOptions.from_request({}) == ViewOptions()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"layer": "l9"}, "unknown layer"),
        ({"layer": 3}, "unknown layer"),
        ({"show_ips": "yes"}, "must be true or false"),
        ({"vlans": 10}, "must be a list"),
        ({"vlans": [0]}, "not a VLAN id"),
        ({"vlans": [4095]}, "not a VLAN id"),
        ({"vlans": [True]}, "not a VLAN id"),
        ({"vlans": ["10"]}, "not a VLAN id"),
        ({"kinds": "switch"}, "must be a list of strings"),
        ({"kinds": [1]}, "must be a list of strings"),
        ({"title": 5}, "must be a string"),
    ],
)
def test_a_request_that_asks_for_something_impossible_is_refused(
    payload: dict[str, Any], reason: str
) -> None:
    with pytest.raises(RequestError, match=reason):
        ViewOptions.from_request(payload)


@pytest.mark.parametrize("named", ["/etc", "../icons", "cisco/../../etc", "Cisco", 7])
def test_the_browser_cannot_choose_an_icon_directory(named: object) -> None:
    """The switch chooses between themes, and cannot invent one.

    The browser may turn icons off and on because that is a question about the
    picture; it may not say *where the pictures are*, because that is a
    directory on the machine running the server. So every name goes through the
    closed set, and one that is not in it is a refusal rather than a theme.
    """
    with pytest.raises(RequestError, match=r"unknown icon theme|must be a string"):
        ViewOptions.from_request({"icons": named})


def test_the_browser_may_turn_the_bundled_theme_on_and_off() -> None:
    assert ViewOptions.from_request({"icons": "cisco"}).icons is CISCO
    assert ViewOptions.from_request({"icons": "none"}, icons=CISCO).icons is None
    # Silence is not "off": a request that says nothing about icons draws with
    # whatever the command line chose.
    assert ViewOptions.from_request({}, icons=CISCO).icons is CISCO


def test_a_directory_theme_is_switchable_as_custom(tmp_path: Path) -> None:
    """``--icons DIR`` stays switchable without the browser naming the directory.

    The theme is offered as ``custom``, which is the whole trick: the page can
    turn it back on after turning it off, and the only name it ever holds for it
    is one that says nothing about this filesystem.
    """
    (tmp_path / "router.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    theme = icon_theme(str(tmp_path))
    assert icon_choices(theme) == ("cisco", "custom", "none")
    assert ViewOptions.from_query({"icons": ["custom"]}, icons=theme).icons is theme
    assert ViewOptions.from_query({"icons": ["none"]}, icons=theme).icons is None
    # Not even the name it has here: that name is a directory on this machine,
    # and a page that never learns it cannot leak it.
    for refused in (str(tmp_path), "/etc"):
        with pytest.raises(RequestError, match="unknown icon theme"):
            ViewOptions.from_query({"icons": [refused]}, icons=theme)


def test_icons_are_part_of_which_drawing_this_is() -> None:
    """Two views that differ only in their theme must not share a cache entry.

    ``ViewOptions`` is a cache key — ``EditingSession.frame`` keys frames on one
    — and the fingerprint a client holds is of the DOT. Both have to move when
    the icons do, or switching them would answer "nothing changed".
    """
    plain, drawn = ViewOptions(), ViewOptions(icons=CISCO)
    assert plain != drawn
    assert len({plain, drawn}) == 2
    graph = build_graph(load_stream(TWO_HOSTS))
    assert graph_digest(graph, plain.render_options) != graph_digest(graph, drawn.render_options)


# --------------------------------------------------------------------------- #
# The server
# --------------------------------------------------------------------------- #


@pytest.fixture
def server() -> Iterator[WebServer]:
    with WebServer.create(source=TWO_HOSTS, host="127.0.0.1", port=0) as running:
        yield running


def request(
    server: WebServer,
    path: str,
    *,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One request against the interface, returning status, headers and body."""
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def post(server: WebServer, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    status, _, body = request(
        server,
        RENDER_PATH,
        method="POST",
        body=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    return status, json.loads(body)


def test_the_page_and_its_assets_are_served(server: WebServer) -> None:
    for path, (name, content_type) in ASSETS.items():
        status, headers, body = request(server, path)
        assert status == 200, path
        assert headers["Content-Type"] == content_type
        assert body == asset(name)


def test_the_layer_selector_offers_every_layer_the_server_accepts() -> None:
    """A layer the page cannot ask for is a view nobody finds."""
    page = asset("index.html").decode("utf-8")
    for layer in Layer:
        assert f'<option value="{layer.value}">' in page, layer.value


def test_the_editor_is_seeded_from_the_command_line(server: WebServer) -> None:
    status, headers, body = request(server, SOURCE_PATH)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["source"] == TWO_HOSTS


@requires_dot
def test_posting_a_stream_returns_a_diagram_and_its_records(server: WebServer) -> None:
    status, payload = post(server, {"source": TWO_HOSTS, "layer": "l1"})
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["counts"] == {"nodes": 2, "edges": 1, "errors": 0, "warnings": 0}
    assert payload["svg"].startswith("<svg")
    assert set(payload["details"]) == {"node-pc-a", "node-pc-b", "edge-cbl-a-b"}
    assert payload["durationMs"] >= 0


@requires_dot
def test_posting_nothing_renders_nothing_rather_than_failing(server: WebServer) -> None:
    status, payload = post(server, {})
    assert status == 200
    assert payload["counts"]["nodes"] == 0


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"source": 5}, "must be the YAML document stream"),
        ({"layer": "l9"}, "unknown layer"),
    ],
)
def test_a_request_the_interface_cannot_honour_is_a_bad_request(
    server: WebServer, payload: dict[str, Any], reason: str
) -> None:
    status, body = post(server, payload)
    assert status == 400
    assert reason in body["message"]
    assert body["status"] == "failed"


def test_a_body_that_is_not_a_json_object_is_refused(server: WebServer) -> None:
    for body in ("not json", "[1, 2]", "null"):
        status, _, payload = request(
            server, RENDER_PATH, method="POST", body=body, headers={"Content-Type": "text/plain"}
        )
        assert status == 400, body
        assert b"message" in payload


def test_a_stream_too_large_to_edit_is_refused_before_it_is_read(server: WebServer) -> None:
    status, _, body = request(
        server,
        RENDER_PATH,
        method="POST",
        body="{}",
        headers={"Content-Length": str(MAX_SOURCE_BYTES + 1)},
    )
    assert status == 400
    assert str(MAX_SOURCE_BYTES) in json.loads(body)["message"]


def test_a_post_without_a_length_is_refused(server: WebServer) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        connection.putrequest("POST", RENDER_PATH, skip_accept_encoding=True)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        assert "Content-Length" in json.loads(response.read())["message"]
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("length", "reason"),
    [("abc", "is not a number"), ("-1", "must not be negative")],
)
def test_a_length_that_is_not_one_is_refused(server: WebServer, length: str, reason: str) -> None:
    # ``request()`` would compute the header; this one has to be malformed, so
    # it is written out field by field with no body behind it.
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        connection.putrequest("POST", RENDER_PATH, skip_accept_encoding=True)
        connection.putheader("Content-Length", length)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        assert reason in json.loads(response.read())["message"]
    finally:
        connection.close()


def test_the_render_route_says_which_method_it_wants(server: WebServer) -> None:
    status, headers, _ = request(server, RENDER_PATH)
    assert status == 405
    assert headers["Allow"] == "POST"


def test_posting_anywhere_else_is_not_found(server: WebServer) -> None:
    status, _, _ = request(server, "/api/other", method="POST", body="{}")
    assert status == 404


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/../app.js", "/assets/app.js", "/index.html", "/app.js.bak"]
)
def test_no_request_path_reaches_a_file(server: WebServer, path: str) -> None:
    assert request(server, path)[0] == 404


def test_a_head_request_carries_the_headers_but_no_body(server: WebServer) -> None:
    status, headers, body = request(server, "/", method="HEAD")
    assert status == 200
    assert int(headers["Content-Length"]) > 0
    assert body == b""


def test_every_response_carries_the_hardening_headers(server: WebServer) -> None:
    _, headers, _ = request(server, "/")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store, max-age=0"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_a_loopback_server_answers_only_to_localhost(server: WebServer) -> None:
    assert request(server, "/", headers={"Host": "127.0.0.1"})[0] == 200
    assert request(server, "/", headers={"Host": "localhost:1234"})[0] == 200
    assert request(server, "/", headers={"Host": "evil.example"})[0] == 421


def test_a_refused_request_leaves_the_connection_usable(server: WebServer) -> None:
    """A body nobody read is the next request, as far as HTTP/1.1 is concerned.

    Every refusal that answers without looking at the body — a 404 for an
    unknown route, a 403 from a read-only session, the 421 from the host check —
    used to strand it in the socket, and the request *after* it on the same
    connection was then parsed out of those leftover bytes. The symptom was a
    ``501 Unsupported method`` that named a fragment of JSON, on a request that
    was perfectly well formed. A browser keeps the connection alive, so this is
    the ordinary case and not an exotic one.
    """
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    try:
        for _ in range(3):
            connection.request(
                "POST",
                "/api/nothing-here",
                body=json.dumps({"source": TWO_HOSTS}),
                headers={"Content-Type": "application/json"},
            )
            refusal = connection.getresponse()
            assert refusal.status == 404
            refusal.read()

            # The one that would have been parsed out of the leftover body.
            connection.request("GET", "/api/state")
            answer = connection.getresponse()
            assert answer.status == 200
            assert json.loads(answer.read())["mode"] == "stream"
    finally:
        connection.close()


def test_the_url_is_one_a_browser_can_open(server: WebServer) -> None:
    assert server.url == f"http://127.0.0.1:{server.port}/"


def test_stopping_twice_is_harmless() -> None:
    server = WebServer.create(host="127.0.0.1", port=0).start()
    server.stop()
    server.stop()


@requires_dot
def test_the_render_callback_sees_every_pass() -> None:
    seen: list[Preview] = []
    with WebServer.create(source="", host="127.0.0.1", port=0, on_render=seen.append) as running:
        post(running, {"source": TWO_HOSTS})
    assert [preview.status for preview in seen] == [Status.OK]


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def run(*args: str, input: str | None = None) -> Any:
    return CliRunner().invoke(cli, list(args), input=input)


def test_the_command_is_documented() -> None:
    result = run("web", "--help")
    assert result.exit_code == 0
    assert "document stream" in result.output
    assert "--no-open" in result.output


def test_a_seed_folder_opens_a_session_rather_than_a_stream(monkeypatch: Any) -> None:
    """A folder is a tree, and a tree is what the editing session is for.

    It used to be flattened into one stream, which lost the folders and with
    them the namespaces; see ``tests/test_web_session.py`` for what it does now.
    """
    seeds: list[str] = []
    sessions: list[Any] = []
    monkeypatch.setattr(
        "netviz.cli.WebServer.create",
        _capture(seeds, sessions=sessions, port_error=SystemExit(0)),
    )
    result = run("web", str(HOME_LAB), "--no-open")
    assert result.exit_code == 0
    assert seeds[0] == ""
    assert sessions[0] is not None
    assert sessions[0].root == HOME_LAB
    assert not sessions[0].writable


def test_a_seed_file_is_used_as_it_is(monkeypatch: Any) -> None:
    seeds: list[str] = []
    monkeypatch.setattr("netviz.cli.WebServer.create", _capture(seeds, port_error=SystemExit(0)))
    run("web", str(HOME_LAB / "routers" / "rtr-home.yaml"), "--no-open")
    assert seeds[0] == (HOME_LAB / "routers" / "rtr-home.yaml").read_text(encoding="utf-8")


def test_a_piped_stream_seeds_the_editor(monkeypatch: Any) -> None:
    seeds: list[str] = []
    monkeypatch.setattr("netviz.cli.WebServer.create", _capture(seeds, port_error=SystemExit(0)))
    run("web", "--no-open", input=TWO_HOSTS)
    assert seeds[0] == TWO_HOSTS


def test_with_nothing_to_seed_from_the_editor_opens_on_the_example(monkeypatch: Any) -> None:
    seeds: list[str] = []
    monkeypatch.setattr("netviz.cli.WebServer.create", _capture(seeds, port_error=SystemExit(0)))
    monkeypatch.setattr("netviz.cli._is_a_terminal", lambda stream: True)
    run("web", "--no-open")
    inventory = load_stream(seeds[0])
    assert not inventory.errors
    assert inventory.devices


def _capture(
    seeds: list[str], *, port_error: BaseException, sessions: list[Any] | None = None
) -> Any:
    """A ``WebServer.create`` that records what it was given and stops the command."""

    def create(**kwargs: Any) -> Any:
        seeds.append(kwargs["source"])
        if sessions is not None:
            sessions.append(kwargs.get("session"))
        raise port_error

    return create


# --------------------------------------------------------------------------- #
# The bindings: one table, three consumers
# --------------------------------------------------------------------------- #


def test_the_bindings_are_served_in_both_faces(server: WebServer) -> None:
    """The page cannot be driven before it has them, so they are never a 404.

    Answered by the scratchpad too: a stream has fewer commands *available*, not
    fewer commands, and the palette says which are out of reach and why.
    """
    status, headers, body = request(server, BINDINGS_PATH)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["sections"] == list(SECTIONS)
    assert len(payload["bindings"]) == len(BINDINGS)
    assert "switch" in payload["kinds"], "the create gesture builds its menu from these"


def test_every_binding_is_well_formed() -> None:
    """The table is data the page executes, so its vocabulary is closed."""
    seen: set[str] = set()
    for binding in BINDINGS:
        assert binding.id not in seen, f"{binding.id} is declared twice"
        seen.add(binding.id)
        assert binding.section in SECTIONS, binding.id
        assert binding.where in ("global", "canvas"), binding.id
        assert binding.needs in ("", "session", "write", "focus"), binding.id
        assert binding.detail.strip(), f"{binding.id} says nothing about itself"
        for key in binding.keys:
            # ``Ctrl-Shift-K``: modifiers in a fixed order, then one key. The
            # matcher in keys.js builds exactly this string from a KeyboardEvent.
            *modifiers, _ = key.split("-")
            assert modifiers == [m for m in ("Ctrl", "Alt", "Shift") if m in modifiers], key


def test_no_chord_runs_two_commands_in_one_place() -> None:
    """A chord bound twice is a coin toss, and the loser is silently dead.

    ``global`` and ``canvas`` may share a chord — ``Escape`` and a letter mean
    different things depending on where the focus is — so the check is per
    scope, which is exactly how keys.js resolves them.
    """
    for where in ("global", "canvas"):
        claimed: dict[str, str] = {}
        for binding in BINDINGS:
            if binding.where != where:
                continue
            for key in binding.keys:
                chord = _normalise_chord(key)
                assert chord not in claimed, (
                    f"{where}: {key} runs both {claimed[chord]} and {binding.id}"
                )
                claimed[chord] = binding.id


def _normalise_chord(chord: str) -> str:
    """The spelling keys.js compares against: a single-character key lower-cased."""
    *modifiers, key = chord.split("-")
    return "-".join([*modifiers, key.lower() if len(key) == 1 else key])


#: Where a command's implementation may live. ``keys.js`` owns the machinery,
#: ``app.js`` the view and navigation commands, ``select.js`` the selection and
#: the tidying that needs one, ``search.js`` the selector language, ``session.js``
#: everything that writes, ``style.js`` the style inspector, ``tour.js`` the
#: guided tour — but which of the seven a given id is in is not this test's
#: business.
_COMMAND_FILES = (
    "keys.js",
    "app.js",
    "select.js",
    "search.js",
    "session.js",
    "style.js",
    "tour.js",
    "clipboard.js",
)


def _registered() -> set[str]:
    """Every id the page registers a handler for, read out of the assets.

    A regex over JavaScript, which is a blunt instrument — and the right one
    here. The alternative is running the page in a browser to ask it, which is
    what ``tests/test_browser.py`` does and what this file exists to *not*
    require: a binding added to the table without a handler should fail in the
    fast suite, a second after it is typed.
    """
    found: set[str] = set()
    for name in _COMMAND_FILES:
        source = asset(name).decode("utf-8")
        found.update(re.findall(r'(?:K|netvizKeys)\.define\(\s*"([^"]+)"', source))
    return found


def test_every_binding_has_a_handler_and_every_handler_a_binding() -> None:
    """The one direction neither the table nor the docs can check for itself.

    A command in :data:`netviz.web.bindings.BINDINGS` with nothing behind it
    is a palette row that does nothing and a documented shortcut that is a lie;
    a handler with no entry is a feature with no key, no palette row and no
    line in ``docs/commands/web.md``. Both are silent, and both fail here.
    """
    declared = {binding.id for binding in BINDINGS}
    registered = _registered()
    assert declared - registered == set(), (
        "declared in netviz/web/bindings.py with no handler in "
        f"{', '.join(_COMMAND_FILES)}: {sorted(declared - registered)}"
    )
    assert registered - declared == set(), (
        "registered in the page with no entry in netviz/web/bindings.py: "
        f"{sorted(registered - declared)}"
    )


def test_the_shortcut_reference_is_the_documented_one() -> None:
    """docs/commands/web.md holds this table, generated. Nothing is written twice."""
    page = (REPO_ROOT / "docs" / "commands" / "web.md").read_text(encoding="utf-8")
    assert "<!-- generated: keybindings -->" in page
    assert markdown_table() in page
    for binding in BINDINGS:
        assert binding.title in page, binding.id


# --------------------------------------------------------------------------- #
# The context menus: a layout over the same table
# --------------------------------------------------------------------------- #


def test_the_menus_are_served_with_the_bindings() -> None:
    """One fetch, because they are one table: the menu is a view of the commands.

    A page that fetched its menus separately could draw one before it knew what
    the rows meant, and would have a window in which right-clicking offers a row
    with no title, no chord and no idea whether it may run.
    """
    payload = bindings_payload()
    assert [menu["target"] for menu in payload["menus"]] == list(MENU_TARGETS)
    for menu in payload["menus"]:
        assert menu["groups"], f"{menu['target']} offers nothing"
        for group in menu["groups"]:
            for item in group:
                assert set(item) == {"binding", "label", "submenu"}, item


def test_every_menu_row_names_a_declared_command() -> None:
    """The one invariant that makes the menu a view rather than a second list.

    A row naming an id that is not in :data:`BINDINGS` would draw with no title
    and refuse with "not available in this build" — a dead row nobody could
    explain. It cannot happen, and this is why.
    """
    declared = {binding.id for binding in BINDINGS}
    for menu in MENUS:
        assert menu.target in MENU_TARGETS, menu.target
        seen: set[tuple[str, str]] = set()
        for group in menu.groups:
            assert group, f"{menu.target} has an empty group, which draws as a stray rule"
            for item in group:
                assert item.binding in declared, f"{menu.target}: {item.binding}"
                assert item.submenu in ("", "kinds"), item.binding
                # A command twice in one menu is two rows that do the same thing
                # under two names, which is the worst way to learn either.
                key = (item.binding, item.label)
                assert key not in seen, f"{menu.target} offers {item.binding} twice"
                seen.add(key)


def test_every_target_has_a_menu() -> None:
    """A right-click that opens nothing is a right-click that looks broken.

    ``menu.js`` falls back to the browser's own menu when a target has no rows,
    which is the right behaviour for a build that has lost one and the wrong
    thing to ship on purpose.
    """
    assert {menu.target for menu in MENUS} == set(MENU_TARGETS)


def test_a_menu_row_is_worded_for_the_thing_it_was_opened_on() -> None:
    """The labels are overridden deliberately, so check they were.

    "Delete the focused element" is how you name a command you cannot point at.
    Every row here *was* pointed at, so every row that would otherwise say
    "focused" carries its own wording.
    """
    for menu in MENUS:
        for group in menu.groups:
            for item in group:
                binding = next(one for one in BINDINGS if one.id == item.binding)
                shown = item.label or binding.title
                assert shown.strip(), item.binding
                assert "focused" not in shown, (
                    f"{menu.target}: {item.binding} still reads as a palette row"
                )


def test_the_context_menus_are_the_documented_ones() -> None:
    """Same bargain as the shortcut sheet: generated, never written twice."""
    page = (REPO_ROOT / "docs" / "commands" / "web.md").read_text(encoding="utf-8")
    assert "<!-- generated: context-menus -->" in page
    assert markdown_menus() in page
