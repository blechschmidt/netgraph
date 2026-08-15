"""The ``.drawio`` envelope: reading one, and writing one back.

draw.io stores a diagram as an ``<mxfile>`` holding one ``<diagram>`` per tab,
and the diagram itself in one of two encodings:

**Plain.** The ``<mxGraphModel>`` sits inside the ``<diagram>`` as XML. This is
what netgraph writes by default, because a diagram that is text is a diagram
that reviews, diffs and merges — which is the whole argument for keeping the
YAML as the source of truth in the first place.

**Compressed.** The ``<diagram>`` holds a single text node:
``base64(raw-deflate(uri-encode(<mxGraphModel>…)))``. It is what the draw.io
desktop app writes by default, so anything that refuses to read it refuses most
of the files it will be handed. :func:`decode_diagram` reads both, deciding by
what is in the element rather than by the file's extension.

Neither encoding is a container format netgraph invented, so both are handled
conservatively:

* **No external entities, no DTD.** A ``<!DOCTYPE`` or ``<!ENTITY`` declaration
  is refused outright rather than parsed, which closes the entity-expansion
  attacks that every stdlib XML parser is open to. netgraph is going to be
  pointed at a file a third party sent, so this is not hypothetical.
* **Bounded.** The text, and the inflated size of a compressed diagram, are both
  capped. A 40:1 deflate ratio on a 10 MB file is not a diagram.
* **Forgiving about the namespace.** The ``netgraph`` prefix is declared on the
  root element, but a re-serialising editor may not carry a declaration it does
  not understand. When it is missing, it is put back before parsing rather than
  the file being refused — an unbound prefix is a spelling problem, not a
  corrupted diagram.
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib
from collections.abc import Iterable, Iterator, Mapping
from typing import Final
from urllib.parse import quote, unquote
from xml.etree import ElementTree

from netgraph import __version__
from netgraph.drawio.identity import (
    ATTR_ANNOTATED,
    ATTR_ORIGIN_X,
    ATTR_ORIGIN_Y,
    ATTR_ROLE,
    ATTR_SCOPE,
    ATTR_VERSION,
    ATTR_VIEW,
    MODEL_VERSION,
    NAMESPACE_PREFIX,
    NAMESPACE_URI,
    CellRole,
    Scope,
)
from netgraph.drawio.model import ROOT_ID, Cell, Diagram, Frame
from netgraph.errors import NetgraphError, clip_text

__all__ = [
    "AGENT",
    "MAX_DOCUMENT_BYTES",
    "MAX_INFLATED_BYTES",
    "DrawioFormatError",
    "decode_diagram",
    "encode_diagram",
    "parse_mxfile",
    "write_mxfile",
]

#: What goes in ``<mxfile agent="…">``: which netgraph wrote the file, which is
#: the first question when a stale diagram turns up.
#:
#: Spelled here rather than imported from
#: :data:`netgraph.export.header.GENERATOR`, which says the same words, because
#: the dependency has to run one way: the export package knows about the wire
#: format, and the wire format must not know about the export package —
#: otherwise importing either one imports both, which is a cycle, and a test
#: module that reaches for :mod:`netgraph.drawio` first cannot import it at all.
#: ``tests/test_drawio.py`` asserts the two strings are equal, so they cannot
#: drift apart in silence.
AGENT: Final = f"netgraph {__version__}"


class DrawioFormatError(NetgraphError):
    """The file is not a draw.io diagram netgraph can read.

    Shares its exit status with the other "could not parse what you pointed me
    at" refusals, which is what a caller branching on the exit code wants: the
    distinction between a bad YAML tree and a bad diagram is in the message.
    """

    exit_code = 3


#: Ceiling on one ``.drawio`` file. A diagram of a large estate is a few
#: megabytes; past this it is an archive, a log or a mistake, and reading it
#: into memory to find that out helps nobody. Mirrors the importer's own bound.
MAX_DOCUMENT_BYTES: Final = 32 * 1024 * 1024

#: Ceiling on what one compressed ``<diagram>`` may inflate to. Deflate reaches
#: about 1000:1 on repetitive input, so an unbounded inflate of a 1 MB file is a
#: denial of service with a ``.drawio`` extension.
MAX_INFLATED_BYTES: Final = 64 * 1024 * 1024

#: The window size that selects raw deflate — no zlib header, no checksum —
#: which is what the JavaScript ``pako.deflateRaw`` draw.io uses produces.
_RAW_DEFLATE: Final = -15

#: What ``encodeURIComponent`` leaves alone beyond Python's own unreserved set.
#: Together they are exactly its output alphabet, which matters: draw.io decodes
#: with ``decodeURIComponent`` and a differently-escaped payload is a different
#: string.
_URI_SAFE: Final = "!~*'()"

#: A declaration or processing instruction that makes a parser fetch or expand
#: something. Refused before the parser sees it.
_UNSAFE_DECLARATION: Final = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

#: The start tag of the root element, for putting a dropped namespace back.
_ROOT_TAG: Final = re.compile(r"<([A-Za-z_][\w.-]*)((?:\s+[^<>]*?)?)(/?)>")

#: How the netgraph attributes reach ElementTree once the prefix is bound.
_NAMESPACE_MARKER: Final = f"{{{NAMESPACE_URI}}}"

#: mxGraph's model root ids: ``0`` is the model, ``1`` its default layer.
_MODEL_ROOT_ID: Final = "0"

#: Attributes of ``<mxGraphModel>``. Fixed values, deliberately: they are page
#: setup rather than content, and varying them would make two exports of one
#: inventory differ. A4-ish at 100%, grid on, which is what draw.io opens with.
_MODEL_ATTRIBUTES: Final[tuple[tuple[str, str], ...]] = (
    ("dx", "1422"),
    ("dy", "794"),
    ("grid", "1"),
    ("gridSize", "10"),
    ("guides", "1"),
    ("tooltips", "1"),
    ("connect", "1"),
    ("arrows", "1"),
    ("fold", "1"),
    ("page", "1"),
    ("pageScale", "1"),
    ("pageWidth", "850"),
    ("pageHeight", "1100"),
    ("math", "0"),
    ("shadow", "0"),
)

#: The order identity attributes are written in. Fixed so the export is
#: byte-stable and so a reviewer meets them in the order they explain each
#: other: what this is, then what it stands for, then where it was.
_ATTRIBUTE_ORDER: Final[tuple[str, ...]] = (
    "role",
    "kind",
    "name",
    "node",
    "link",
    "document",
    "hash",
    "source",
    "target",
    "sourcePort",
    "targetPort",
    "view",
    "scope",
    "version",
    "generator",
    "placed",
    "x",
    "y",
    "waypoints",
    "routing",
    "originX",
    "originY",
    "annotated",
    # The three an annotation carries and nothing else does, last so that adding
    # them left every cell that predates §21 byte-identical.
    "label",
    "text",
    "width",
    "height",
)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def write_mxfile(diagram: Diagram, *, compress: bool = False) -> str:
    """``diagram`` as a complete ``.drawio`` document.

    Args:
        diagram: The model to write. Its metadata cell is emitted first, so the
            identity of the file is the first thing in it.
        compress: Write the deflate+base64 encoding draw.io's desktop app
            writes by default. Off by default: the plain form is the one that
            reviews and diffs, and netgraph's whole argument is that a diagram
            should be readable.

    Returns:
        The document, newline-terminated.
    """
    model = "".join(_model_lines(diagram, indent=3))
    body = encode_diagram(model) if compress else f"\n{model}      "
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<mxfile "
        + _attributes(
            (
                ("host", "netgraph"),
                ("agent", AGENT),
                ("type", "device"),
                (f"xmlns:{NAMESPACE_PREFIX}", NAMESPACE_URI),
            )
        )
        + ">",
        f"  <diagram {_attributes((('id', _diagram_id(diagram)), ('name', diagram.name)))}>"
        f"{body}</diagram>",
        "</mxfile>",
    ]
    return "\n".join(lines) + "\n"


def _diagram_id(diagram: Diagram) -> str:
    """A stable id for the tab. Derived, never random: two exports must match."""
    return f"netgraph-{diagram.view or 'diagram'}"


def _model_lines(diagram: Diagram, *, indent: int) -> Iterator[str]:
    pad = "  " * indent
    yield f"{pad}<mxGraphModel {_attributes(_MODEL_ATTRIBUTES)}>\n"
    yield f"{pad}  <root>\n"
    yield f'{pad}    <mxCell id="{_MODEL_ROOT_ID}" />\n'
    yield f'{pad}    <mxCell id="{ROOT_ID}" parent="{_MODEL_ROOT_ID}" />\n'
    for cell in diagram.cells:
        yield from _cell_lines(cell, indent=indent + 2)
    yield f"{pad}  </root>\n"
    yield f"{pad}</mxGraphModel>\n"


def _cell_lines(cell: Cell, *, indent: int) -> Iterator[str]:
    """One cell, as an ``<object>`` wrapper around an ``<mxCell>``.

    Every cell netgraph writes is wrapped, even one with a bare label: the
    wrapper is where custom attributes live, and a cell that grows one later
    would otherwise change shape in the diff for no reason a reader can see.
    """
    pad = "  " * indent
    yield f"{pad}<object {_attributes(_object_attributes(cell))}>\n"
    yield f"{pad}  <mxCell {_attributes(_mxcell_attributes(cell))}>\n"
    yield from _geometry_lines(cell, indent=indent + 2)
    yield f"{pad}  </mxCell>\n"
    yield f"{pad}</object>\n"


def _object_attributes(cell: Cell) -> Iterator[tuple[str, str]]:
    yield ("label", cell.label)
    for name in _ATTRIBUTE_ORDER:
        value = cell.attributes.get(name)
        if value is not None:
            yield (f"{NAMESPACE_PREFIX}:{name}", value)
    for name in sorted(set(cell.attributes) - set(_ATTRIBUTE_ORDER)):
        yield (f"{NAMESPACE_PREFIX}:{name}", cell.attributes[name])
    yield ("id", cell.id)


def _mxcell_attributes(cell: Cell) -> Iterator[tuple[str, str]]:
    yield ("style", cell.style)
    yield ("parent", cell.parent)
    if cell.edge:
        yield ("edge", "1")
        if cell.source:
            yield ("source", cell.source)
        if cell.target:
            yield ("target", cell.target)
    if cell.vertex:
        yield ("vertex", "1")
    if not cell.visible:
        yield ("visible", "0")


def _geometry_lines(cell: Cell, *, indent: int) -> Iterator[str]:
    pad = "  " * indent
    if cell.edge:
        if not cell.points:
            yield f'{pad}<mxGeometry relative="1" as="geometry" />\n'
            return
        yield f'{pad}<mxGeometry relative="1" as="geometry">\n'
        yield f'{pad}  <Array as="points">\n'
        for x, y in cell.points:
            yield f'{pad}    <mxPoint x="{_number(x)}" y="{_number(y)}" />\n'
        yield f"{pad}  </Array>\n"
        yield f"{pad}</mxGeometry>\n"
        return
    x, y, width, height = cell.geometry
    yield (
        f'{pad}<mxGeometry x="{_number(x)}" y="{_number(y)}" '
        f'width="{_number(width)}" height="{_number(height)}" as="geometry" />\n'
    )


def _attributes(pairs: Iterable[tuple[str, str]]) -> str:
    return " ".join(f'{name}="{_escape(value)}"' for name, value in pairs)


#: XML's five predefined entities. Written out rather than delegated to
#: ``saxutils`` so that ``"`` and ``'`` are escaped in *every* position: these
#: strings are attribute values, and an inventory may legitimately hold both.
_ESCAPES: Final[tuple[tuple[str, str], ...]] = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("'", "&apos;"),
)


def _escape(value: str) -> str:
    for character, entity in _ESCAPES:
        value = value.replace(character, entity)
    # A newline inside an attribute value is normalised to a space by every
    # conforming parser, so a description that holds one has to be written as a
    # reference or it will not survive the round trip.
    return value.replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")


def _number(value: float) -> str:
    """A coordinate in the XML. Twelve significant digits, for the same reason
    :func:`netgraph.drawio.identity._number` uses them: ``%g`` would round a
    six-figure page coordinate to the nearest point."""
    return str(int(value)) if value == int(value) else f"{value:.12g}"


# --------------------------------------------------------------------------- #
# The two diagram encodings
# --------------------------------------------------------------------------- #


def encode_diagram(model: str) -> str:
    """A ``<mxGraphModel>`` as the compressed payload of a ``<diagram>``.

    URI-encoded, raw-deflated and base64'd, in that order — the pipeline the
    draw.io client runs, in the order it runs it. Getting the order or the
    escaping alphabet wrong produces a file that opens as an empty canvas
    rather than one that fails to open, which is why
    ``tests/test_drawio.py`` asserts the round trip both ways.
    """
    encoded = quote(model, safe=_URI_SAFE)
    compressor = zlib.compressobj(zlib.Z_BEST_COMPRESSION, zlib.DEFLATED, _RAW_DEFLATE)
    deflated = compressor.compress(encoded.encode("utf-8")) + compressor.flush()
    return base64.b64encode(deflated).decode("ascii")


def decode_diagram(payload: str) -> str:
    """The inverse of :func:`encode_diagram`.

    Raises:
        DrawioFormatError: The payload is not base64, is not deflate, or
            inflates past :data:`MAX_INFLATED_BYTES`.
    """
    try:
        deflated = base64.b64decode(payload.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DrawioFormatError(
            "the <diagram> element holds text that is not base64, so it is neither the plain "
            "nor the compressed encoding draw.io writes"
        ) from exc

    inflater = zlib.decompressobj(_RAW_DEFLATE)
    try:
        inflated = inflater.decompress(deflated, MAX_INFLATED_BYTES + 1)
    except zlib.error as exc:
        raise DrawioFormatError(f"the compressed <diagram> does not inflate: {exc}") from exc
    if len(inflated) > MAX_INFLATED_BYTES:
        raise DrawioFormatError(
            f"the compressed <diagram> inflates past the {MAX_INFLATED_BYTES}-byte ceiling; "
            "this is not a diagram"
        )
    return unquote(inflated.decode("utf-8", errors="replace"))


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def parse_mxfile(text: str, *, source: str = "<input>") -> Diagram:
    """Read a ``.drawio`` document into the neutral model.

    Args:
        text: The whole file. Either encoding, and either root element — an
            ``<mxfile>`` or a bare ``<mxGraphModel>``, both of which draw.io
            produces depending on how the file was saved.
        source: How the file is named in diagnostics.

    Raises:
        DrawioFormatError: The text is not XML, is not a draw.io model, carries
            a document type declaration, or holds no diagram at all.
    """
    if len(text.encode("utf-8", errors="ignore")) > MAX_DOCUMENT_BYTES:
        raise DrawioFormatError(
            f"{source} is past the {MAX_DOCUMENT_BYTES}-byte ceiling on one diagram"
        )
    if _UNSAFE_DECLARATION.search(text):
        raise DrawioFormatError(
            f"{source} carries a document type or entity declaration; netgraph does not "
            "expand entities in a file it was handed, so the diagram is refused rather "
            "than parsed"
        )

    root = _parse_xml(text, source=source)
    model = _model_element(root, source=source)
    return _diagram_of(model, name=_diagram_name(root))


def _parse_xml(text: str, *, source: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(_ensure_namespace(text))
    except ElementTree.ParseError as exc:
        raise DrawioFormatError(f"{source} is not well-formed XML: {clip_text(str(exc))}") from exc


def _ensure_namespace(text: str) -> str:
    """Bind the ``netgraph`` prefix on the root element when nothing else has.

    A prefix an editor did not declare makes the whole document unparseable,
    and the attributes carrying it are the ones the round trip depends on. So
    the declaration is put back rather than the file being refused — the
    prefix means one thing here whoever wrote it.
    """
    if f"xmlns:{NAMESPACE_PREFIX}" in text:
        return text
    match = _ROOT_TAG.search(text)
    if match is None:
        return text
    declaration = f' xmlns:{NAMESPACE_PREFIX}="{NAMESPACE_URI}"'
    start, end = match.span()
    tag = match.group(1)
    rest = match.group(2)
    close = match.group(3)
    return f"{text[:start]}<{tag}{rest}{declaration}{close}>{text[end:]}"


def _diagram_name(root: ElementTree.Element) -> str:
    element = root if root.tag == "diagram" else root.find("diagram")
    return (element.get("name") or "netgraph") if element is not None else "netgraph"


def _model_element(root: ElementTree.Element, *, source: str) -> ElementTree.Element:
    """The ``<mxGraphModel>``, whichever of the four shapes the file has."""
    if root.tag == "mxGraphModel":
        return root
    diagram = root if root.tag == "diagram" else root.find("diagram")
    if diagram is None:
        raise DrawioFormatError(
            f"{source} holds no <diagram>; the root element is <{root.tag}>, which is not a "
            "draw.io file netgraph knows how to read"
        )
    model = diagram.find("mxGraphModel")
    if model is not None:
        return model
    payload = (diagram.text or "").strip()
    if not payload:
        raise DrawioFormatError(f"{source} holds a <diagram> with nothing in it")
    inner = decode_diagram(payload)
    decoded = _parse_xml(inner, source=f"{source} (compressed diagram)")
    if decoded.tag != "mxGraphModel":
        raise DrawioFormatError(
            f"{source} decompresses to <{decoded.tag}> rather than to an <mxGraphModel>"
        )
    return decoded


def _diagram_of(model: ElementTree.Element, *, name: str) -> Diagram:
    root = model.find("root")
    cells = tuple(_cells(root)) if root is not None else ()
    metadata = next((cell for cell in cells if cell.role is CellRole.METADATA), None)
    if metadata is None:
        return Diagram(name=name, cells=cells)
    return Diagram(
        view=metadata.attribute(ATTR_VIEW),
        name=name,
        cells=cells,
        frame=Frame(
            origin_x=_float(metadata.attribute(ATTR_ORIGIN_X)),
            origin_y=_float(metadata.attribute(ATTR_ORIGIN_Y)),
        ),
        scope=_scope(metadata.attribute(ATTR_SCOPE)),
        version=metadata.attribute(ATTR_VERSION) or MODEL_VERSION,
        generator=metadata.attribute("generator"),
        annotated=metadata.attribute(ATTR_ANNOTATED) == "1",
    )


def _scope(value: str) -> Scope:
    """A scope token, defaulting to the cautious answer.

    An unreadable or absent scope is treated as ``partial``, which is the
    reading under which nothing is ever deleted. Guessing the other way would
    let a corrupted attribute authorise removing every element of the tree.
    """
    try:
        return Scope(value)
    except ValueError:
        return Scope.PARTIAL


def _cells(root: ElementTree.Element) -> Iterator[Cell]:
    for child in root:
        if child.tag == "object" or child.tag == "UserObject":
            inner = child.find("mxCell")
            if inner is not None:
                yield _cell(inner, wrapper=child)
        elif child.tag == "mxCell":
            if child.get("id") in {_MODEL_ROOT_ID, ROOT_ID}:
                continue
            yield _cell(child, wrapper=None)


def _cell(element: ElementTree.Element, *, wrapper: ElementTree.Element | None) -> Cell:
    carrier = wrapper if wrapper is not None else element
    attributes = _netgraph_attributes(carrier)
    geometry = element.find("mxGeometry")
    return Cell(
        id=carrier.get("id") or element.get("id") or "",
        role=_role(attributes.get(ATTR_ROLE)),
        label=(carrier.get("label") if wrapper is not None else element.get("value")) or "",
        style=element.get("style") or "",
        parent=element.get("parent") or ROOT_ID,
        edge=element.get("edge") in {"1", "true"},
        vertex=element.get("vertex") in {"1", "true"},
        source=element.get("source") or "",
        target=element.get("target") or "",
        x=_optional_float(geometry, "x"),
        y=_optional_float(geometry, "y"),
        width=_optional_float(geometry, "width"),
        height=_optional_float(geometry, "height"),
        points=_points(geometry),
        visible=element.get("visible") not in {"0", "false"},
        attributes=attributes,
    )


def _role(value: str | None) -> CellRole | None:
    """The declared role, or ``None`` for a cell netgraph did not write."""
    if value is None:
        return None
    try:
        return CellRole(value)
    except ValueError:
        return None


def _netgraph_attributes(element: ElementTree.Element) -> Mapping[str, str]:
    """The ``netgraph:*`` attributes of one element, prefix stripped.

    Both spellings are accepted: the expanded ``{uri}kind`` a namespace-aware
    parser produces, and the literal ``netgraph:kind`` that reaches us when an
    editor wrote the attribute without ever binding the prefix.
    """
    found: dict[str, str] = {}
    for key, value in element.attrib.items():
        if key.startswith(_NAMESPACE_MARKER):
            found[key[len(_NAMESPACE_MARKER) :]] = value
        elif key.startswith(f"{NAMESPACE_PREFIX}:"):
            found[key[len(NAMESPACE_PREFIX) + 1 :]] = value
    return found


def _points(geometry: ElementTree.Element | None) -> tuple[tuple[float, float], ...]:
    if geometry is None:
        return ()
    array = next(
        (child for child in geometry.findall("Array") if child.get("as") == "points"), None
    )
    if array is None:
        return ()
    return tuple(
        (_float(point.get("x") or "0"), _float(point.get("y") or "0"))
        for point in array.findall("mxPoint")
    )


def _optional_float(element: ElementTree.Element | None, name: str) -> float | None:
    if element is None:
        return None
    value = element.get(name)
    return None if value is None else _float(value)


def _float(value: str) -> float:
    """A coordinate from a file somebody else may have written.

    An unparseable one is zero rather than an exception: draw.io writes an
    empty ``x`` for a cell it has never positioned, and a diagram is not worth
    refusing over a missing number.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if -1e9 < number < 1e9 else 0.0
