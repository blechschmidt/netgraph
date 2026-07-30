#!/usr/bin/env python3
"""Generate ``docs/example-report/``: one committed, browsable as-built report.

``netgraph report`` produces a document, and a document is the one kind of output
a reader cannot judge from a flag list — so one is committed, generated from
``examples/patch-room`` because that inventory is the one with patch panels,
racks, PDUs, PoE and a wireless access point in it, which is what an as-built
record is mostly about.

Two stamps are pinned rather than discovered:

* **the timestamp**, to :data:`GENERATED_AT`. It is the only part of a report that
  is not a function of the inventory, and a committed artefact that changed on
  every run would be a diff nobody reads;
* **the revision**, to nothing. The real one would be the commit of this
  repository, which changes with every commit *including the one that regenerates
  this file* — so the pages say "not under version control" rather than naming a
  revision that is wrong by the time it is pushed. A report of a real inventory
  names the real commit; see ``docs/commands/report.md``.

Every page also carries the netgraph version, so a version bump is one of the two
reasons to regenerate this — the release checklist in ``docs/releasing.md`` says
so, and ``tests/test_report.py`` fails until it is done.

Usage::

    python tools/gen_example_report.py            # rewrite docs/example-report/
    python tools/gen_example_report.py --check    # exit 1 if the pages are stale

``tests/test_report.py`` runs the ``--check`` path, which compares the Markdown
pages and ignores the SVG files: a drawing is Graphviz's output and differs
between Graphviz versions, so pinning its bytes would fail on a machine whose
Graphviz is a release ahead of the one the artefact was committed from.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from netgraph.diagnostics import build_report as build_diagnostics  # noqa: E402
from netgraph.loader import load_tree  # noqa: E402
from netgraph.report import Bundle, Options, generate  # noqa: E402
from netgraph.validate import validate  # noqa: E402

#: The inventory the committed report documents.
INVENTORY: Final = REPO_ROOT / "examples" / "patch-room"

#: Where the bundle is committed.
OUTPUT: Final = REPO_ROOT / "docs" / "example-report"

#: The pinned generated-at stamp. Bump it when the artefact is regenerated for a
#: reason other than a change in the inventory.
GENERATED_AT: Final = "2026-07-30T00:00:00Z"

#: The pages compared by ``--check``; see the module docstring on the drawings.
CHECKED_SUFFIXES: Final = (".md",)


def build() -> Bundle:
    """The bundle exactly as it should be committed."""
    inventory = load_tree(INVENTORY)
    findings = validate(inventory)
    bundle, diagrams = generate(
        inventory,
        options=Options(
            format="markdown",
            title="patch-room — as-built network documentation",
            generated_at=GENERATED_AT,
            revision="",
            revision_state="",
        ),
        diagnostics=build_diagnostics(inventory, findings).diagnostics,
    )
    if diagrams.problems:  # pragma: no cover - a broken Graphviz
        raise SystemExit("; ".join(diagrams.problems))
    return bundle


def check(bundle: Bundle) -> list[str]:
    """The paths that differ from what is committed, ignoring the drawings."""
    differences: list[str] = []
    for path in bundle.paths:
        if not path.endswith(CHECKED_SUFFIXES):
            continue
        target = OUTPUT / path
        current = target.read_bytes() if target.is_file() else b""
        if current != bundle.files[path]:
            differences.append(path)
    committed = {
        entry.relative_to(OUTPUT).as_posix()
        for entry in OUTPUT.rglob("*")
        if entry.is_file() and entry.suffix in CHECKED_SUFFIXES
    }
    differences.extend(sorted(committed - set(bundle.paths)))
    return differences


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed pages are not what this would write",
    )
    args = parser.parse_args(argv)

    bundle = build()
    if args.check:
        stale = check(bundle)
        if stale:
            print(
                f"docs/example-report/ is out of date ({len(stale)} page(s)); "
                "run 'python tools/gen_example_report.py'",
                file=sys.stderr,
            )
            return 1
        return 0

    removed = bundle.write(OUTPUT, prune=True)
    print(f"wrote {len(bundle.files)} file(s) to {OUTPUT}")
    if removed:
        print(f"removed {len(removed)} stale file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
