"""The starter inventory ``netgraph init`` writes.

An empty directory is a bad place to start from: the envelope has four keys and
an ``apiVersion`` nobody remembers, and the JSON Schema that would have told the
editor about both has to be found and wired up by hand. ``init`` writes a tree
that is already correct — it validates clean and renders at all three layers,
which :mod:`tests.test_init` asserts rather than assumes — so the first run of
``netgraph validate`` succeeds and the first edit is made with completion and
inline errors already working.

The content is kept here rather than in :mod:`netgraph.cli` for two reasons: the
CLI stays about arguments and reporting, and the files can be built and checked
without touching a filesystem. :func:`build_scaffold` is pure — it returns
relative POSIX paths mapped to text — and :func:`write_scaffold` is the only
part that writes anything.

The three documents mirror ``examples/quickstart``, which is the tree the README
walkthrough builds, so a user who reads both is not shown two different
networks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from netgraph.config import CONFIG_FILE_NAME
from netgraph.errors import NetgraphError
from netgraph.schema import build_schema

__all__ = [
    "GITIGNORE_FILE_NAME",
    "SCHEMA_FILE_NAME",
    "Scaffold",
    "ScaffoldError",
    "build_scaffold",
    "write_scaffold",
]

#: Where the generated JSON Schema goes, relative to the inventory root. The
#: modeline of every generated document points at this path, so the two move
#: together or not at all.
SCHEMA_FILE_NAME: Final = "schema/netgraph.schema.json"

GITIGNORE_FILE_NAME: Final = ".gitignore"


class ScaffoldError(NetgraphError):
    """Raised when the target directory is not one ``init`` may write into.

    Shares its exit status with the other "netgraph refused to act on this
    directory" outcomes, so a script can treat them alike.
    """

    exit_code = 1


@dataclass(frozen=True, slots=True)
class Scaffold:
    """The files one ``netgraph init`` would write, before any of them exist."""

    #: Relative POSIX path -> file content. Insertion order is the order the
    #: files are written and reported in, which is why the mapping is ordered
    #: root-first: the reader sees the configuration before the documents.
    files: Mapping[str, str]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self.files)


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #

_CONFIG: Final = f"""\
# {CONFIG_FILE_NAME} — how netgraph validates *this* inventory.
#
# Everything here is optional, and what is commented out below is exactly what
# netgraph does without it; the file is generated fully commented on purpose.
# Rules are named by their short id (E001, W103, I002), by the NG-* alias from
# the specification, or by "*" for all of them. Run 'netgraph rules' for the
# catalogue.

# [validate]
#
# Promote every warning that survives 'ignore' to an error, so any finding
# fails 'netgraph validate' and refuses 'netgraph render'. Same as --strict,
# which can turn this on but never off.
# strict = false
#
# Never report these rules at all. 'netgraph validate --disable RULE' adds to
# the list for a single run; a 'netgraph/ignore' annotation on an element
# silences a rule for that element alone.
# ignore = ["W103", "NG-C010"]

# Re-grade a rule instead of silencing it: "error", "warning" or "info".
# [validate.severity]
# E004 = "warning"
"""

_GITIGNORE: Final = """\
# Rendered diagrams. The YAML tree is the source of truth and 'netgraph render'
# regenerates these from it, so they are output rather than source. Drop the
# line for a format you publish on purpose — a network.svg committed for a
# README, say.
*.dot
*.mmd
*.pdf
*.png
*.svg
/out/

# schema/netgraph.schema.json is deliberately *not* ignored: it is editor
# wiring, and a fresh checkout should offer completion before netgraph is
# installed. Refresh it with 'netgraph schema -o schema/netgraph.schema.json'.
"""

_ROUTER: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: router
metadata:
  name: rtr-gw
  labels: {site: office}
  annotations:
    # wan0 faces the ISP, which is not an element of this inventory, so it
    # terminates no cable on purpose. Saying so is what an exception looks
    # like; deleting the rule for everybody would not be.
    netgraph/ignore: "NG-C015"
spec:
  vendor: MikroTik
  vlans:
    - id: 10
      name: office
  interfaces:
    - name: wan0
      type: ethernet
      description: ISP hand-off
      mtu: 1500
      ipv4:
        addresses: [203.0.113.2/30]
    - name: lan0
      type: ethernet
      description: Downlink to the switch
      mac: 00:1e:8c:aa:00:01
      mtu: 1500
      vlan:
        mode: access
        access_vlan: 10
      ipv4:
        # Shorthand for {ip: 192.168.10.1, prefix_length: 24}.
        addresses: [192.168.10.1/24]
"""

_SWITCH: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-office
  labels: {site: office}
spec:
  vlans:
    - id: 10
      name: office
  # A bridge carries VLAN membership on its ports and no address of its own; a
  # management address belongs on a 'type: vlan' SVI (putting one here is W104).
  interfaces:
    - name: port1
      type: ethernet
      description: Uplink to rtr-gw
      mtu: 1500
      vlan: {mode: access, access_vlan: 10}
    - name: port2
      type: ethernet
      description: Desk
      mtu: 1500
      vlan: {mode: access, access_vlan: 10}
"""

_COMPUTER: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-alice
  labels: {site: office}
spec:
  # No 'vlan' block: the host sends untagged frames and inherits the VLAN of
  # the access port facing it, which netgraph knows not to complain about.
  interfaces:
    - name: eno1
      type: ethernet
      mac: 00:1e:8c:bb:00:01
      mtu: 1500
      ipv4:
        addresses: [192.168.10.20/24]
"""

_CABLES: Final = """\
# One file, two documents, separated by '---'. A cable is an element in its own
# right rather than a field on a device: it joins exactly two interfaces,
# written 'device:interface', and the order carries no meaning.
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-rtr-sw}
spec:
  endpoints: [rtr-gw:lan0, sw-office:port1]
  medium: copper
  speed: 1Gbps
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-sw-alice}
spec:
  endpoints: [sw-office:port2, pc-alice:eno1]
  medium: copper
  speed: 1Gbps
"""

#: ``--minimal``: the envelope and nothing else. Every line is a comment, so the
#: tree holds no elements at all and still validates — the point is to show the
#: four keys and the values they take, not to hand out a network someone has to
#: delete before writing their own.
_TEMPLATE: Final = """\
# The envelope every netgraph document shares. Uncomment, edit, and add one
# file per element; the folder a file sits in becomes its namespace, so names
# only have to be unique within their own directory.
#
# apiVersion: netgraph.dev/v1alpha1
# kind: switch          # switch router hub computer server adapter cable
# metadata:
#   name: sw-office     # unique within this folder
#   labels: {site: office}
# spec:
#   interfaces:
#     - name: port1
#       type: ethernet
#       mtu: 1500
#       vlan: {mode: access, access_vlan: 10}
#
# 'netgraph schema --kind switch' prints the full grammar of one kind, and the
# modeline above hands the same thing to your editor.
"""

#: The example tree, in the order the files are reported.
_EXAMPLE_DOCUMENTS: Final[tuple[tuple[str, str], ...]] = (
    ("devices/rtr-gw.yaml", _ROUTER),
    ("devices/sw-office.yaml", _SWITCH),
    ("devices/pc-alice.yaml", _COMPUTER),
    ("cables/links.yaml", _CABLES),
)

#: ``--minimal``: one commented template instead of the three devices.
_MINIMAL_DOCUMENTS: Final[tuple[tuple[str, str], ...]] = (("devices/example.yaml", _TEMPLATE),)


def build_scaffold(*, minimal: bool = False, schema: bool = True) -> Scaffold:
    """The files a ``netgraph init`` with these options would write.

    Args:
        minimal: Write the commented envelope template instead of the
            three-device example topology.
        schema: Write the JSON Schema and point each document at it with a
            ``yaml-language-server`` modeline. Turning this off leaves an
            editor with nothing to complete from, so it is opt-out.
    """
    files: dict[str, str] = {CONFIG_FILE_NAME: _CONFIG, GITIGNORE_FILE_NAME: _GITIGNORE}
    if schema:
        files[SCHEMA_FILE_NAME] = json.dumps(build_schema(), indent=2, ensure_ascii=False) + "\n"
    documents = _MINIMAL_DOCUMENTS if minimal else _EXAMPLE_DOCUMENTS
    for path, body in documents:
        files[path] = _with_modeline(path, body, schema=schema)
    return Scaffold(files=files)


def _with_modeline(path: str, body: str, *, schema: bool) -> str:
    """Prefix ``body`` with the modeline that wires ``path`` to the schema.

    The reference is relative rather than the published ``$id``, so the tree
    keeps working offline, inside a container, and on the schema of the netgraph
    version that wrote it rather than of whatever is published today.
    """
    if not schema:
        return body
    depth = len(PurePosixPath(path).parents) - 1
    return f"# yaml-language-server: $schema={'../' * depth}{SCHEMA_FILE_NAME}\n{body}"


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def write_scaffold(scaffold: Scaffold, target: Path, *, force: bool = False) -> list[Path]:
    """Write ``scaffold`` into ``target``, creating it if it does not exist.

    Args:
        scaffold: What to write, from :func:`build_scaffold`.
        target: Directory to write into. Created, with its parents, when absent.
        force: Write into a directory that already holds something, overwriting
            any file of the same name.

    Returns:
        The files written, in scaffold order.

    Raises:
        ScaffoldError: ``target`` is not a directory, is not empty, or cannot be
            written to. Scaffolding is a one-shot convenience over files a user
            typed by hand; silently overwriting them would be a poor trade for
            saving a ``--force``.
    """
    if target.exists() and not target.is_dir():
        raise ScaffoldError(f"{target} exists and is not a directory")
    if not force:
        _refuse_if_occupied(scaffold, target)

    written: list[Path] = []
    for relative, content in scaffold.files.items():
        path = target.joinpath(*PurePosixPath(relative).parts)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ScaffoldError(f"cannot write {path}: {exc.strerror or exc}") from exc
        written.append(path)
    return written


def _refuse_if_occupied(scaffold: Scaffold, target: Path) -> None:
    """Fail unless ``target`` is absent or empty, naming what is in the way."""
    try:
        existing = sorted(entry.name for entry in target.iterdir()) if target.is_dir() else []
    except OSError as exc:
        raise ScaffoldError(f"cannot read {target}: {exc.strerror or exc}") from exc
    if not existing:
        return

    # The clash a user is most likely to have made is running init twice, so
    # name the files that would be overwritten when there are any, and fall back
    # to what the directory holds when the collision is only that it is in use.
    clashes = [
        relative for relative in scaffold.files if target.joinpath(*relative.split("/")).exists()
    ]
    what = (
        f"would overwrite {', '.join(clashes)}"
        if clashes
        else f"is not empty (it holds {', '.join(existing[:5])}"
        + (", ..." if len(existing) > 5 else "")
        + ")"
    )
    raise ScaffoldError(
        f"{target} {what}; pass --force to write anyway, or name an empty directory"
    )
