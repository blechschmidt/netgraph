#!/usr/bin/env python3
"""Generate a large synthetic inventory and time the pipeline over it.

This is the harness behind the timing table in ``docs/follow-ups.md``. It is not
part of the test suite -- it takes tens of seconds and its numbers depend on the
machine -- but it is committed so a later measurement is comparable with an
earlier one instead of being a fresh guess::

    python tools/bench_pipeline.py                  # generate, time, report
    python tools/bench_pipeline.py --keep out/bench # leave the tree behind

The generated tree is deterministic: same arguments, same bytes. Every element
it emits is schema-valid and semantically clean, so ``validate`` does real work
rather than bailing out on the first error.

Pass ``--compare-loaders`` to time the parse step through both YAML parsers in
one process. That is the number the libyaml follow-up was opened on.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - convenience for a checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from netgraph.loader import load_tree  # noqa: E402
from netgraph.loader.documents import (  # noqa: E402
    HAVE_LIBYAML,
    NodeLoader,
    PureStrictSafeLoader,
    StrictSafeLoader,
    libyaml_loader,
    read_documents,
)
from netgraph.render import build_graph, render  # noqa: E402
from netgraph.validate import validate  # noqa: E402

T = TypeVar("T")

API_VERSION = "netgraph.dev/v1alpha1"


# --------------------------------------------------------------------------- #
# Generating the inventory
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Shape:
    """How big the synthetic inventory is.

    The defaults reproduce the tree the 2026-07-27 review measured: 1056
    devices in ~2100 documents, roughly 8 MB of YAML.
    """

    sites: int = 6
    racks_per_site: int = 7
    hosts_per_rack: int = 24

    @property
    def switches(self) -> int:
        return self.sites * self.racks_per_site

    @property
    def devices(self) -> int:
        return self.sites * (1 + self.racks_per_site * (1 + self.hosts_per_rack))

    @property
    def cables(self) -> int:
        # One per host, one per rack switch uplink, one per site router uplink.
        return self.devices - 1


def _mac(*parts: int) -> str:
    """A locally-administered, unicast MAC built from the position in the tree."""
    octets = [0x02, *(part & 0xFF for part in parts)]
    octets += [0x00] * (6 - len(octets))
    return ":".join(f"{octet:02x}" for octet in octets[:6])


def _router(site: int, racks: int) -> str:
    ports = "\n".join(
        f"""\
    - name: xe-0/0/{rack}
      type: ethernet
      description: Uplink to rack {rack:02d}
      mac: {_mac(1, site, rack)}
      mtu: 9000
      ipv4:
        addresses: [10.{site}.{rack}.1/24]"""
        for rack in range(1, racks + 1)
    )
    return f"""\
apiVersion: {API_VERSION}
kind: router
metadata:
  name: rtr-s{site:02d}
  description: Site {site:02d} border router
  labels:
    site: s{site:02d}
    role: core
    env: prod
spec:
  vendor: Juniper
  model: MX204
  location: Site {site:02d} / MDF
  interfaces:
    - name: lo0
      type: loopback
      ipv4:
        addresses: [10.255.0.{site}/32]
{ports}
"""


def _switch(site: int, rack: int, hosts: int) -> str:
    access = "\n".join(
        f"""\
    - name: GigabitEthernet1/0/{host}
      type: ethernet
      description: Rack {rack:02d} host {host:02d}
      mac: {_mac(2, site, rack, host)}
      mtu: 1500
      vlan:
        mode: access
        access_vlan: 10"""
        for host in range(1, hosts + 1)
    )
    members = "\n".join(f"        - GigabitEthernet1/0/{host}" for host in range(1, hosts + 1))
    return f"""\
apiVersion: {API_VERSION}
kind: switch
metadata:
  name: sw-s{site:02d}-r{rack:02d}
  description: Site {site:02d} rack {rack:02d} top-of-rack switch
  labels:
    site: s{site:02d}
    rack: r{rack:02d}
    role: access
    env: prod
spec:
  vendor: Cisco
  model: C9300-48T
  location: Site {site:02d} / rack {rack:02d}
  bridge:
    name: br0
    type: customer-vlan-bridge
    address: {_mac(3, site, rack)}
  vlans:
    - id: 10
      name: hosts
    - id: 99
      name: mgmt
  interfaces:
    - name: br0
      type: bridge
      description: Switching instance
      members:
{members}
        - TenGigabitEthernet1/1/1
    - name: Vlan99
      type: vlan
      parent: br0
      description: In-band management
      vlan:
        mode: access
        access_vlan: 99
      ipv4:
        addresses: [10.{site}.{rack}.2/24]
    - name: TenGigabitEthernet1/1/1
      type: ethernet
      description: Uplink to rtr-s{site:02d}
      mac: {_mac(4, site, rack)}
      mtu: 9000
{access}
"""


def _host(site: int, rack: int, host: int) -> str:
    kind = "server" if host % 7 == 0 else "computer"
    return f"""\
apiVersion: {API_VERSION}
kind: {kind}
metadata:
  name: h-s{site:02d}-r{rack:02d}-{host:02d}
  description: Site {site:02d} rack {rack:02d} host {host:02d}
  labels:
    site: s{site:02d}
    rack: r{rack:02d}
    role: {"application" if kind == "server" else "workstation"}
    env: prod
spec:
  vendor: Dell
  model: PowerEdge R660
  location: Site {site:02d} / rack {rack:02d} / U{host:02d}
  interfaces:
    - name: lo
      type: loopback
      ipv4:
        addresses: [127.0.0.1/8]
      ipv6:
        addresses: ["::1/128"]
    - name: eno1
      type: ethernet
      description: Primary NIC
      mac: {_mac(5, site, rack, host)}
      mtu: 1500
      ipv4:
        addresses: [10.{site}.{rack}.{host + 10}/24]
      ipv6:
        addresses: ["2001:db8:{site}:{rack}::{host + 10:x}/64"]
"""


def _cable(name: str, a: str, b: str, *, medium: str, speed: str) -> str:
    return f"""\
apiVersion: {API_VERSION}
kind: cable
metadata:
  name: {name}
  description: {a} to {b}
  labels:
    medium: {medium}
spec:
  endpoints:
    - {a}
    - {b}
  medium: {medium}
  speed: {speed}
  length_m: 3
"""


def generate(root: Path, shape: Shape) -> tuple[int, int]:
    """Write the tree under ``root``; return ``(files, documents)``."""
    files = documents = 0

    def write(relative: str, *docs: str) -> None:
        nonlocal files, documents
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n".join(docs), encoding="utf-8")
        files += 1
        documents += len(docs)

    for site in range(1, shape.sites + 1):
        base = f"sites/s{site:02d}"
        write(f"{base}/core/rtr-s{site:02d}.yaml", _router(site, shape.racks_per_site))
        uplinks = []
        for rack in range(1, shape.racks_per_site + 1):
            rack_dir = f"{base}/racks/r{rack:02d}"
            write(
                f"{rack_dir}/sw-s{site:02d}-r{rack:02d}.yaml",
                _switch(site, rack, shape.hosts_per_rack),
            )
            hosts = [_host(site, rack, host) for host in range(1, shape.hosts_per_rack + 1)]
            # Several documents per file: the review's tree had ~2 per file, and
            # a multi-document file is the case the loader is slowest on.
            write(f"{rack_dir}/hosts.yaml", *hosts)
            write(
                f"{rack_dir}/cables.yaml",
                *(
                    _cable(
                        f"cbl-s{site:02d}-r{rack:02d}-{host:02d}",
                        f"h-s{site:02d}-r{rack:02d}-{host:02d}:eno1",
                        f"sw-s{site:02d}-r{rack:02d}:GigabitEthernet1/0/{host}",
                        medium="copper",
                        speed="1Gbps",
                    )
                    for host in range(1, shape.hosts_per_rack + 1)
                ),
            )
            uplinks.append(
                _cable(
                    f"cbl-s{site:02d}-up-r{rack:02d}",
                    f"sites/s{site:02d}/racks/r{rack:02d}/sw-s{site:02d}-r{rack:02d}"
                    ":TenGigabitEthernet1/1/1",
                    f"sites/s{site:02d}/core/rtr-s{site:02d}:xe-0/0/{rack}",
                    medium="fiber",
                    speed="10Gbps",
                )
            )
        write(f"{base}/cables/uplinks.yaml", *uplinks)

    return files, documents


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #


def timed(label: str, call: Callable[[], T], *, repeat: int) -> tuple[T, float]:
    """Run ``call`` ``repeat`` times; report the median, return the last result."""
    samples = []
    result: T
    for _ in range(repeat):
        start = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - start) * 1000)
    median = statistics.median(samples)
    print(f"{label:<34} {median:8.1f} ms   (min {min(samples):.1f}, n={repeat})")
    return result, median


def parse_with(loader: type[NodeLoader], files: list[Path], root: Path) -> int:
    """Re-parse every file through ``loader``, returning the document count."""
    import netgraph.loader.documents as module

    previous = module.StrictSafeLoader
    module.StrictSafeLoader = loader  # type: ignore[misc]
    try:
        count = 0
        for path in files:
            relative = PurePosixPath(path.relative_to(root).as_posix())
            count += sum(1 for _ in read_documents(path, relative=relative))
        return count
    finally:
        module.StrictSafeLoader = previous  # type: ignore[misc]


def yaml_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix in {".yaml", ".yml"})


def report(root: Path, *, repeat: int, compare: bool) -> None:
    files = yaml_files(root)
    size = sum(path.stat().st_size for path in files)
    print(f"inventory: {root}")
    print(f"           {len(files)} files, {size / 1_000_000:.1f} MB")
    print(f"           parser in use: {StrictSafeLoader.__name__} (libyaml={HAVE_LIBYAML})")
    print()

    inventory, _ = timed("load_tree", lambda: load_tree(root), repeat=repeat)
    if inventory.errors:
        print(f"!! {len(inventory.errors)} load errors, first: {inventory.errors[0]}")
    print(f"           {len(inventory)} elements, {len(inventory.devices)} devices")

    findings, _ = timed("validate", lambda: validate(inventory), repeat=repeat)
    errors = [finding for finding in findings if finding.severity.name == "ERROR"]
    if errors:
        print(f"!! {len(errors)} validation errors, first: {errors[0]}")

    graph, _ = timed("build_graph", lambda: build_graph(inventory), repeat=repeat)
    print(f"           {len(graph.nodes)} nodes, {len(graph.edges)} edges")

    for fmt in ("dot", "mermaid", "json"):

        def one(fmt: str = fmt) -> bytes:
            return render(graph, fmt)

        timed(f"render ({fmt})", one, repeat=repeat)

    if compare:
        print()
        fast = libyaml_loader()
        _, pure_ms = timed(
            "parse only (pure Python)",
            lambda: parse_with(PureStrictSafeLoader, files, root),
            repeat=repeat,
        )
        if fast is None:
            print("parse only (libyaml)               -- unavailable in this PyYAML build")
        else:
            count, fast_ms = timed(
                "parse only (libyaml)",
                lambda: parse_with(fast, files, root),
                repeat=repeat,
            )
            print(f"           {count} documents; libyaml is {pure_ms / fast_ms:.1f}x faster")


def _inventory_root(args: argparse.Namespace) -> Iterator[Path]:
    if args.inventory is not None:
        yield Path(args.inventory)
        return
    shape = Shape(sites=args.sites, racks_per_site=args.racks, hosts_per_rack=args.hosts)
    target = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="netgraph-bench-"))
    if target.exists() and args.keep:
        shutil.rmtree(target)
    files, documents = generate(target, shape)
    print(f"generated {files} files / {documents} documents / {shape.devices} devices")
    try:
        yield target
    finally:
        if not args.keep:
            shutil.rmtree(target, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default = Shape()  # ``slots=True`` hides the defaults from the class itself
    parser.add_argument("--sites", type=int, default=default.sites)
    parser.add_argument("--racks", type=int, default=default.racks_per_site)
    parser.add_argument("--hosts", type=int, default=default.hosts_per_rack)
    parser.add_argument("--repeat", type=int, default=3, help="samples per stage (median wins)")
    parser.add_argument("--keep", help="write the tree here and leave it behind")
    parser.add_argument("--inventory", help="time an existing tree instead of generating one")
    parser.add_argument(
        "--compare-loaders",
        action="store_true",
        help="also time the parse step through both YAML parsers",
    )
    args = parser.parse_args(argv)

    for root in _inventory_root(args):
        report(root, repeat=args.repeat, compare=args.compare_loaders)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
