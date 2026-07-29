#!/usr/bin/env python3
"""Write ``schema/netgraph.schema.json`` from the pydantic models.

The schema is committed so that an editor, a pre-commit hook or a CI job can
reach it by path or by URL without installing netgraph first. That only works
if the committed copy is the one the models actually produce, which is what
``--check`` asserts — the same drift guard ``docs/schema-reference.md`` has.

Usage::

    python tools/gen_json_schema.py            # rewrite the committed schema
    python tools/gen_json_schema.py --check    # exit 1 if it is out of date
    python tools/gen_json_schema.py -k cable   # one kind, to stdout

``tests/test_schema.py`` runs the ``--check`` path, so CI fails when a model
changes without the schema being regenerated. ``netgraph schema`` produces the
identical document at runtime; this script exists so that a checkout without an
installed netgraph can still refresh the file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from netgraph.models import DOCUMENT_KINDS  # noqa: E402
from netgraph.schema import build_schema  # noqa: E402

OUTPUT: Final = REPO_ROOT / "schema" / "netgraph.schema.json"


def build(kind: str | None = None) -> str:
    """The schema document, exactly as it is committed."""
    return json.dumps(build_schema(kind), indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 when the committed file is out of date.",
    )
    parser.add_argument(
        "-k",
        "--kind",
        choices=DOCUMENT_KINDS,
        default=None,
        help="Emit the schema for a single kind. Implies writing to stdout.",
    )
    parser.add_argument("-o", "--output", type=Path, default=None)
    args = parser.parse_args(argv)

    content = build(args.kind)

    if args.kind is not None and args.output is None:
        if args.check:
            parser.error("--check applies to the committed all-kinds schema, not to --kind")
        sys.stdout.write(content)
        return 0

    output: Path = args.output or OUTPUT
    if args.check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != content:
            print(
                f"{output} is out of date; run 'python tools/gen_json_schema.py'",
                file=sys.stderr,
            )
            return 1
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
