"""Property-based tests: the invariants example tests can only sample.

Every other module in this suite asserts behaviour on an inventory somebody
wrote. That is the right way to pin down *what a feature does*, and the wrong
way to pin down the handful of statements in this codebase that are universally
quantified:

* the loader and the model layer are inverses of each other, for every document;
* ``netgraph fmt`` is idempotent and meaning-preserving, for every document;
* ``range:`` and ``spec.from`` are shorthands, so for every document that uses
  them there is a longhand document that means exactly the same thing;
* no free text in an inventory can become *syntax* in any output format;
* ``validate`` is a function of the inventory and of nothing else;
* ``netgraph path`` reports routes that exist in the graph it was derived from.

Each of those is a statement about all inputs, and an example test can only ever
say it held for the inputs somebody thought of. The strategies in
``tests/strategies.py`` produce the rest.

Reading a failure
-----------------

Hypothesis prints the shrunk counterexample as an :class:`InventoryPlan`, which
is a list of plain mappings — ``print(plan.per_document())`` gives the YAML
files verbatim, and dropping them in a directory reproduces the failure under
the ordinary CLI. The example is also written to ``.hypothesis/examples`` and
re-run first next time; see ``docs/testing.md``.

Every failure these properties have found is *also* pinned as a plain example
below, next to the property that found it, under "Regressions". A property test
proves the class of bug is gone; the example proves this particular one is, and
says out loud what it was.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from netgraph.fmt import format_source
from netgraph.fmt.verify import meaning, verify
from netgraph.loader import Inventory, load_tree, namespace_of
from netgraph.render import RENDERERS, Graph, Layer, RenderOptions, build_graph
from netgraph.render.registry import draws_racks
from netgraph.trace import TraceError, trace
from netgraph.validate import validate

import strategies as ng  # isort: skip -- tests/ is on sys.path, not a package

requires_dot = pytest.mark.skipif(
    shutil.which("dot") is None, reason="the Graphviz 'dot' executable is not installed"
)

#: Layers a topology can be drawn at. Every one of them has to survive every
#: inventory, including the ones where the layer is empty.
LAYERS: Final[tuple[Layer, ...]] = tuple(Layer)

#: The text formats, which need no external binary and can therefore be asserted
#: on in the fast properties.
TEXT_FORMATS: Final[tuple[str, ...]] = tuple(
    name for name, renderer in RENDERERS.items() if renderer.is_text and name != "html"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@contextmanager
def written(
    plan: ng.InventoryPlan, files: Mapping[str, str] | None = None
) -> Iterator[tuple[Path, Inventory]]:
    """Write ``plan`` to a temporary tree and load it.

    A temporary directory rather than pytest's ``tmp_path``: a function-scoped
    fixture is created once and then reused by every example of a property,
    which is exactly the shared mutable state Hypothesis warns about.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plan.write(root, files)
        yield root, load_tree(root)


def emit(element: Any) -> str:
    """One element as the YAML document it would be written as.

    The round-trip property needs an emitter, and netgraph has none: it reads
    inventories and draws them. This is the smallest honest one — the model's
    own JSON-mode dump, which is exactly the set of fields the loader read —
    which makes "load, emit, load again" a test of the model layer rather than
    of a second serialiser written for the occasion.
    """
    return yaml.safe_dump(
        element.model_dump(mode="json", by_alias=True),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


def emit_tree(inventory: Inventory, root: Path) -> None:
    """Write every element of ``inventory`` back out, one file per element."""
    for index, (fqn, element) in enumerate(inventory.elements.items()):
        namespace = namespace_of(fqn)
        directory = root / namespace if namespace else root
        path = directory / f"element{index}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(emit(element), encoding="utf-8")


def assert_loaded(inventory: Inventory) -> None:
    """The generated documents are valid by construction; say so loudly."""
    assert not inventory.errors, [str(error) for error in inventory.errors[:3]]


def capped(limit: int) -> int:
    """At most ``limit`` examples, and fewer when the profile asks for fewer.

    Every property here inherits its budget from the profile
    (``NETGRAPH_HYPOTHESIS_PROFILE``, see ``tests/conftest.py``) — an explicit
    ``max_examples`` on the decorator overrides the profile rather than being
    combined with it, which would make the deep profile a no-op. The two
    Graphviz-backed properties are the exception: each example shells out to
    ``dot``, so they are capped in absolute terms as well.
    """
    # ``settings()`` inherits from the loaded profile, and is typed; the
    # ``settings.default`` attribute is ``settings | None``.
    return min(int(settings().max_examples), limit)


# --------------------------------------------------------------------------- #
# 1. Loader round-trip
# --------------------------------------------------------------------------- #


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans())
def test_loading_an_emitted_inventory_reproduces_the_model(plan: ng.InventoryPlan) -> None:
    """``load(emit(load(x)))`` is ``load(x)``, element for element.

    This is the property that says the model layer loses nothing: every default
    it materialises, every value it normalises and every shorthand it expands
    has to be expressible in a document the loader will read back the same way.
    A field that could be *set* but not *written* would show up here as an
    element that no longer compares equal to itself.
    """
    with written(plan) as (_, first):
        assert_loaded(first)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            emit_tree(first, root)
            second = load_tree(root)

    assert_loaded(second)
    assert set(second.elements) == set(first.elements)
    for fqn, element in first.elements.items():
        assert second.elements[fqn] == element, fqn


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans())
def test_re_emitting_an_inventory_is_byte_identical(plan: ng.InventoryPlan) -> None:
    """Emission is a function of the model, so the second pass changes nothing.

    Separate from the equality property above because it is a strictly stronger
    claim and fails differently: two models can compare equal while emitting
    different bytes — a set that lost its order, a mapping that gained a key —
    and it is the bytes a reviewer sees in a diff.
    """
    with written(plan) as (_, first):
        assert_loaded(first)
        emitted = {fqn: emit(element) for fqn, element in first.elements.items()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            emit_tree(first, root)
            second = load_tree(root)

    assert_loaded(second)
    assert {fqn: emit(element) for fqn, element in second.elements.items()} == emitted


# --------------------------------------------------------------------------- #
# 2. netgraph fmt
# --------------------------------------------------------------------------- #


def _sources(plan: ng.InventoryPlan) -> list[str]:
    """The YAML text of the plan, in each layout, as the formatter sees it."""
    return [text for layout in plan.layouts().values() for text in layout.values()]


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans())
def test_formatting_is_idempotent(plan: ng.InventoryPlan) -> None:
    """``fmt(fmt(x)) == fmt(x)`` for every generated document.

    A formatter that is not idempotent turns every commit into a fight: the
    hook rewrites the file, the next run rewrites it back, and the diff never
    converges.
    """
    for source in _sources(plan):
        once = format_source(source, name="generated.yaml")
        assert format_source(once, name="generated.yaml") == once


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans())
def test_formatting_preserves_meaning(plan: ng.InventoryPlan) -> None:
    """The model loaded from formatted YAML equals the one loaded from the original.

    ``format_source`` already refuses to return output whose *documents* moved
    (:func:`netgraph.fmt.verify.verify`); this goes one layer further and
    compares what the loader and the models make of the two texts, which is the
    thing a user actually loses if the formatter is wrong.
    """
    layout = plan.per_document()
    formatted = {path: format_source(text, name=path) for path, text in layout.items()}
    with written(plan, layout) as (_, before):
        assert_loaded(before)
    with written(plan, formatted) as (_, after):
        assert_loaded(after)
    assert after.elements == before.elements


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(st.data(), ng.inventory_plans())
def test_formatting_comment_bearing_input_preserves_meaning_and_comments(
    data: st.DataObject, plan: ng.InventoryPlan
) -> None:
    """Comments are the half a YAML round trip normally destroys.

    ``fmt`` exists because ``validate`` and ``render`` parse with a loader that
    throws comments away, so the one thing it may never do is throw them away
    too — and the one thing an idempotence test on comment-free input cannot
    see is a comment being re-indented differently on each pass.
    """
    for source in plan.per_document().values():
        commented = data.draw(ng.commented_yaml(source))
        once = format_source(commented, name="generated.yaml")
        assert verify(commented, once, name="generated.yaml") is None
        assert format_source(once, name="generated.yaml") == once
        assert meaning(once, name="generated.yaml") == meaning(commented, name="generated.yaml")


# --------------------------------------------------------------------------- #
# 3. Ranges and templates
# --------------------------------------------------------------------------- #


@given(ng.range_cases())
def test_interface_ranges_equal_their_hand_expansion(case: ng.TemplateCase) -> None:
    """``range:`` is a shorthand, so the longhand must load to the same element.

    The expansion on the right-hand side is built by the strategy, not by
    netgraph: comparing the loader's expansion against the loader's expansion
    would assert only that it is deterministic.
    """
    with written(ng.InventoryPlan(case.written)) as (_, short):
        assert_loaded(short)
    with written(ng.InventoryPlan((case.expanded,))) as (_, long):
        assert_loaded(long)
    assert short.elements == long.elements


@given(ng.template_cases())
def test_template_inheritance_equals_the_hand_merged_document(case: ng.TemplateCase) -> None:
    """``spec.from`` is a shorthand too, under the merge rules of §6.6."""
    with written(ng.InventoryPlan(case.written)) as (_, inherited):
        assert_loaded(inherited)
    with written(ng.InventoryPlan((case.expanded,))) as (_, merged):
        assert_loaded(merged)
    assert inherited.elements == merged.elements


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(ng.template_cases())
def test_template_resolution_does_not_depend_on_document_order(case: ng.TemplateCase) -> None:
    """A template may be declared after the device that inherits it.

    The loader defers a document whose ``from`` it has not seen yet, so the two
    orders — and the two file layouts — must produce the same tree. If they did
    not, an inventory would mean different things depending on how a filesystem
    happened to sort it.
    """
    plan = ng.InventoryPlan(case.written)
    reversed_plan = ng.InventoryPlan(tuple(reversed(case.written)))

    with written(plan) as (_, forwards):
        assert_loaded(forwards)
    with written(reversed_plan) as (_, backwards):
        assert_loaded(backwards)
    with written(plan, {"all.yaml": ng.dump_documents([d.data for d in case.written])}) as (
        _,
        single,
    ):
        assert_loaded(single)
    with written(
        plan, {"all.yaml": ng.dump_documents([d.data for d in reversed(case.written)])}
    ) as (_, single_reversed):
        assert_loaded(single_reversed)

    assert backwards.elements == forwards.elements
    assert single.elements == forwards.elements
    assert single_reversed.elements == forwards.elements


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans())
def test_the_tree_does_not_depend_on_the_file_layout(plan: ng.InventoryPlan) -> None:
    """One document per file, one per namespace or two per file: same elements.

    A file is a container, not a scope. The namespace comes from the *directory*
    (§2.2), so how documents are packed into files below it can change the
    provenance of a diagnostic and nothing else.
    """
    loaded: dict[str, dict[str, Any]] = {}
    for name, layout in plan.layouts().items():
        with written(plan, layout) as (_, inventory):
            assert_loaded(inventory)
            loaded[name] = dict(inventory.elements)

    reference = loaded["per-document"]
    for name, elements in loaded.items():
        assert elements == reference, name


# --------------------------------------------------------------------------- #
# 4. Renderers
# --------------------------------------------------------------------------- #


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans())
def test_every_text_renderer_completes_and_parses(plan: ng.InventoryPlan) -> None:
    """No valid inventory makes a renderer raise, and every output parses.

    "Completes" is half of it. A renderer that emits unparseable output has
    still failed, and the formats that have a real parser available are checked
    with one: JSON with :mod:`json`, Mermaid's front matter with a YAML parser.
    """
    with written(plan) as (_, inventory):
        assert_loaded(inventory)
        for layer in LAYERS:
            graph = build_graph(inventory, layer=layer)
            for name in TEXT_FORMATS:
                if layer is Layer.RACK and not draws_racks(name):
                    continue
                text = RENDERERS[name].text(graph, RenderOptions())
                _assert_parses(name, text, graph)


def _assert_parses(format: str, text: str, graph: Graph) -> None:
    """Hand ``text`` to a real parser for its format.

    "Completes without raising" is the weak half of the property and the easy
    half to satisfy by accident: a renderer that returned the empty string would
    pass it. So each format is also checked against something that can *read*
    it, and against the graph it was rendered from.
    """
    if format == "json":
        document = json.loads(text)
        assert {node["id"] for node in document["nodes"]} == set(graph.nodes)
        assert len(document["edges"]) == len(graph.edges)
    elif format == "mermaid":
        front = _front_matter(text)
        if front is not None:
            assert isinstance(yaml.safe_load(front), dict)
        # Every label is closed. Mermaid has no escape syntax inside one, so an
        # odd number of quotes on a line is a label that swallowed the rest of
        # the statement.
        for line in text.splitlines():
            assert line.count('"') % 2 == 0, line
        assert "flowchart" in text or "graph" in text
    elif format == "dot":
        assert text.lstrip().startswith(("digraph", "graph", "strict"))
        # Balanced braces once every string literal is blanked: the cheap
        # structural check, with ``dot -Tcanon`` doing the real one in
        # ``test_dot_output_is_accepted_by_graphviz``.
        skeleton = _DOT_STRING.sub('""', text)
        assert skeleton.count("{") == skeleton.count("}")


def _front_matter(text: str) -> str | None:
    """The YAML block a Mermaid document may open with, or ``None``."""
    if not text.startswith("---\n"):
        return None
    end = text.index("\n---\n", 3)
    return text[4:end]


@requires_dot
@settings(max_examples=capped(15), suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans())
def test_dot_output_is_accepted_by_graphviz(plan: ng.InventoryPlan) -> None:
    """The DOT source parses — asserted by the program that has to read it.

    ``dot -Tcanon`` is a parse-and-print: it fails on anything the real grammar
    refuses, which is a far stronger claim than "the string starts with
    ``digraph``".
    """
    with written(plan) as (_, inventory):
        assert_loaded(inventory)
        source = RENDERERS["dot"].text(build_graph(inventory, layer=Layer.L2), RenderOptions())
    completed = subprocess.run(
        ["dot", "-Tcanon"],
        input=source,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


@requires_dot
@settings(max_examples=capped(10), suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans(max_devices=3))
def test_the_laid_out_formats_complete_and_parse(plan: ng.InventoryPlan) -> None:
    """SVG is XML and HTML is markup; both are handed to a parser that says so."""
    from xml.etree import ElementTree

    with written(plan) as (_, inventory):
        assert_loaded(inventory)
        graph = build_graph(inventory, layer=Layer.L2)
        svg = RENDERERS["svg"].bytes(graph, RenderOptions())
        page = RENDERERS["html"].text(graph, RenderOptions())

    assert ElementTree.fromstring(svg.decode("utf-8")).tag.endswith("svg")
    assert _tags(page).count("html") == 1


# --------------------------------------------------------------------------- #
# 5. Escaping
# --------------------------------------------------------------------------- #
#
# The shape of every property here is the same, and it is worth stating once.
#
# An escaping bug is not "the output looks wrong"; it is *the output has more
# structure than the graph does*. So each of these renders the same graph twice
# — once with a benign string in every free-text field, once with a payload that
# is syntax in the format under test — and asserts that the two outputs have the
# same skeleton: the same tokens once every string literal is blanked, the same
# tag and attribute sequence, the same parsed JSON shape. A payload that escaped
# its string would change the skeleton; a payload that is merely rendered
# awkwardly would not.


def _payload_documents(text: str) -> ng.InventoryPlan:
    """A fixed two-device inventory with ``text`` in every free-text field."""
    api = "netgraph.dev/v1alpha1"
    switch = {
        "apiVersion": api,
        "kind": "switch",
        "metadata": {
            "name": "sw1",
            "description": text,
            "labels": {"role": text},
        },
        "spec": {
            "vendor": text,
            "model": text,
            "location": text,
            "vlans": [{"id": 10, "name": text[:32]}],
            "interfaces": [
                {
                    "name": "p1",
                    "type": "ethernet",
                    "description": text,
                    "ipv4": {"addresses": ["10.0.0.1/24"]},
                    "vlan": {"mode": "access", "access_vlan": 10},
                }
            ],
        },
    }
    host = {
        "apiVersion": api,
        "kind": "computer",
        "metadata": {"name": "pc1", "description": text},
        "spec": {
            "interfaces": [
                {
                    "name": "eth0",
                    "type": "ethernet",
                    "description": text,
                    "ipv4": {"addresses": ["10.0.0.2/24"]},
                    "vlan": {"mode": "access", "access_vlan": 10},
                }
            ]
        },
    }
    cable = {
        "apiVersion": api,
        "kind": "cable",
        "metadata": {"name": "c1", "description": text},
        "spec": {
            "endpoints": ["sw1:p1", "pc1:eth0"],
            "medium": "copper",
            "label": text,
        },
    }
    return ng.InventoryPlan(
        tuple(
            ng.PlannedDocument(namespace="", stem=stem, data=data)
            for stem, data in (("sw1", switch), ("pc1", host), ("c1", cable))
        )
    )


#: The benign string the payload is compared against. Deliberately boring: it
#: is syntax in nothing, so any structural difference is the payload's doing.
BENIGN: Final = "benign"

#: Element and interface names that *look* like syntax somewhere. Names are far
#: more constrained than free text — an element name is
#: ``[A-Za-z0-9]+([-_.][A-Za-z0-9]+)*`` and an interface name adds only ``/`` —
#: but "constrained" is not "harmless": ``--`` is Graphviz's edge operator,
#: ``a-->b`` is Mermaid's, and every one of these is a legal port name somebody
#: has actually used.
AWKWARD_NAMES: Final[tuple[tuple[str, str], ...]] = (
    ("sw-1", "--"),
    ("sw.1", "a/b"),
    ("sw_1", "-"),
    ("0", "0"),
    ("a.b-c_d", "Gi1/0/1.100"),
    ("sw1", "a-b--c"),
)


def _rendered(text: str, format: str, options: RenderOptions | None = None) -> str:
    with written(_payload_documents(text)) as (_, inventory):
        assert_loaded(inventory)
        graph = build_graph(inventory, layer=Layer.L2)
    return RENDERERS[format].text(graph, options if options is not None else RenderOptions())


#: A DOT string literal: double quotes, backslash escapes.
_DOT_STRING: Final = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)
#: A Mermaid label: double quotes, and no escape syntax at all — which is why
#: the renderer has to use HTML entities rather than backslashes.
_MERMAID_STRING: Final = re.compile(r'"[^"]*"')


def _named(element: str, interface: str) -> ng.InventoryPlan:
    """The payload inventory with one device renamed, and its port with it."""
    plan = _payload_documents(BENIGN)
    documents = []
    for document in plan.documents:
        data = json.loads(json.dumps(document.data))
        if data["kind"] == "switch":
            data["metadata"]["name"] = element
            data["spec"]["interfaces"][0]["name"] = interface
        if data["kind"] == "cable":
            data["spec"]["endpoints"] = [f"{element}:{interface}", "pc1:eth0"]
        documents.append(ng.PlannedDocument(document.namespace, document.stem, data))
    return ng.InventoryPlan(tuple(documents))


@pytest.mark.parametrize(("element", "interface"), AWKWARD_NAMES)
def test_no_name_can_break_out_of_a_rendering(element: str, interface: str) -> None:
    """A name that is syntax elsewhere is still only a name.

    Names get a property of their own because they land where free text does
    not: a DOT node id and the *markup* of a DOT HTML-like label, a Mermaid
    statement, a JSON value. What is asserted is a count rather than the
    skeleton the payload properties compare — a name legitimately changes the
    *order* of things, because both a cable's endpoints and a subnet's members
    are sorted by it, and a diff over ordering would say nothing about escaping.
    A name that became syntax would add a node, an edge or a tag; none of those
    survives a count.
    """
    from xml.etree import ElementTree

    with written(_named(element, interface)) as (_, inventory):
        assert_loaded(inventory)
        graph = build_graph(inventory, layer=Layer.L2)
    options = RenderOptions()
    dot = RENDERERS["dot"].text(graph, options)
    mermaid = RENDERERS["mermaid"].text(graph, options)
    document = json.loads(RENDERERS["json"].text(graph, options))

    # DOT: balanced once every string literal is blanked, and every HTML-like
    # label (``label=<…>``) is well-formed markup. That second one is the check
    # that matters here: a label is the one part of a DOT file where a name is
    # not inside quotes, so an unescaped ``<`` would open a tag.
    skeleton = _DOT_STRING.sub('""', dot)
    assert skeleton.count("{") == skeleton.count("}")
    labels = re.findall(r"label=<(.*?)>\];", dot, re.S)
    assert labels, "no HTML-like label was emitted, so nothing was checked"
    for label in labels:
        ElementTree.fromstring(label.strip())

    # Mermaid: one declaration per node, one statement per edge.
    assert len(re.findall(r"^ +n\d+[\[(/]", mermaid, re.M)) == len(graph.nodes)
    assert len(re.findall(r"^ +n\d+ .*n\d+$", mermaid, re.M)) == len(graph.edges)

    # JSON: the same graph, and the name given back exactly as it was written.
    assert len(document["nodes"]) == len(graph.nodes)
    assert len(document["edges"]) == len(graph.edges)
    assert element in {node["id"] for node in document["nodes"]}
    assert interface in _strings(document)


@requires_dot
@pytest.mark.parametrize(("element", "interface"), AWKWARD_NAMES)
def test_graphviz_accepts_a_rendering_of_awkward_names(element: str, interface: str) -> None:
    """And Graphviz agrees, including with ``--`` as a port name."""
    with written(_named(element, interface)) as (_, inventory):
        assert_loaded(inventory)
        source = RENDERERS["dot"].text(
            build_graph(inventory, layer=Layer.L2), RenderOptions(element_ids=True)
        )
    completed = subprocess.run(
        ["dot", "-Tcanon"], input=source, capture_output=True, text=True, check=False, timeout=60
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("payload", ng.BREAKOUT_PAYLOADS)
def test_no_text_can_break_out_of_a_dot_string(payload: str) -> None:
    """Every payload stays inside its quotes, and Graphviz still parses it."""
    assert _DOT_STRING.sub('""', _rendered(payload, "dot")) == _DOT_STRING.sub(
        '""', _rendered(BENIGN, "dot")
    )


@requires_dot
@pytest.mark.parametrize("payload", ng.BREAKOUT_PAYLOADS)
def test_graphviz_accepts_dot_output_carrying_a_payload(payload: str) -> None:
    """The skeleton check says the tokens match; this says Graphviz agrees."""
    completed = subprocess.run(
        ["dot", "-Tcanon"],
        input=_rendered(payload, "dot"),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("payload", ng.BREAKOUT_PAYLOADS)
def test_no_text_can_break_out_of_a_mermaid_label(payload: str) -> None:
    assert _MERMAID_STRING.sub('""', _rendered(payload, "mermaid")) == _MERMAID_STRING.sub(
        '""', _rendered(BENIGN, "mermaid")
    )


@pytest.mark.parametrize("payload", ng.BREAKOUT_PAYLOADS)
def test_no_title_can_break_out_of_the_mermaid_front_matter(payload: str) -> None:
    """The front matter is read by a YAML parser, so it is asserted with one."""
    text = _rendered(payload, "mermaid", RenderOptions(title=payload))
    front = _front_matter(text)
    assert front is not None
    parsed = yaml.safe_load(front)
    assert isinstance(parsed, dict)
    # A title is collapsed to one line -- the front matter has no room for a
    # second -- but nothing else about it may change.
    assert parsed["title"] == " ".join(payload.split())


@pytest.mark.parametrize("payload", ng.BREAKOUT_PAYLOADS)
def test_no_text_can_break_out_of_a_json_string(payload: str) -> None:
    """The export parses, has the same shape, and gives the payload back intact."""
    document = json.loads(_rendered(payload, "json"))
    benign = json.loads(_rendered(BENIGN, "json"))
    assert _shape(document) == _shape(benign)
    assert payload in _strings(document)


def _shape(value: Any) -> Any:
    """``value`` with every string replaced by its type, so only structure is left."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return "str" if isinstance(value, str) else value


def _strings(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {text for item in value.values() for text in _strings(item)} | {
            text for key in value for text in _strings(key)
        }
    if isinstance(value, list):
        return {text for item in value for text in _strings(item)}
    return {value} if isinstance(value, str) else set()


class _Markup(HTMLParser):
    """The tags and attribute names of a page, as a parser resolves them.

    Deliberately not a regular expression. An escaping hole shows up as *more
    markup than intended*, and only a parser can tell markup from text that
    merely looks like it.
    """

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str]] = []
        self.data: list[str] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend((tag, name) for name, _ in attrs)

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag: str) -> None:
        self.tags.append(f"/{tag}")

    def handle_data(self, data: str) -> None:
        self.data.append(data)


def _tags(source: str) -> list[str]:
    return _Markup(source).tags


@requires_dot
@pytest.mark.parametrize("payload", ng.BREAKOUT_PAYLOADS)
def test_no_text_can_break_out_of_the_html_page(payload: str) -> None:
    """Same tags, same attributes, whatever the inventory says.

    The page carries its records in a ``<script type="application/json">``
    block, which is the one context in HTML where escaping the *JSON* is not
    enough: ``</script>`` inside a string ends the element as far as the HTML
    parser is concerned, whatever the JSON around it says.
    """
    page = _rendered(payload, "html")
    reference = _rendered(BENIGN, "html")
    assert _Markup(page).tags == _Markup(reference).tags
    assert _Markup(page).attributes == _Markup(reference).attributes
    # The record block is JSON *inside* markup, so the JSON has to parse and the
    # element it sits in has to still be one element.
    blocks = _script_payloads(page)
    assert blocks, "the page carries no script block"
    for block in blocks:
        assert "</script" not in block.lower()
        if block.lstrip().startswith("{"):
            assert payload in _strings(json.loads(block))


def _script_payloads(page: str) -> list[str]:
    """The text of every ``<script>`` element, as an HTML parser delimits it.

    A regular expression is the right tool here and only here: what is being
    asserted is precisely where an HTML parser would put the element's *end*,
    which is at the first ``</script`` whatever the content claims.
    """
    return [match.group(1) for match in re.finditer(r"<script\b[^>]*>(.*?)</script>", page, re.S)]


# --------------------------------------------------------------------------- #
# 6. validate
# --------------------------------------------------------------------------- #


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(ng.inventory_plans())
def test_validate_is_a_function_of_the_inventory_alone(plan: ng.InventoryPlan) -> None:
    """The same inventory yields the same findings, whatever the file layout.

    Order included, for a fixed layout: a findings list whose order depended on
    a dictionary iteration or a directory walk would make every golden file and
    every ``--output-format sarif`` diff meaningless.
    """
    per_layout: dict[str, list[tuple[str, str, str]]] = {}
    for name, layout in plan.layouts().items():
        with written(plan, layout) as (_, inventory):
            assert_loaded(inventory)
            first = _findings(validate(inventory))
            # Same tree, same process, twice: the cheapest determinism check
            # there is, and the one that catches a rule that iterates a set.
            assert _findings(validate(inventory)) == first
        with written(plan, layout) as (_, again):
            # A second load of the same layout, in a different directory: the
            # findings may not depend on the path the inventory happens to sit
            # at, only on what is in it.
            assert _findings(validate(again)) == first
        per_layout[name] = first

    reference = sorted(per_layout["per-document"])
    for name, findings in per_layout.items():
        assert sorted(findings) == reference, name


def _findings(findings: Sequence[Any]) -> list[tuple[str, str, str]]:
    """Findings as comparable tuples: the rule, the elements, the message."""
    return [(finding.rule, ",".join(finding.elements), finding.message) for finding in findings]


# --------------------------------------------------------------------------- #
# 7. netgraph path
# --------------------------------------------------------------------------- #


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(st.data(), ng.inventory_plans(min_devices=2))
def test_a_traced_path_only_crosses_edges_the_graph_has(
    data: st.DataObject, plan: ng.InventoryPlan
) -> None:
    """Every hop of a reported route is an edge of the graph it was derived from.

    A trace is the one feature that answers a question about *reachability*, so
    a route it invents is worse than no answer: it says two things are connected
    that are not. Each link carries the ids of the graph edges it stands for, so
    the check is exact rather than approximate.
    """
    with written(plan) as (_, inventory):
        assert_loaded(inventory)
        reachable = sorted(inventory.devices) + sorted(inventory.adapters)
        assume(len(reachable) >= 2)
        source = data.draw(st.sampled_from(reachable))
        destination = data.draw(st.sampled_from(reachable))
        assume(source != destination)

        try:
            result = trace(inventory, source, destination)
        except TraceError:
            # An endpoint that names nothing or too much is a refusal to answer,
            # not an answer; the property is about the answers.
            return

        graphs = {
            layer: build_graph(inventory, layer=layer)
            for layer in {path.layer for path in result.paths}
        }
        for path in result.paths:
            graph = graphs[path.layer]
            edges = {edge.id for edge in graph.edges}
            nodes = set(graph.nodes)
            for waypoint in path.waypoints:
                assert waypoint.element in nodes
            for link in path.links:
                assert set(link.graph_edges) <= edges, link.id
                assert set(link.graph_nodes) <= nodes, link.id


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(st.integers(min_value=0, max_value=4), st.sampled_from(("copper", "fiber")))
def test_a_hand_built_chain_is_always_traceable(switches: int, medium: str) -> None:
    """Two hosts joined through ``n`` switches are reachable for every ``n``.

    The converse of the property above: an edge the graph has must be crossable.
    The chain is built here rather than generated, because the point is that a
    route *exists* — a strategy that produced a disconnected inventory would
    make the assertion vacuous.
    """
    plan = _chain(switches, medium)
    with written(plan) as (_, inventory):
        assert_loaded(inventory)
        result = trace(inventory, "hostA", "hostB")

    assert result.paths, f"no path across {switches} switch(es)"
    best = result.paths[0]
    assert best.elements[0] == "hostA"
    assert best.elements[-1] == "hostB"
    assert best.hops == switches + 1


def _chain(switches: int, medium: str) -> ng.InventoryPlan:
    """``hostA -- sw0 -- sw1 -- ... -- hostB``, all in one VLAN."""
    api = "netgraph.dev/v1alpha1"
    documents: list[ng.PlannedDocument] = []

    def add(stem: str, data: dict[str, Any]) -> None:
        documents.append(ng.PlannedDocument(namespace="", stem=stem, data=data))

    def host(name: str, address: str) -> dict[str, Any]:
        return {
            "apiVersion": api,
            "kind": "computer",
            "metadata": {"name": name},
            "spec": {
                "interfaces": [
                    {
                        "name": "eth0",
                        "type": "ethernet",
                        "ipv4": {"addresses": [address]},
                        "vlan": {"mode": "access", "access_vlan": 10},
                    }
                ]
            },
        }

    add("hostA", host("hostA", "10.0.0.1/24"))
    add("hostB", host("hostB", "10.0.0.2/24"))
    for index in range(switches):
        add(
            f"sw{index}",
            {
                "apiVersion": api,
                "kind": "switch",
                "metadata": {"name": f"sw{index}"},
                "spec": {
                    "interfaces": [
                        {
                            "name": f"p{port}",
                            "type": "ethernet",
                            "vlan": {"mode": "access", "access_vlan": 10},
                        }
                        for port in (1, 2)
                    ]
                },
            },
        )

    hops: list[tuple[str, str]] = []
    if switches == 0:
        hops.append(("hostA:eth0", "hostB:eth0"))
    else:
        hops.append(("hostA:eth0", "sw0:p1"))
        for index in range(switches - 1):
            hops.append((f"sw{index}:p2", f"sw{index + 1}:p1"))
        hops.append((f"sw{switches - 1}:p2", "hostB:eth0"))

    for index, (left, right) in enumerate(hops):
        add(
            f"cbl{index}",
            {
                "apiVersion": api,
                "kind": "cable",
                "metadata": {"name": f"cbl{index}"},
                "spec": {"endpoints": [left, right], "medium": medium},
            },
        )
    return ng.InventoryPlan(tuple(documents))


# --------------------------------------------------------------------------- #
# Regressions
# --------------------------------------------------------------------------- #
#
# One plain example per bug a property above found. The property proves the
# class of bug is gone; the example says out loud what the bug *was*, fails with
# a one-line diff rather than with a shrunk inventory, and keeps working when
# somebody later narrows the strategy that happened to reach it.


def test_a_hash_inside_a_multiline_scalar_is_not_a_comment() -> None:
    """``fmt`` refused a legal file whose scalar continued onto a ``#`` line.

    Found by :func:`test_formatting_comment_bearing_input_preserves_meaning_and_comments`.
    :func:`netgraph.fmt.verify.comments` counted every line starting with ``#``,
    including the second line of a multi-line quoted scalar. Formatting folds
    that scalar onto one line — legitimately, the meaning is identical — and the
    counter then reported a comment as lost and refused the file, with a message
    saying it was a bug in netgraph. It was.
    """
    source = (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n"
        "  name: sw1\n"
        "spec:\n"
        "  location: '\n"
        "# not a comment: this is the second line of the scalar above\n"
        "\n"
        "    '\n"
        "  interfaces:\n"
        "    - name: e0\n"
        "      type: ethernet\n"
    )
    formatted = format_source(source, name="regression.yaml")
    assert meaning(formatted, name="x") == meaning(source, name="x")
    assert format_source(formatted, name="regression.yaml") == formatted


def test_a_real_comment_after_a_block_scalar_is_still_counted() -> None:
    """The other half of the same fix: the scanner's end mark is exclusive.

    A block scalar's end mark sits at column 0 of the line *after* its content,
    which is exactly where a whole-line comment following it starts. Treating
    that line as "inside the scalar" would have made the formatter free to drop
    the one comment most likely to be there.
    """
    source = (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n"
        "  name: sw1\n"
        "  description: |\n"
        "    provisioned by hand:\n"
        "    # apt install lldpd\n"
        "# a real comment, and it has to survive\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: e0\n"
        "      type: ethernet\n"
    )
    formatted = format_source(source, name="regression.yaml")
    assert "# a real comment, and it has to survive" in formatted
    assert "    # apt install lldpd" in formatted


def test_a_duplicate_address_is_reported_in_the_same_order_whatever_the_layout() -> None:
    """``E004``/``W106`` named the two claimants in inventory load order.

    Found by :func:`test_validate_is_a_function_of_the_inventory_alone`. Load
    order is directory order, so merging two documents into one file — or
    renaming one — flipped the order the offenders were listed in, moved the
    ``elements`` list and re-anchored the finding to the other file. A duplicate
    address is symmetric: neither end is the original.
    """
    api = "netgraph.dev/v1alpha1"

    def host(name: str) -> dict[str, Any]:
        return {
            "apiVersion": api,
            "kind": "computer",
            "metadata": {"name": name},
            "spec": {
                "interfaces": [
                    {"name": "eth0", "type": "ethernet", "ipv4": {"addresses": ["10.0.0.1/24"]}}
                ]
            },
        }

    # ``zulu`` first in the plan, so the per-namespace layout declares it first
    # and the per-document layout (sorted by filename) declares ``alpha`` first.
    plan = ng.InventoryPlan(
        (
            ng.PlannedDocument(namespace="", stem="zulu", data=host("zulu")),
            ng.PlannedDocument(namespace="", stem="alpha", data=host("alpha")),
        )
    )

    reports = {}
    for name, layout in plan.layouts().items():
        with written(plan, layout) as (_, inventory):
            assert_loaded(inventory)
            reports[name] = _findings(validate(inventory))

    duplicates = {
        name: [finding for finding in findings if finding[0] == "E004"]
        for name, findings in reports.items()
    }
    assert all(len(found) == 1 for found in duplicates.values()), duplicates
    messages = {found[0] for found in duplicates.values()}
    assert len(messages) == 1, messages
    rule, elements, message = next(iter(messages))
    assert (rule, elements) == ("E004", "alpha,zulu")
    assert "'alpha:eth0', 'zulu:eth0'" in message


def test_a_rack_collision_is_reported_in_the_same_order_whatever_the_layout() -> None:
    """``E025`` named the two occupants of a rack unit in load order.

    The same class of bug as the duplicate address above, found by the same
    property in the deep profile, in a rule far enough away that the first fix
    did not reach it: two things bolted to the same four screw holes, reported
    in the order their documents happened to load. Fixed by ordering the
    occupants bottom-of-the-rack upwards, which is also how an elevation reads.
    """
    api = "netgraph.dev/v1alpha1"

    def racked(name: str) -> dict[str, Any]:
        return {
            "apiVersion": api,
            "kind": "server",
            "metadata": {
                "name": name,
                "location": {"site": "dc1", "rack": "r1", "position": 12, "height": 1},
            },
            "spec": {"interfaces": [{"name": "eth0", "type": "ethernet"}]},
        }

    plan = ng.InventoryPlan(
        (
            ng.PlannedDocument(namespace="", stem="zulu", data=racked("zulu")),
            ng.PlannedDocument(namespace="", stem="alpha", data=racked("alpha")),
        )
    )

    messages = set()
    for layout in plan.layouts().values():
        with written(plan, layout) as (_, inventory):
            assert_loaded(inventory)
            found = [entry for entry in _findings(validate(inventory)) if entry[0] == "E025"]
            assert len(found) == 1
            messages.add(found[0])

    assert len(messages) == 1, messages
    _, elements, message = next(iter(messages))
    assert elements == "alpha,zulu"
    assert "'alpha' and 'zulu' both occupy" in message
