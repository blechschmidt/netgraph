"""Annotations are invisible to everything that is not a renderer (§21).

:mod:`netviz.models.annotation` promises this file exists and asserts each
guarantee separately, because the central claim of §21 is a *negative* one: a
``note``, an ``area`` and a ``legend`` change the picture and nothing else. A
decorative layer inside a file that also generates configuration and answers
"can these two hosts reach each other" is only worth having if it provably
cannot reach the conclusions — otherwise the first review question about any
diagnostic becomes "is that real, or is it the annotations?".

The shape of every assertion below is the same. Each case is a **pair**: one
inventory, and the same inventory with a full complement of annotations beside
it. Two copies of one example, laid out identically under two temporary
directories so that even the inventory's own name is the same, differing in
nothing but the annotation documents. Everything that is not a renderer is then
run over both and the two answers are compared — usually as bytes, because "the
same set of devices" is a much weaker claim than "the same file".

The pairs are deliberately not synthetic. ``campus`` is the shipped example
*as committed*, with its bare twin made by deleting ``annotations.yaml``, so
this module is also the test that the annotations in ``examples/`` stayed inert;
``home-lab`` and ``overlay`` get a complement written into a copy, which is what
covers an adapter, a user, a group and five tunnels.

What is deliberately **not** here: how an annotation is drawn. That is
``tests/test_render_annotations.py`` for the graph backends,
``tests/test_drawio_annotations.py`` for the mxGraph one, and
``tests/test_cli_annotations.py`` for the flag that turns them off.
``tests/test_plan_annotations.py`` owns the anatomy of an annotation *change* —
how it is addressed, what operation it becomes, how a field write is grouped;
what is asserted here is only the neutrality a changeset has to preserve.
"""

from __future__ import annotations

import functools
import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from netviz.annotations import note_anchor
from netviz.console import Console
from netviz.drift import as_json as drift_as_json
from netviz.drift import compare
from netviz.edit import EditSession, SetField
from netviz.export import FORMATS as EXPORT_FORMATS
from netviz.export import ExportContext, ExportOptions, Recorder, export
from netviz.export.config import CONFIG_DIALECTS
from netviz.fmt import format_source
from netviz.importer.draft import Draft, DraftDevice, DraftInterface
from netviz.listing import LISTINGS, SUBJECTS
from netviz.loader import Inventory, load_stream, load_tree
from netviz.models.annotation import ANNOTATION_KINDS
from netviz.plan import ANNOTATION_TYPES, Plan, diff, render_plan, translate, write_plan
from netviz.render import Layer, RenderOptions, build_graph, graph_to_dict, render_text
from netviz.render.annotations import annotation_views
from netviz.report import Options as ReportOptions
from netviz.report import generate as report_generate
from netviz.rules import RULES
from netviz.testing import run_tests
from netviz.testing.report import as_json as verdicts_as_json
from netviz.trace import render_trace, trace, trace_to_dict
from netviz.validate import Severity, has_errors, validate

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
EXAMPLES: Final = REPO_ROOT / "examples"

#: The two rules that are *about* an annotation, and therefore the only two
#: findings a pair of trees is allowed to differ by. Both are warnings; §21.4
#: says why neither can be anything else.
ANNOTATION_RULES: Final[frozenset[str]] = frozenset({"W142", "W143"})

#: The file every pair keeps its annotations in, so the bare twin is made by
#: deleting one path and the annotated one by writing it.
ANNOTATION_FILE: Final = "annotations.yaml"


# --------------------------------------------------------------------------- #
# The pairs
# --------------------------------------------------------------------------- #

#: A full complement of §21 for ``examples/home-lab``: a note about a device, a
#: note about a cable, a note about nothing in particular, an area named
#: element by element, an area said as a query, an area that is a rectangle of
#: canvas, a generated key and a written-out one — and ``views`` on most of
#: them, so no two crowd one drawing.
HOME_LAB_ANNOTATIONS: Final = """\
apiVersion: netviz.dev/v1alpha1
kind: note
metadata:
  name: poe-budget
spec:
  color: '#fef3c7'
  text: |
    The access point is **powered over the cable**. The switch has one PoE
    port, so a second AP needs an injector.
  anchor:
    link: cables/cbl-sw-ap
---
apiVersion: netviz.dev/v1alpha1
kind: note
metadata:
  name: laptop-via-dongle
spec:
  views:
    - l1
  text: |
    The laptop owns no cabled port; the *dongle* does, and `attached_to` is
    what joins the two.
  anchor:
    element: hosts/adp-usb-eth
  geometry:
    x: 420
    y: 320
    width: 220
  leader: true
---
apiVersion: netviz.dev/v1alpha1
kind: note
metadata:
  name: house-rules
spec:
  views:
    - l2
  color: '#e0f2fe'
  text: |
    Guests land in VLAN 20 and see the internet and nothing else.
  geometry:
    x: -360
    y: 640
    width: 220
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata:
  name: hallway
spec:
  views:
    - l1
  color: '#fee2e2'
  label: Hallway cabinet
  members:
    - routers/rtr-home
    - switches/sw-home
  border: solid
  padding: 20.0
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata:
  name: the-desks
spec:
  views:
    - l2
  color: '#ecfdf5'
  label: Desks
  selector:
    namespace: hosts
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata:
  name: upstairs
spec:
  views:
    - l3
  color: '#f1f5f9'
  label: Upstairs
  geometry:
    x: 0.0
    y: -240.0
    width: 900.0
    height: 360.0
  border: dotted
---
apiVersion: netviz.dev/v1alpha1
kind: legend
metadata:
  name: key
spec:
  views:
    - l3
  title: Key
  corner: bottom-right
  auto: layers
---
apiVersion: netviz.dev/v1alpha1
kind: legend
metadata:
  name: media
spec:
  views:
    - physical
  title: Media
  corner: top-left
  entries:
    - label: copper
      color: '#64748b'
      shape: line
    - label: wireless
      color: '#a855f7'
      shape: dotted
      description: association, not a cable
"""

#: The same complement for ``examples/overlay``, where the note about a link is
#: about a **tunnel** rather than a cable — a tunnel is a link too, and an
#: anchor has to reach one.
OVERLAY_ANNOTATIONS: Final = """\
apiVersion: netviz.dev/v1alpha1
kind: note
metadata:
  name: why-nested
spec:
  color: '#fef3c7'
  text: |
    `vx-100` and `gre-mgmt` ride **inside** the IPsec tunnel, which is why
    they carry no keys of their own.
  anchor:
    link: tunnels/wg-mesh
---
apiVersion: netviz.dev/v1alpha1
kind: note
metadata:
  name: one-public-address
spec:
  views:
    - l3
  text: |
    The WAN core is the only element with a *public* address; everything
    else is behind it.
  anchor:
    element: wan/wan-core
  geometry:
    x: 320
    y: 480
    width: 220
  leader: true
---
apiVersion: netviz.dev/v1alpha1
kind: note
metadata:
  name: no-split-tunnel
spec:
  views:
    - overlay
  color: '#e0f2fe'
  text: |
    Nothing here is split-tunnelled: every branch prefix is carried by the
    mesh.
  geometry:
    x: -400
    y: 560
    width: 220
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata:
  name: the-cores
spec:
  views:
    - l1
  color: '#fee2e2'
  label: Routed edge
  members:
    - wan/wan-core
    - sites/hq/rtr-hq
  border: solid
  padding: 20.0
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata:
  name: headquarters
spec:
  views:
    - l2
  color: '#ecfdf5'
  label: HQ
  selector:
    namespace: sites/hq
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata:
  name: the-public-internet
spec:
  views:
    - l3
  color: '#f1f5f9'
  label: The public internet
  geometry:
    x: 0.0
    y: 720.0
    width: 1200.0
    height: 260.0
  border: dotted
---
apiVersion: netviz.dev/v1alpha1
kind: legend
metadata:
  name: key
spec:
  views:
    - overlay
  title: Key
  corner: bottom-right
  auto: layers
---
apiVersion: netviz.dev/v1alpha1
kind: legend
metadata:
  name: encapsulation
spec:
  views:
    - physical
  title: Encapsulation
  corner: top-left
  entries:
    - label: wireguard
      color: '#22c55e'
      shape: line
    - label: ipsec
      color: '#ef4444'
      shape: dashed
      description: carries vxlan and gre
"""


@dataclass(frozen=True)
class Pair:
    """One inventory, twice: without annotations and with them.

    The two roots share a **basename**, because several of the things compared
    below print the inventory's own name — a report page, a diagram title — and
    a pair that differed in its directory name would fail for a reason that has
    nothing to do with §21.
    """

    #: The example the pair is made from.
    name: str
    #: The tree with no annotation document in it at all.
    bare: Path
    #: The same tree, plus a full complement of them.
    annotated: Path
    #: Two elements far enough apart to make ``netviz path`` do some work.
    source: str
    destination: str
    #: A device whose ``metadata.description`` is safe to rewrite, for the
    #: changeset assertions. Named the short way an address is written.
    device: str

    def __str__(self) -> str:  # pragma: no cover - only in a test id
        return self.name


#: ``example -> (annotations or None, source, destination, device)``. ``None``
#: means the example ships its own annotation document and the *bare* twin is
#: the one that has to be made, by deleting it.
CASES: Final[dict[str, tuple[str | None, str, str, str]]] = {
    "campus": (None, "pc-north-01", "pc-south-01", "sw-north-acc-01"),
    "home-lab": (HOME_LAB_ANNOTATIONS, "pc-desk", "srv-nas", "sw-home"),
    "overlay": (OVERLAY_ANNOTATIONS, "pc-branch-a", "srv-hq", "rtr-hq"),
}


@pytest.fixture(scope="session", params=sorted(CASES), ids=sorted(CASES))
def pair(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Pair:
    """Two copies of one example, differing only in ``annotations.yaml``."""
    name = str(request.param)
    annotations, source, destination, device = CASES[name]
    root = tmp_path_factory.mktemp(f"pair-{name}")

    bare = root / "without" / name
    annotated = root / "with" / name
    shutil.copytree(EXAMPLES / name, bare)
    shutil.copytree(EXAMPLES / name, annotated)

    if annotations is None:
        # The example ships the complement; the bare twin loses it.
        (bare / ANNOTATION_FILE).unlink()
    else:
        (annotated / ANNOTATION_FILE).write_text(annotations, encoding="utf-8")

    return Pair(
        name=name,
        bare=bare,
        annotated=annotated,
        source=source,
        destination=destination,
        device=device,
    )


@functools.cache
def inventory(root: Path) -> Inventory:
    """Load a tree once per path and insist it parsed cleanly."""
    loaded = load_tree(root)
    assert loaded.errors == [], "\n".join(str(error) for error in loaded.errors)
    return loaded


def annotations_of(root: Path) -> tuple[tuple[str, str, Any], ...]:
    return tuple(inventory(root).annotations)


# --------------------------------------------------------------------------- #
# The pairs really are a pair
# --------------------------------------------------------------------------- #


def test_the_bare_twin_carries_no_annotation_at_all(pair: Pair) -> None:
    """Every claim below is vacuous if the two halves are the same tree."""
    assert annotations_of(pair.bare) == ()
    assert annotations_of(pair.annotated) != ()


def test_the_annotated_twin_exercises_every_shape_section_21_allows(pair: Pair) -> None:
    """A complement is not "some annotations": it is one of each written form.

    Asserted per pair rather than once, so that the shipped
    ``examples/campus/annotations.yaml`` is held to the same standard as the
    two written here — an example that quietly lost its selector would stop
    being the thing a reader is pointed at.
    """
    rich = inventory(pair.annotated)
    notes = tuple(rich.notes.values())
    areas = tuple(rich.areas.values())
    legends = tuple(rich.legends.values())

    assert any(
        note.spec.anchor is not None and note.spec.anchor.element is not None for note in notes
    ), "no note is anchored to an element"
    assert any(note.spec.anchor is not None and note.spec.anchor.link is not None for note in notes)
    assert any(
        note.spec.anchor is None and note.spec.geometry is not None and note.spec.geometry.placed
        for note in notes
    ), "no note is placed by geometry alone"
    assert any(note.spec.color is not None for note in notes)
    assert any(note.spec.anchor is not None and note.spec.leader for note in notes)

    assert any(area.spec.members for area in areas)
    assert any(
        area.spec.selector is not None and area.spec.selector.namespace is not None
        for area in areas
    )
    assert any(
        area.spec.geometry is not None and area.spec.geometry.placed and area.spec.geometry.sized
        for area in areas
    )

    assert any(legend.spec.auto == "layers" for legend in legends)
    assert any(legend.spec.entries for legend in legends)

    assert any(annotation.spec.views for _, _, annotation in rich.annotations), (
        "nothing is scoped with spec.views"
    )
    assert {kind for kind, _, _ in rich.annotations} == set(ANNOTATION_KINDS)


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


def finding_keys(findings: Sequence[Any]) -> list[tuple[str, str, str, tuple[str, ...]]]:
    """A finding without its ``source``, which names a file and so a directory.

    The two halves of a pair live under different temporary roots, so the path
    a finding was found at is the one thing that legitimately differs. Rule,
    severity, sentence and blamed elements are not.
    """
    return [
        (finding.rule, finding.severity.value, finding.message, finding.elements)
        for finding in findings
    ]


#: One annotation document per way of being stale, each naming something no
#: example declares. Appended to the annotated twin, they are the *only* thing
#: that may add a finding, and the finding they add is named beside them.
STALE: Final[dict[str, tuple[str, str]]] = {
    "note-about-a-ghost": (
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: note\n"
        "metadata: {name: nv-stale-note}\n"
        "spec:\n"
        "  text: about a machine that was skipped\n"
        "  anchor: {element: nv-no-such-element}\n",
        "W142",
    ),
    "member-that-is-gone": (
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: area\n"
        "metadata: {name: nv-stale-area}\n"
        "spec:\n"
        "  members: [nv-no-such-element]\n",
        "W142",
    ),
    "selector-that-matches-nothing": (
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: area\n"
        "metadata: {name: nv-empty-area}\n"
        "spec:\n"
        "  selector: {namespace: nv-no-such-namespace}\n",
        "W143",
    ),
}


def test_validation_says_exactly_the_same_thing_about_both(pair: Pair) -> None:
    """Rule for rule, sentence for sentence, element for element.

    Not "neither has errors": the *whole* diagnosis has to be the same, because
    a note that suppressed somebody else's warning would be as bad as one that
    raised its own.
    """
    plain = finding_keys(validate(inventory(pair.bare)))
    rich = finding_keys(validate(inventory(pair.annotated)))
    assert [entry for entry in rich if entry[0] not in ANNOTATION_RULES] == plain
    assert [entry for entry in rich if entry[0] in ANNOTATION_RULES] == []


@pytest.mark.parametrize("case", sorted(STALE))
def test_a_stale_annotation_adds_its_own_warning_and_nothing_else(
    pair: Pair, case: str, tmp_path: Path
) -> None:
    document, rule = STALE[case]
    tree = tmp_path / pair.name
    shutil.copytree(pair.annotated, tree)
    (tree / "stale.yaml").write_text(document, encoding="utf-8")

    findings = validate(load_tree(tree))
    baseline = finding_keys(validate(inventory(pair.bare)))
    added = [entry for entry in finding_keys(findings) if entry not in baseline]
    assert [entry[0] for entry in added] == [rule]
    assert not has_errors(findings), "an annotation raised an error"


def test_no_rule_about_an_annotation_can_fail_a_build() -> None:
    """§21.4 in the catalogue: both annotation rules are warnings.

    The severity is what makes a stale note survivable, so it is asserted
    against :data:`netviz.rules.RULES` rather than inferred from an example
    that happens not to have one.
    """
    by_id = {rule.id: rule for rule in RULES}
    assert set(by_id) >= ANNOTATION_RULES
    for name in sorted(ANNOTATION_RULES):
        assert by_id[name].severity is not Severity.ERROR
        assert by_id[name].severity is Severity.WARNING


# --------------------------------------------------------------------------- #
# The graph, at every layer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("layer", list(Layer), ids=[layer.value for layer in Layer])
def test_the_graph_is_identical_at_every_layer(pair: Pair, layer: Layer) -> None:
    """No annotation contributes a node or an edge to any of the nine views.

    Iterated over :class:`~netviz.render.Layer` rather than over a chosen few,
    because the layers differ in what they *build* — a subnet, a VRF, a rack
    slot — and an annotation leaking into one of the derived ones is exactly
    the failure a spot check would miss.
    """
    plain = build_graph(inventory(pair.bare), layer=layer)
    rich = build_graph(inventory(pair.annotated), layer=layer)

    assert list(plain.nodes) == list(rich.nodes)
    assert plain.nodes == rich.nodes
    assert plain.edges == rich.edges
    assert plain.dangling == rich.dangling
    assert not plain.annotations, "the bare twin grew an annotation"


def test_the_annotations_reach_the_drawing_at_several_layers(pair: Pair) -> None:
    """The control for the sweep above.

    ``spec.views`` means no annotation is in every view, so this cannot be
    asserted layer by layer — but a complement that reached *no* drawing, a
    misspelt view name say, would make every equality above hold for the wrong
    reason.
    """
    carried = {
        layer.value
        for layer in Layer
        if build_graph(inventory(pair.annotated), layer=layer).annotations
    }
    assert len(carried) >= 4, carried


@pytest.mark.parametrize("layer", list(Layer), ids=[layer.value for layer in Layer])
def test_the_json_export_differs_only_in_its_annotations_key(pair: Pair, layer: Layer) -> None:
    """``-f json`` is the contract a consumer reads; ``annotations`` is additive."""
    options = RenderOptions()
    plain = graph_to_dict(build_graph(inventory(pair.bare), layer=layer), options)
    rich = graph_to_dict(build_graph(inventory(pair.annotated), layer=layer), options)

    assert "annotations" not in plain, "a document with no annotations grew the key"
    assert {key: value for key, value in rich.items() if key != "annotations"} == plain


# --------------------------------------------------------------------------- #
# netviz path
# --------------------------------------------------------------------------- #


def test_a_traced_path_is_identical(pair: Pair) -> None:
    """Nothing an annotation says can move a hop, add one, or reroute."""
    plain = trace(inventory(pair.bare), pair.source, pair.destination)
    rich = trace(inventory(pair.annotated), pair.source, pair.destination)

    assert plain.paths, f"{pair.source} -> {pair.destination} found no route to compare"
    assert trace_to_dict(plain, all_paths=True) == trace_to_dict(rich, all_paths=True)
    assert render_trace(plain, "text") == render_trace(rich, "text")


# --------------------------------------------------------------------------- #
# netviz plan / apply
# --------------------------------------------------------------------------- #


def annotate_only(plan: Plan) -> bool:
    return all(change.address.type in ANNOTATION_TYPES for change in plan)


def rewrite_a_description(root: Path, device: str) -> None:
    """One ordinary infrastructure change, so a plan has something to say."""
    session = EditSession(root=root)
    session.apply_all(
        [SetField(address=device, path="metadata.description", value="reviewed 2026-08-15")]
    )
    session.commit()


def test_a_plan_between_the_twins_is_annotations_and_nothing_else(pair: Pair) -> None:
    plan = diff(inventory(pair.bare), inventory(pair.annotated))
    assert not plan.empty
    assert annotate_only(plan)
    assert {str(change.address).split(".", 1)[0] for change in plan} <= set(ANNOTATION_KINDS)


def written_plan(plan: Plan, capsys: pytest.CaptureFixture[str]) -> str:
    """The changeset as ``netviz plan`` prints it for a person to read."""
    capsys.readouterr()
    write_plan(Console(), plan, verbose=True)
    return capsys.readouterr().out


def test_an_infrastructure_diff_is_byte_identical_with_and_without_annotations(
    pair: Pair, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The strongest form of the claim, in both of the plan's forms.

    The same edit is made to both halves of the pair and both changesets are
    rendered. Because the annotations are unchanged on either side of the
    annotated diff, the two plans are not merely equivalent — they are the same
    text, field changes, sources and ordering included.
    """
    plain_after = tmp_path / "without" / pair.name
    rich_after = tmp_path / "with" / pair.name
    shutil.copytree(pair.bare, plain_after)
    shutil.copytree(pair.annotated, rich_after)
    rewrite_a_description(plain_after, pair.device)
    rewrite_a_description(rich_after, pair.device)

    plain = diff(inventory(pair.bare), load_tree(plain_after))
    rich = diff(inventory(pair.annotated), load_tree(rich_after))

    assert not plain.empty, "the edit produced no plan to compare"
    assert not annotate_only(plain), "the edit touched no infrastructure"
    assert render_plan(rich, "json") == render_plan(plain, "json")
    assert written_plan(rich, capsys) == written_plan(plain, capsys)


def test_applying_an_annotation_plan_rewrites_no_element_document(
    pair: Pair, tmp_path: Path
) -> None:
    """``netviz apply`` writes the picture without touching the network.

    A changeset that added the annotations to the bare tree has to reach the
    same state as the annotated one, and it has to get there without rewriting
    a single byte of a document that declares an element.
    """
    tree = tmp_path / pair.name
    shutil.copytree(pair.bare, tree)
    before = {
        path.relative_to(tree): path.read_bytes()
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }

    plan = diff(load_tree(tree), inventory(pair.annotated))
    assert annotate_only(plan)
    session = EditSession(root=tree)
    for _, operations in translate(plan, session):
        assert operations
    session.commit()

    after = {
        path.relative_to(tree): path.read_bytes()
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }
    assert {name: text for name, text in after.items() if name in before} == before
    assert diff(load_tree(tree), inventory(pair.annotated)).empty


# --------------------------------------------------------------------------- #
# Everything that is not a renderer
# --------------------------------------------------------------------------- #


def config_context(inventory_: Inventory) -> Callable[[Recorder], ExportContext]:
    def factory(recorder: Recorder) -> ExportContext:
        return ExportContext(
            inventory=inventory_,
            graphs={layer: build_graph(inventory_, layer=layer) for layer in Layer},
            options=ExportOptions(),
            recorder=recorder,
        )

    return factory


def exported(root: Path, export_format: str) -> tuple[str, Any, list[tuple[str, str]]]:
    """One export, whole: the artefact, what it declined to write, and its tree.

    The manifest is compared as well as the payload, because "nothing came out
    differently" and "nothing was *skipped* differently" are two claims, and an
    annotation that made an exporter decline a device would satisfy the first.
    """
    result = export(export_format, config_context(inventory(root)))
    files = sorted(result.bundle.files()) if result.bundle is not None else []
    return (result.payload, result.manifest, files)


#: ``drawio`` is the one export that is a *drawing*: §21 exists so that a note
#: survives a round trip through it, so it is expected to differ and is asserted
#: to, rather than quietly skipped.
DRAWING_FORMATS: Final[frozenset[str]] = frozenset({"drawio"})


@pytest.mark.parametrize("export_format", sorted(set(EXPORT_FORMATS) - DRAWING_FORMATS))
def test_every_export_is_byte_identical(pair: Pair, export_format: str) -> None:
    """Including every configuration dialect: nothing a device would run.

    ``netplan``, ``networkd``, ``ifupdown``, ``frr``, ``wireguard`` and
    ``interfaces`` are in this sweep by name, which is what
    :func:`test_no_configuration_dialect_escapes_the_sweep` keeps true.
    """
    assert exported(pair.bare, export_format) == exported(pair.annotated, export_format)


def test_no_configuration_dialect_escapes_the_sweep() -> None:
    assert set(CONFIG_DIALECTS) <= set(EXPORT_FORMATS)
    assert not set(CONFIG_DIALECTS) & DRAWING_FORMATS


def test_some_export_actually_wrote_something(pair: Pair) -> None:
    """Byte equality between two empty lists would prove nothing."""
    written = {
        export_format: exported(pair.annotated, export_format)[0]
        for export_format in sorted(set(EXPORT_FORMATS) - DRAWING_FORMATS)
    }
    assert [name for name, payload in written.items() if payload]
    assert any(name in CONFIG_DIALECTS for name, payload in written.items() if payload), (
        "no configuration dialect wrote a file for this inventory"
    )


def test_the_drawing_export_is_the_one_that_does_differ(pair: Pair) -> None:
    """The control for the sweep above: a renderer *must* see them."""
    assert exported(pair.bare, "drawio") != exported(pair.annotated, "drawio")


def a_capture_of(inventory_: Inventory) -> Draft:
    """One observed interface on the pair's device, for ``netviz drift``.

    Hand-built rather than captured, and deliberately wrong about the MTU, so
    the comparison has a difference to report and the report has content to be
    identical about.
    """
    name = next(iter(sorted(inventory_.devices)))
    device = DraftDevice(name=inventory_.devices[name].metadata.name, sources=["capture"])
    port = next(iter(inventory_.devices[name].interface_names()))
    device.add_interface(DraftInterface(name=port, type="ethernet", mtu=1234))
    return Draft(devices={device.name: device}, dialects={"capture": "iproute"})


def without_the_root(document: dict[str, Any]) -> dict[str, Any]:
    """A report minus wherever it was run, which is a temporary directory here."""
    stripped = dict(document)
    stripped.pop("inventory", None)
    stripped.pop("root", None)
    return stripped


def listing(subject: str) -> Callable[[Inventory], Any]:
    def read(inventory_: Inventory) -> Any:
        result = LISTINGS[subject](inventory_)
        return (result.headers, result.rows, result.records)

    return read


def read_the_report(inventory_: Inventory) -> Any:
    """The pages ``netviz report`` writes, with the clock pinned.

    ``diagrams=False`` on purpose: a diagram *is* a drawing, and a drawing is
    the one thing an annotation is allowed to change. What is asserted here is
    that every page of prose around them is unchanged.
    """
    bundle, _ = report_generate(
        inventory_,
        options=ReportOptions(
            diagrams=False,
            generated_at="1970-01-01T00:00:00Z",
            revision="",
            version="0",
        ),
    )
    return sorted(bundle.files)


def read_the_test_run(inventory_: Inventory) -> Any:
    return without_the_root(verdicts_as_json(run_tests(inventory_)))


def read_the_drift(inventory_: Inventory) -> Any:
    return without_the_root(drift_as_json(compare(inventory_, a_capture_of(inventory_))))


#: Everything that answers a question about the network rather than drawing it.
#: One table so the sweep is broad and the assertion is one line; a reader
#: adding a command should add it here rather than write a test of their own.
READERS: Final[dict[str, Callable[[Inventory], Any]]] = {
    **{f"list {subject}": listing(subject) for subject in SUBJECTS},
    "report": read_the_report,
    "test": read_the_test_run,
    "drift": read_the_drift,
}


@pytest.mark.parametrize("reader", sorted(READERS))
def test_a_reader_of_the_inventory_cannot_tell(pair: Pair, reader: str) -> None:
    read = READERS[reader]
    assert read(inventory(pair.bare)) == read(inventory(pair.annotated))


def test_the_readers_table_covers_every_listing() -> None:
    """A new ``netviz list`` subject must not slip past this module."""
    assert {f"list {subject}" for subject in SUBJECTS} <= set(READERS)


# --------------------------------------------------------------------------- #
# netviz fmt
# --------------------------------------------------------------------------- #


def test_the_formatter_leaves_a_canonical_annotation_document_alone() -> None:
    """The shipped complement is already in the canonical form, and stays there."""
    path = EXAMPLES / "campus" / ANNOTATION_FILE
    text = path.read_text(encoding="utf-8")
    assert format_source(text, name=path.name) == text


#: One of each kind, written the way somebody actually writes one: keys in the
#: order they were thought of, a double-quoted colour, a flow mapping, an
#: upper-case hex colour. Nothing here is wrong — it is simply not canonical.
UNTIDY: Final = """\
apiVersion: netviz.dev/v1alpha1
kind: note
spec:
  anchor: {element: sw-a}
  leader: false
  color: "#FEF3C7"
  text: about the switch
metadata:
  name: untidy-note
  labels: {owner: nobody}
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata: {name: untidy-area}
spec:
  padding: 8
  members: [sw-a]
  label: A box
  views: [l2, l1]
---
apiVersion: netviz.dev/v1alpha1
kind: legend
spec:
  entries:
    - {shape: line, label: copper, color: "#64748B"}
  corner: top-left
metadata: {name: untidy-legend}
"""


@pytest.mark.parametrize("case", sorted(CASES))
def test_formatting_an_annotation_document_is_idempotent(case: str) -> None:
    """Twice through the formatter is once through it, for all three kinds."""
    annotations = CASES[case][0]
    if annotations is None:
        annotations = (EXAMPLES / case / ANNOTATION_FILE).read_text(encoding="utf-8")
    once = format_source(annotations, name=ANNOTATION_FILE)
    assert format_source(once, name=ANNOTATION_FILE) == once
    assert load_stream(once).errors == []


def test_an_untidy_annotation_document_reaches_the_canonical_form_and_stays() -> None:
    """And says the same thing afterwards, which is the point of a formatter.

    Idempotence over text the formatter has already produced is no property at
    all, so the input here is deliberately not canonical in any respect the
    canonical form has an opinion about.
    """
    once = format_source(UNTIDY, name=ANNOTATION_FILE)
    assert once != UNTIDY, "nothing about this document was reordered"
    assert format_source(once, name=ANNOTATION_FILE) == once
    assert tuple(load_stream(once).annotations) == tuple(load_stream(UNTIDY).annotations)
    assert tuple(load_stream(once).annotations) != ()


# --------------------------------------------------------------------------- #
# A stale annotation is never fatal
# --------------------------------------------------------------------------- #

#: Three cabled elements, a spare with nothing plugged into it, and three
#: annotations about the spare. Deleting one document is then a realistic
#: decommissioning rather than a hand-written dangling reference.
DECOMMISSIONABLE: Final = """\
apiVersion: netviz.dev/v1alpha1
kind: switch
metadata: {name: sw-a}
spec:
  interfaces: [{name: port1, type: ethernet, ipv4: [10.0.0.1/24]}]
---
apiVersion: netviz.dev/v1alpha1
kind: server
metadata: {name: srv-b}
spec:
  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.2/24]}]
---
apiVersion: netviz.dev/v1alpha1
kind: cable
metadata: {name: cbl-a-b}
spec: {endpoints: [sw-a:port1, srv-b:eth0], medium: copper}
"""

#: The document that goes away.
SPARE: Final = """\
---
apiVersion: netviz.dev/v1alpha1
kind: server
metadata: {name: srv-spare, labels: {role: spare}}
spec:
  interfaces: [{name: eth0, type: ethernet, enabled: false}]
"""

#: What was written about it, and outlives it.
ABOUT_THE_SPARE: Final = """\
---
apiVersion: netviz.dev/v1alpha1
kind: note
metadata: {name: about-the-spare}
spec:
  text: The spare is **cold**; it is racked and not patched.
  anchor: {element: srv-spare}
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata: {name: the-spares}
spec:
  label: Spares
  members: [srv-spare]
---
apiVersion: netviz.dev/v1alpha1
kind: area
metadata: {name: by-role}
spec:
  label: Spare by label
  selector: {labels: {role: spare}}
"""


@pytest.fixture(scope="module")
def decommissioned() -> Inventory:
    """The tree after the spare's document was deleted, annotations and all."""
    return load_stream(DECOMMISSIONABLE + ABOUT_THE_SPARE, name="decommissioned.yaml")


@pytest.fixture(scope="module")
def still_there() -> Inventory:
    return load_stream(DECOMMISSIONABLE + SPARE + ABOUT_THE_SPARE, name="whole.yaml")


def test_nothing_is_said_about_the_annotations_while_the_spare_is_there(
    still_there: Inventory,
) -> None:
    """The baseline: while the spare exists, its three annotations are silent."""
    assert still_there.errors == []
    findings = validate(still_there)
    assert not has_errors(findings)
    assert not {finding.rule for finding in findings} & ANNOTATION_RULES


def test_deleting_the_element_a_note_is_about_is_not_fatal(
    decommissioned: Inventory, still_there: Inventory
) -> None:
    """Deleting a server must not stop ``netviz validate``.

    Three warnings, one per stale reference, and not one error — because
    somebody once wrote a note about it is not a reason to fail a build.
    """
    assert decommissioned.errors == []
    findings = validate(decommissioned)
    assert not has_errors(findings)

    about_annotations = [finding for finding in findings if finding.rule in ANNOTATION_RULES]
    assert sorted(finding.rule for finding in about_annotations) == ["W142", "W142", "W143"]
    assert {finding.severity for finding in about_annotations} == {Severity.WARNING}
    assert {finding.elements for finding in about_annotations} == {
        ("about-the-spare",),
        ("the-spares",),
        ("by-role",),
    }


@pytest.mark.parametrize("text_format", ["dot", "mermaid", "json"])
def test_a_stale_annotation_still_renders(decommissioned: Inventory, text_format: str) -> None:
    graph = build_graph(decommissioned, layer=Layer.L1)
    assert render_text(graph, text_format)


@requires_dot
def test_a_stale_annotation_still_lays_out(decommissioned: Inventory) -> None:
    from netviz.render.dot import to_image

    drawing = to_image(build_graph(decommissioned, layer=Layer.L1), format="svg")
    assert drawing.decode("utf-8").rstrip().endswith("</svg>")


def test_what_is_left_of_a_stale_annotation(decommissioned: Inventory) -> None:
    """§21.4: the dead reference is dropped, the rest of the annotation is not.

    So a note keeps its words and loses its leader, an area keeps the members
    that are still there, and an area left with nothing at all is not drawn.
    """
    graph = build_graph(decommissioned, layer=Layer.L1)
    note = decommissioned.notes["about-the-spare"]
    assert note.spec.anchor is not None, "the document still says what it is about"
    assert note_anchor(decommissioned, "about-the-spare", note) is None

    views = annotation_views(graph, RenderOptions())
    (drawn,) = views.notes
    assert drawn.fqn == "about-the-spare"
    assert "cold" in drawn.text
    assert drawn.anchor == ""
    assert not drawn.leader, "a leader was drawn to something that is gone"

    assert [area.fqn for area in views.areas] == [], "an empty box was drawn"
    assert graph.annotation_targets["the-spares"] == ()
    assert graph.annotation_targets["by-role"] == ()


def test_the_topology_is_untouched_by_the_stale_annotations(
    decommissioned: Inventory, still_there: Inventory
) -> None:
    """And the elements that remain are drawn exactly as they were.

    The comparison is against the *whole* tree minus the spare, which is what
    makes it a statement about the annotations rather than about the deletion.
    """
    whole = build_graph(still_there, layer=Layer.L1)
    left = build_graph(decommissioned, layer=Layer.L1)
    assert set(whole.nodes) - set(left.nodes) == {"srv-spare"}
    assert left.edges == whole.edges
    assert (
        json.loads(render_text(left, "json"))["edges"]
        == (json.loads(render_text(whole, "json"))["edges"])
    )
