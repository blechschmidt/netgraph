# Trace golden files

Committed snapshots of `netgraph path` against the example inventories.
`tests/test_path.py` asserts each report reproduces its file byte for byte.

They exist for the same reason the renderer goldens next door do, and for one
more: a trace is *derived* — it is the output of two graph searches over a
resolution pass — so a change anywhere in the pipeline can quietly change an
answer without breaking a single unit test. These files are what turns that into
a readable diff.

| Stem | Inventory | Question | What it pins |
|---|---|---|---|
| `home-lab-adapter` | `examples/home-lab` | `laptop` → `pc-desk` | an adapter attachment as a hop (§8.2), and the highlighted DOT rendering |
| `campus-l2` | `examples/campus` | `pc-north-01` → `pc-north-02` | a switched path across two trunks, with the VLAN the trace assumed |
| `campus-l3` | `examples/campus` | `10.1.10.51` → `10.2.20.11`, `--all` | address endpoints, a routed path, and the redundant pair the backbone ring creates |
| `campus-none` | `examples/campus` | `pc-north-01` → an unpatched port | a trace that finds nothing, and the frontier that locates the break |
| `overlay-vxlan` | `examples/overlay` | `rtr-hq` → `rtr-branch-b`, `--vlan 100` | a layer-2 tunnel crossed inside a VLAN, with `vxlan over ipsec` and what protects it |
| `overlay-l3` | `examples/overlay` | `pc-branch-a` → `srv-hq` | a routed hop realised by a WireGuard tunnel, and the overlay beating the underlay on hop count |

Each stem has a `.txt` (the hop-by-hop report) and a `.json` file;
`home-lab-adapter` also has a `.dot` of the `--highlight` rendering. The matrix
is defined by `CASES` in `tests/test_path.py`; a case with a missing file, or a
file with no case, fails the suite.

## Regenerating

```console
$ pytest tests/test_path.py --regen-golden --no-cov
$ git diff tests/fixtures/golden/path/
```

Regeneration is never implicit. **The diff is the review** — read it before
committing: a reworded note shows up as one line, and a path that changed shape
shows up as a block.

## Keep them machine-independent

Nothing here may contain an absolute path, a timestamp or a hostname, or the
files would differ between checkouts. `test_goldens_are_free_of_machine_specific_paths`
guards it.
