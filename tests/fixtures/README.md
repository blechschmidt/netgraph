# Test fixtures

| Path | What it is |
|---|---|
| [`aggregate/`](aggregate/) | A clean two-site inventory whose east site declares a four-member `Port-channel1` with two spare cross-links beside it, and which is joined to the west site by two cables that only become parallel once the sites collapse. It exists to exercise `--bundle-links` and `--collapse`; it teaches nothing, which is why it is here and not in `examples/`. |
| [`export/`](export/) | Committed `netviz export` artefacts — one per format over the published examples. Regenerated the same way, and for the same reason: an exported hosts file, zone or pull list is only worth committing if it is byte-stable. |
| [`golden/`](golden/) | Committed renderer snapshots. Regenerate with `pytest --regen-golden` and **review the diff**; a snapshot that rewrites itself asserts nothing. |
| [`import/`](import/) | Captures of real tool output — `lldpctl -f json`, `ip -j addr`, cabling CSV — that `netviz import` is driven with. |
| [`invalid/`](invalid/) | One inventory per validation rule, each tripping exactly that rule. The file name is the rule id. |
| `sarif-schema-2.1.0.json` | Third-party; see below. |

## `sarif-schema-2.1.0.json`

The normative JSON Schema for **SARIF 2.1.0**, used by `tests/test_report.py` to
check the document `netviz validate --output-format sarif` emits.

* **Source:** <https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json>
  — the same URL netviz puts in the `$schema` of every log it emits
  (`netviz.diagnostics.SARIF_SCHEMA_URL`), so the tests check the document against
  what it claims to be. Byte-identical to the copy in the
  [OASIS SARIF TC](https://github.com/oasis-tcs/sarif-spec) repository.
* **Licence:** the [OASIS IPR Policy](https://www.oasis-open.org/policies-guidelines/ipr/).
* **Retrieved:** 2026-07-29. It is a draft-04 schema; `jsonschema` selects the
  matching validator from its own `$schema` key.

It is **vendored rather than fetched at test time** on purpose. A test that
downloads its own oracle fails on an aeroplane, fails when a CDN has a bad day,
and — worse — silently starts asserting something different the day upstream
edits the file. Refresh it deliberately, in its own commit, and read the diff.
