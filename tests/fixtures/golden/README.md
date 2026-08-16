# Renderer golden files

Committed snapshots of the DOT, Mermaid and JSON renderings of the example
inventories, plus the `aggregate/` fixture next door. `tests/test_golden.py`
asserts each renderer reproduces its file byte for byte.

They exist because `netviz render -f dot > topology.dot` is meant to produce a
file worth committing. If the renderer reshuffles attribute order or node order
between releases, every downstream diff becomes noise; these files are what
makes that regression fail a test instead of surprising a user.

| Stem | Inventory | Layer | Options |
|---|---|---|---|
| `home-lab-l1` | `examples/home-lab` | L1 | defaults |
| `home-lab-l2` | `examples/home-lab` | L2 | `title` |
| `campus-l1-plain` | `examples/campus` | L1 | `show_ips=False`, `show_vlans=False` |
| `campus-l2-grouped` | `examples/campus` | L2 | `group_by_namespace=True`, `title`, `max_addresses=2` |
| `home-lab-l3` | `examples/home-lab` | L3 | `title` |
| `campus-l3-grouped` | `examples/campus` | L3 | `group_by_namespace=True`, `title` |
| `overlay-l1` | `examples/overlay` | L1 | `title` |
| `overlay-l2` | `examples/overlay` | L2 | `title` |
| `overlay-overlay` | `examples/overlay` | overlay | `group_by_namespace=True`, `title` |
| `home-lab-l1-interactive` | `examples/home-lab` | L1 | `group_by_namespace=True`, `element_ids=True`, `link_template` |
| `home-lab-l1-inert` | `examples/home-lab` | L1 | `tooltips=False`, `show_ips=False`, `show_vlans=False` |
| `campus-l1-collapsed` | `examples/campus` | L1 | `--collapse-depth 1`, `element_ids=True`, `title` |
| `campus-l1-collapsed-north` | `examples/campus` | L1 | `--collapse sites/north`, `show_ips=False`, `show_vlans=False` |
| `aggregate-l1-lag` | `tests/fixtures/aggregate` | L1 | default bundling: the declared LAG folded, the spare cross-links not |
| `aggregate-l1-bundled` | `tests/fixtures/aggregate` | L1 | `--bundle-links` |
| `aggregate-l1-unbundled` | `tests/fixtures/aggregate` | L1 | `--no-bundle-links` |
| `aggregate-l1-collapsed-bundled` | `tests/fixtures/aggregate` | L1 | `--collapse-depth 1 --bundle-links`, `group_by_namespace=True` |

Most stems have a `.dot`, `.mmd` and `.json` file; a case whose option only one
backend honours keeps the `.dot` alone rather than committing two more
byte-identical copies. The matrix is defined by `CASES` in
`tests/test_golden.py`; a case with a missing file, or a file with no case,
fails the suite.

## Regenerating

```console
$ pytest tests/test_golden.py --regen-golden --no-cov
$ git diff tests/fixtures/golden/
```

Regeneration is never implicit — a snapshot that rewrites itself whenever the
renderer changes asserts nothing. **The diff is the review.** Read it before
committing: an intentional change to a label or an attribute shows up as a
handful of readable lines, and an accidental reordering shows up as a large one.

## Keep them machine-independent

Nothing here may contain an absolute path, a timestamp or a hostname, or the
files would differ between checkouts. `Graph.root` is deliberately absent from
all three formats, and `test_goldens_are_free_of_machine_specific_paths` guards
it.
