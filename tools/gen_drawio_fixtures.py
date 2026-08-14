#!/usr/bin/env python3
"""Regenerate the ``.drawio`` fixtures under ``tests/fixtures/drawio/``.

    python tools/gen_drawio_fixtures.py

Three files come out, and each answers a different question:

``arranged-l1.drawio``
    What ``netgraph export drawio`` produces from the fixture inventory. A
    golden: a byte for byte diff of it is the review of a change to the emitter.
``arranged-l1-compressed.drawio``
    The same model in the deflate+base64 encoding draw.io writes by default,
    so the reader is exercised against both.
``arranged-edited.drawio``
    ``arranged-l1.drawio`` with the four gestures a draw.io user can perform
    applied to it by hand — a node moved, a label retyped, a node and its cable
    deleted, and an edge drawn between two boxes. The golden plan it must
    produce lives beside it and is written by ``tests/test_drawio.py``.

The edits are applied here as text substitutions rather than being committed as
an opaque blob, so the *diff between the two diagrams* is reviewable and the
fixture cannot quietly stop testing what it says it tests.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from netgraph.export import ExportContext, ExportOptions, export  # noqa: E402
from netgraph.fsio import write_text  # noqa: E402
from netgraph.loader import load_tree  # noqa: E402
from netgraph.render import build_graph  # noqa: E402
from netgraph.render.graph import Layer  # noqa: E402
from netgraph.render.icons import icon_theme  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "drawio"
INVENTORY = FIXTURES / "inventory"

#: How far ``pc-1`` is dragged, in draw.io points. A round number so the
#: expected coordinate in the golden plan is one somebody can check by eye.
MOVED_BY = 120.0


def emitted(*, compress: bool) -> str:
    inventory = load_tree(INVENTORY)
    assert not inventory.errors, inventory.errors
    graph = build_graph(inventory, layer=Layer.L1)
    result = export(
        "drawio",
        lambda recorder: ExportContext(
            inventory=inventory,
            graphs={Layer.L1: graph},
            options=ExportOptions(view="l1", icons=icon_theme("cisco"), compress=compress),
            recorder=recorder,
        ),
    )
    return result.payload


def edited(pristine: str) -> str:
    """The four gestures, applied to the exported diagram as a person would."""
    text = pristine

    # 1. Moved: pc-1 dragged to the right.
    text = re.sub(
        r'(netgraph:name="hosts/pc-1".*?<mxGeometry x=")([\d.-]+)',
        lambda match: match[1] + _plain(float(match[2]) + MOVED_BY),
        text,
        flags=re.DOTALL,
    )

    # 2. Renamed: the label retyped on the canvas.
    text = text.replace('<object label="srv-app"', '<object label="srv-web"')

    # 3. Deleted: pc-2's cell removed entirely.
    text = re.sub(r"\n *<object label=\"pc-2\".*?</object>", "", text, flags=re.DOTALL)

    # 4. Newly connected: an edge drawn between two boxes, with no identity of
    #    its own -- exactly what draw.io produces when somebody drags a link.
    ids = dict(re.findall(r'netgraph:name="([^"]+)"[^>]*id="(n-[^"]+)"', text))
    drawn = (
        f'          <mxCell id="drawn-1" value="" style="edgeStyle=none;html=1;" '
        f'edge="1" parent="1" source="{ids["hosts/pc-1"]}" target="{ids["devices/sw-access"]}">\n'
        f'            <mxGeometry relative="1" as="geometry" />\n'
        f"          </mxCell>\n"
    )
    return text.replace("        </root>", drawn + "        </root>")


def _plain(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def main() -> int:
    pristine = emitted(compress=False)
    write_text(FIXTURES / "arranged-l1.drawio", pristine)
    write_text(FIXTURES / "arranged-l1-compressed.drawio", emitted(compress=True))
    write_text(FIXTURES / "arranged-edited.drawio", edited(pristine))
    print(f"wrote 3 diagrams to {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
