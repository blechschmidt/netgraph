# `netgraph test`

Grade the assertions the inventory makes about itself. It exits 1 when one of
them has stopped being true and 0 otherwise, so it drops into CI beside
[`netgraph validate`](validate.md) without any wrapping. Nothing is probed and no
device is contacted: every verdict comes from the files.

`validate` says whether the files are *coherent* — a cable endpoint resolves, an
address is inside its subnet. `test` says whether the network still does what
somebody built it to do. Those are different questions, and only the second one
can be answered by the people who built it, which is why the answers live in the
inventory as [`kind: testsuite`](../schema.md#20-test-suites-executable-assertions)
documents rather than in a rule catalogue.

---

## Contents

- [Synopsis](#synopsis)
- [A passing run](#a-passing-run)
- [A failing run](#a-failing-run)
- [What an assertion looks like](#what-an-assertion-looks-like)
- [Selecting elements](#selecting-elements)
- [Choosing which suites run](#choosing-which-suites-run)
- [Machine-readable output](#machine-readable-output)
- [JUnit, for CI](#junit-for-ci)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)

---

## Synopsis

<!-- generated: synopsis test -->
```text
netgraph [GLOBAL OPTIONS] test [OPTIONS] [SUITE]...
```
<!-- /generated -->

## A passing run

One progress line per suite, then the totals. A green run is short whatever the
size of the inventory — the interesting output of a test run is the part that
failed:

<!-- run: -->
```console
$ netgraph -i examples/home-lab test
ok    home  10 passed  (the house reaches the internet, and the guest VLAN stays guest)

10 passed in 1 suite
```

`-v` adds a line per passing assertion, which is what you want when you are
writing them and not when you are reading a pipeline.

## A failing run

A failure names four things: **which assertion**, **which elements**, **what the
graph actually contained**, and **the file and line the assertion is written
on** — so an editor and a CI annotation can both link straight to it.
`tests/fixtures/testsuite` is a valid two-desk inventory (`netgraph validate` has
nothing to say about it) that fails four of the five claims made about it:

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/testsuite test
FAIL  office  1 passed, 4 failed  (what the office is supposed to guarantee)
  FAIL  the two desks cannot see each other  [not-reachable]  tests.yaml:13
        1 route exists and should not
          pc-a -> pc-b at layer l2: pc-a -> sw -> pc-b
        why: Ticket NET-412 asked for client isolation on the office VLAN.
  FAIL  the office switch has room to grow  [port-count-at-least]  tests.yaml:19
        1 element has fewer than 24 interfaces
          sw declares 5
  FAIL  there is a second router for failover  [count]  tests.yaml:24
        the selector 'kind=router' matches 1 element: 1 is not at least 2
          rtr
  FAIL  no single failure takes the office offline  [no-single-point-of-failure]  tests.yaml:29
        5 single points of failure in the l1 view
          l1: losing link cbl-rtr isolates 3 endpoints (sw, pc-a, pc-b)
          l1: losing node rtr isolates 3 endpoints (sw, pc-a, pc-b)
          l1: losing node sw isolates 2 endpoints (pc-a, pc-b)
          l1: losing link cbl-a isolates 1 endpoint (pc-a)
          l1: losing link cbl-b isolates 1 endpoint (pc-b)

1 passed, 4 failed in 1 suite
```

The `why:` line is the assertion's own `description`. It is printed only under a
failure, which is the moment somebody is asking why anybody ever wanted this.

An assertion that names an element the inventory does not hold is a *failing
test*, not a broken command: it fails with the same shape, so a suite that has
drifted away from the inventory shows up in the report rather than as a
traceback.

## What an assertion looks like

Eleven claims, listed in full in
[§20.2 of the schema](../schema.md#202-an-assertion). The ones that come up most:

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: testsuite
metadata:
  name: connectivity
spec:
  assertions:
    - assert: reachable
      name: a north desk reaches its own file server
      from: pc-north-01
      to: srv-north-01
      layer: l3

    - assert: not-reachable
      name: staff and servers are separated by a router, not by a switch
      from: pc-north-01
      to: srv-north-01
      layer: l2

    - assert: unique
      name: no two switches claim the same management address
      select: kind=switch
      field: spec.interfaces[name=Vlan99].ipv4.addresses[].ip

    - assert: count
      name: there are three site routers, one per campus
      select: kind=router
      equals: 3
```

Those first two are the pair worth understanding, because a segmentation
requirement always has this shape: the two hosts *do* reach each other, and a
switch is not how. `layer:` is what lets both be said.

`--list` prints what would be graded without grading it, which is the fast way to
answer "what does this inventory actually check?":

<!-- run: -->
```console
$ netgraph -i examples/home-lab test --list
home  10 assertions  (the house reaches the internet, and the guest VLAN stays guest)
  the desktop reaches the NAS  [reachable]  tests.yaml:13
  the laptop reaches the router through its USB dongle  [reachable]  tests.yaml:20
  the guest VLAN carries none of the house's traffic  [not-reachable]  tests.yaml:28
  nothing in the house is more than three hops from the router  [path-shorter-than]  tests.yaml:38
  the wired house is one broadcast domain  [same-vlan]  tests.yaml:44
  the house is addressed out of 192.168.10.0/24  [within-prefix]  tests.yaml:49
  both switches have a management SVI  [has-interface]  tests.yaml:54
  no two hosts claim the same address  [unique]  tests.yaml:59
  there is exactly one router  [count]  tests.yaml:67
  the access point is the only wireless switch  [count]  tests.yaml:72
```

## Selecting elements

`select:` is the vocabulary [`netgraph render`](render.md) already filters with,
written as one scalar:

```yaml
select: kind=switch, namespace=sites/north, name=sw-*
```

Comma-separated terms; a repeated key is an alternative, different keys are
combined with AND, and a bare word is short for `name=`. It parses to the very
same filter the renderer uses, so an assertion and a diagram cannot disagree
about what `kind=switch` selects. [§20.3](../schema.md#203-selectors) is the
grammar in full.

`from` and `to` accept a selector too, which turns twelve assertions into one:

```yaml
- assert: reachable
  name: every access switch reaches its site distribution switch
  from: name=sw-north-acc-*
  to: sw-north-dist-01
```

An empty selection **fails** — everywhere except `count`, where `equals: 0` is a
legitimate claim. A test graded against nothing is a test that reports green
having checked nothing.

## Choosing which suites run

`SUITE` narrows the run. It is a glob, matched against the fully-qualified and
the short name, and it is repeatable:

<!-- run: -->
```console
$ netgraph -i examples/campus test connect*
ok    connectivity  20 passed  (staff, servers and management reach what they are supposed to)

20 passed in 1 suite
```

A glob that matches nothing **fails the run** rather than quietly grading zero
assertions:

<!-- run: rc=1 -->
```console
$ netgraph -i examples/campus test segmentation
no test suite matches 'segmentation'
```

## Machine-readable output

`-F json` writes the whole run — every verdict, its detail and its location — for
a script that wants to do something other than print it. The envelope is stable:
`schemaVersion` is bumped only for a change that could break a consumer.

<!-- norun: the envelope quotes an absolute inventory path, which differs per machine -->
```console
$ netgraph -q -i inventory test -F json
{
  "schemaVersion": 1,
  "kind": "TestReport",
  "tool": { "name": "netgraph", "version": "0.1.0" },
  "inventory": "/home/ops/net/inventory",
  "summary": {
    "suites": 1, "assertions": 10,
    "passed": 9, "failed": 1, "skipped": 0,
    "ok": false
  },
  "unmatched": [],
  "suites": [
    {
      "name": "office",
      "state": "failed",
      "description": "what the office is supposed to guarantee",
      "location": { "file": "tests.yaml", "line": 1 },
      "assertions": [
        {
          "index": 1,
          "assert": "not-reachable",
          "name": "the two desks cannot see each other",
          "state": "failed",
          "message": "1 route exists and should not",
          "detail": ["pc-a -> pc-b at layer l2: pc-a -> sw -> pc-b"],
          "elements": ["pc-a", "pc-b"],
          "description": "Ticket NET-412 asked for client isolation on the office VLAN.",
          "location": { "file": "tests.yaml", "line": 13 }
        }
      ]
    }
  ]
}
```

`summary.ok` is the exit code in a field: false exactly when the command exits 1.

## JUnit, for CI

`-F junit` writes the XML GitHub, GitLab and Jenkins all render natively — one
`<testcase>` per assertion, grouped by suite, each carrying the file and line of
the assertion as attributes *and* again in the body of `<failure>`. That is the
format that turns a red pipeline into a list of named, clickable failures rather
than a wall of log:

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/testsuite test -F junit
<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="netgraph test" tests="5" failures="4" errors="0" skipped="0">
  <testsuite name="netgraph test" tests="5" failures="4" errors="0" skipped="0">
    <properties>
      <property name="inventory" value="tests/fixtures/testsuite"/>
      <property name="netgraph" value="0.1.0"/>
    </properties>
    <testcase classname="netgraph.test.office" name="both desks reach the router" file="tests.yaml" line="8"/>
    <testcase classname="netgraph.test.office" name="the two desks cannot see each other" file="tests.yaml" line="13">
      <failure message="1 route exists and should not" type="not-reachable">
at tests.yaml:13
why: Ticket NET-412 asked for client isolation on the office VLAN.
pc-a -&gt; pc-b at layer l2: pc-a -&gt; sw -&gt; pc-b
elements: pc-a, pc-b
      </failure>
    </testcase>
    <testcase classname="netgraph.test.office" name="the office switch has room to grow" file="tests.yaml" line="19">
      <failure message="1 element has fewer than 24 interfaces" type="port-count-at-least">
at tests.yaml:19
sw declares 5
elements: sw
      </failure>
    </testcase>
    <testcase classname="netgraph.test.office" name="there is a second router for failover" file="tests.yaml" line="24">
      <failure message="the selector 'kind=router' matches 1 element: 1 is not at least 2" type="count">
at tests.yaml:24
rtr
elements: rtr
      </failure>
    </testcase>
    <testcase classname="netgraph.test.office" name="no single failure takes the office offline" file="tests.yaml" line="29">
      <failure message="5 single points of failure in the l1 view" type="no-single-point-of-failure">
at tests.yaml:29
l1: losing link cbl-rtr isolates 3 endpoints (sw, pc-a, pc-b)
l1: losing node rtr isolates 3 endpoints (sw, pc-a, pc-b)
l1: losing node sw isolates 2 endpoints (pc-a, pc-b)
l1: losing link cbl-a isolates 1 endpoint (pc-a)
l1: losing link cbl-b isolates 1 endpoint (pc-b)
elements: cbl-rtr, rtr, sw, cbl-a, cbl-b
      </failure>
    </testcase>
  </testsuite>
</testsuites>
```

`-o` writes it to a file instead of stdout, which is what a CI step wants:

<!-- norun: writes into the working tree, and the path is the caller's to choose -->
```bash
netgraph -i inventory test -F junit -o test-results.xml
```

[`docs/ci.md`](../ci.md#netgraph-test-assertions-as-a-gate) has the workflow
snippets for GitHub Actions and GitLab.

## Arguments

<!-- generated: arguments test -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[SUITE]...` | no | any number | — |
<!-- /generated -->

## Options

<!-- generated: options test -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-F`, `--output-format` | `[text\|json\|junit]` | `text` | text is the progress report; json is the whole run for a script; junit is the XML GitHub, GitLab and Jenkins all render as a test report. |
| `-o`, `--output` | `FILE` | — | Write the report to this file instead of stdout. Usual for -F junit. |
| `--max-hops` | `INTEGER, 1-64` | `16` | Abandon a traced route that crosses more links than this, unless the assertion sets its own 'max_hops'. |
| `--list` | — | off | List the suites and their assertions without grading any of them. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Proceed even when validation failed. The result may not match the files. |
<!-- /generated -->

## Exit codes

| Code | When |
|---|---|
| 0 | Every assertion that ran held. |
| 1 | An assertion failed; a `SUITE` glob matched nothing; nothing was graded at all; or the inventory has errors and `--force` was not given. |
| 2 | A usage error — an unknown flag or an unreadable inventory path. |

"Nothing was graded at all" is deliberate, and is what `pytest` does with an
empty collection for the same reason: a run that checked nothing is not a run
that passed.

A **skipped** assertion does not fail the run: a claim about a power plan that an
inventory declaring no PDU cannot be wrong about is genuinely not a claim this
inventory got wrong. It is still counted and still printed, so a suite that
skipped everything cannot pass for a suite that checked everything.

## See also

- [`netgraph validate`](validate.md) — the other CI gate: whether the files cohere.
- [`netgraph path`](path.md) — trace one route interactively, hop by hop.
- [`netgraph impact`](impact.md) — the failure simulation `no-single-point-of-failure` runs.
- [§20 of the schema](../schema.md#20-test-suites-executable-assertions) — the document kind in full.
- [`docs/ci.md`](../ci.md) — putting both gates in a pipeline.
