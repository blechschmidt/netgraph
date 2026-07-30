"""``netgraph web``: the stream pipeline, the info-box records, and the server.

The properties asserted here are the ones the interface promises and the ones a
user would otherwise discover the hard way:

* **A stream is loaded exactly as a folder is.** Same parser, same schema, same
  rules — so what the page reports and what ``netgraph validate`` reports about
  the same text cannot differ.
* **Broken text still draws.** ``netgraph render`` refuses an inventory with
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
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.errors import RenderError
from netgraph.loader import load_stream
from netgraph.render import Layer, build_graph, graph_to_dict
from netgraph.render.ids import element_ids
from netgraph.watch import Status
from netgraph.web import (
    ASSETS,
    MAX_SOURCE_BYTES,
    RENDER_PATH,
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

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_LAB = REPO_ROOT / "examples" / "home-lab"

requires_dot = pytest.mark.skipif(
    shutil.which("dot") is None, reason="Graphviz 'dot' is not installed"
)


def stream_of(root: Path) -> str:
    """Every document under ``root``, concatenated into one stream."""
    return "\n---\n".join(path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.yaml")))


@pytest.fixture(scope="module")
def home_lab() -> str:
    return stream_of(HOME_LAB)


TWO_HOSTS = """\
apiVersion: netgraph.dev/v1alpha1
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
apiVersion: netgraph.dev/v1alpha1
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
apiVersion: netgraph.dev/v1alpha1
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
    inventory = load_stream("apiVersion: netgraph.dev/v1alpha1\nkind: [computer\nmtu: 3\n")
    assert len(inventory.errors) == 1
    # The line the parser gave up on, which is where the unclosed sequence ran
    # into something that cannot be in it -- not where it was opened.
    assert inventory.errors[0].line == 3
    assert inventory.errors[0].location.startswith("stream.yaml:3")


def test_a_rejected_document_does_not_stop_the_ones_after_it() -> None:
    inventory = load_stream(f"kind: nonsense\n---\n{TWO_HOSTS}")
    assert [error.rule for error in inventory.errors] == ["NG-D003"]
    assert sorted(inventory.elements) == ["cbl-a-b", "pc-a", "pc-b"]


def test_a_duplicate_name_in_one_stream_is_reported() -> None:
    inventory = load_stream(f"{TWO_HOSTS}\n---\n{TWO_HOSTS}")
    assert any(error.rule == "NG-N002" for error in inventory.errors)


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
        "apiVersion: netgraph.dev/v1alpha1\n"
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
apiVersion: netgraph.dev/v1alpha1
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

    monkeypatch.setattr("netgraph.web.preview.to_image", explode)
    preview = render_source(TWO_HOSTS)
    assert preview.status is Status.FAILED
    assert preview.message == "No such file or directory: /themes/router.svg"


def test_a_missing_graphviz_is_reported_rather_than_raised(monkeypatch: Any) -> None:
    monkeypatch.setattr("netgraph.render.dot.shutil.which", lambda name: None)
    preview = render_source(TWO_HOSTS)
    assert preview.status is Status.FAILED
    assert "Graphviz" in preview.message
    assert preview.svg is None


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


def test_the_browser_cannot_choose_an_icon_directory() -> None:
    view = ViewOptions.from_request({"icons": "/etc"})
    assert view.icons is None


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


def test_a_seed_folder_becomes_one_stream(monkeypatch: Any) -> None:
    seeds: list[str] = []
    monkeypatch.setattr("netgraph.cli.WebServer.create", _capture(seeds, port_error=SystemExit(0)))
    result = run("web", str(HOME_LAB), "--no-open")
    assert result.exit_code == 0
    assert "# routers/rtr-home.yaml" in seeds[0]
    assert load_stream(seeds[0]).elements


def test_a_seed_file_is_used_as_it_is(monkeypatch: Any) -> None:
    seeds: list[str] = []
    monkeypatch.setattr("netgraph.cli.WebServer.create", _capture(seeds, port_error=SystemExit(0)))
    run("web", str(HOME_LAB / "routers" / "rtr-home.yaml"), "--no-open")
    assert seeds[0] == (HOME_LAB / "routers" / "rtr-home.yaml").read_text(encoding="utf-8")


def test_a_piped_stream_seeds_the_editor(monkeypatch: Any) -> None:
    seeds: list[str] = []
    monkeypatch.setattr("netgraph.cli.WebServer.create", _capture(seeds, port_error=SystemExit(0)))
    run("web", "--no-open", input=TWO_HOSTS)
    assert seeds[0] == TWO_HOSTS


def test_with_nothing_to_seed_from_the_editor_opens_on_the_example(monkeypatch: Any) -> None:
    seeds: list[str] = []
    monkeypatch.setattr("netgraph.cli.WebServer.create", _capture(seeds, port_error=SystemExit(0)))
    monkeypatch.setattr("netgraph.cli._is_a_terminal", lambda stream: True)
    run("web", "--no-open")
    inventory = load_stream(seeds[0])
    assert not inventory.errors
    assert inventory.devices


def _capture(seeds: list[str], *, port_error: BaseException) -> Any:
    """A ``WebServer.create`` that records the seed and then stops the command."""

    def create(**kwargs: Any) -> Any:
        seeds.append(kwargs["source"])
        raise port_error

    return create
