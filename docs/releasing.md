# Releasing netgraph

What a version number promises, which parts of netgraph those promises cover, and the
mechanics of cutting a release.

This page is for maintainers, but the first two sections are for everyone: they are the
compatibility contract, and the reason a `0.x` version number is not an invitation to break
things quietly.

## The versioning policy

netgraph uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html): `MAJOR.MINOR.PATCH`.
While the major version is `0`, SemVer allows anything at all to change in a minor release.
netgraph does not use that latitude in full. What `0.x` promises:

| Change | Where it lands | What you must do |
|---|---|---|
| New command, new flag, new schema field, new validation rule at `info`/`warning` | `0.x.**y+1**` | Nothing. |
| Bug fix that changes output because the old output was wrong | `0.x.**y+1**` | Nothing, but read the entry. |
| A rule promoted to `error`, a default changed, a documented output reshaped, a flag or field renamed or removed | `0.**x+1**.0` | Read the `### Changed` and `### Removed` entries; a migration note is there when one is needed. |
| Anything that requires you to edit an inventory that validated cleanly before | `0.**x+1**.0`, with a migration note | Follow the note. |

So a minor bump is the signal to read the changelog before upgrading, and a patch bump is
not. That is a weaker promise than `1.x` will make and a stronger one than `0.x` requires.

Two things `0.x` explicitly does **not** promise:

- **The schema is `v1alpha1` and the `alpha` is meant.** `netgraph.dev/v1alpha1` may gain
  fields freely and may lose or rename them in a minor release. When that happens, the
  `apiVersion` string does not change — `v1alpha1` is the whole alpha line — but the change
  is recorded as `### Changed` or `### Removed` with the edit an existing inventory needs.
  §12 of [`schema.md`](schema.md#12-compatibility-policy) is the normative version of this.
- **No Python API stability.** See "internal", below.

`1.0.0` is the version at which the schema graduates to `netgraph.dev/v1`, and at that point
the alpha latitude above goes away.

### Pre-releases

`0.2.0rc1` and friends are spelled the PEP 440 way — `a1`, `b1`, `rc1`, `.post1`, `.dev1`,
no hyphen — because that is what the tag check accepts and what `pip install netgraph==…`
resolves. A pre-release is published to PyPI like any other version; `pip install netgraph`
will not pick it up without `--pre`.

## What is public API

These are the surfaces a change to which is a **breaking change** and must be recorded as
such. They are public because something outside this repository depends on their exact
shape: a shell script, a pipeline, an editor, a pinned schema, a colleague's inventory.

1. **The CLI.** Command names, flag names and their short forms, argument order, the values
   an enumerated flag accepts, and the defaults. Adding a flag is not breaking; renaming or
   removing one is, and so is changing what a flag defaults to.
   [`docs/commands/`](commands/README.md) is generated from Click, so it is also the
   inventory of this surface.
2. **The `netgraph.dev/v1alpha1` document schema.** Kinds, field names, value spaces,
   required-ness, and how references resolve. Normatively
   [`schema.md`](schema.md); field by field, [`schema-reference.md`](schema-reference.md);
   machine-readably, `netgraph schema`.
3. **The JSON output documents.** Each carries a `schemaVersion`, and each has its own:
   - `netgraph validate --output-format json` — the findings envelope ([ci.md](ci.md)).
   - `netgraph validate --output-format sarif` — SARIF 2.1.0, including the rule ids and
     the `helpUri` anchors, because code scanning deduplicates alerts on them.
   - `netgraph render -f json` — the resolved graph.
   - `netgraph drift --output-format json`, `netgraph path --output-format json`,
     `netgraph ipam --output-format json`, `netgraph version --json`.

   Adding a key does not bump a `schemaVersion`. Removing one, renaming one, or changing
   what an existing key means does, and that is a minor release.
4. **The exit codes.** `0` success, `1` the command's own negative answer (findings,
   `--check` differences, no path found), `2` usage error, `130` interrupted. A command that
   started returning `1` where it used to return `0` is a breaking change even if nothing
   else about it moved.
5. **The rule ids and their `NG-*` aliases.** They appear in `--disable` lists, in
   `netgraph.toml`, in suppression comments inside inventories and in code-scanning alert
   history. A rule may be added; an id may not be reused for a different rule, and a rule's
   severity may only be *raised* in a minor release.
6. **The published integrations.** The three `pre-commit` hook ids, and the inputs and
   outputs of the `netgraph-validate` composite action. Somebody else's
   `.pre-commit-config.yaml` and workflow name them.
   [`tests/test_integrations.py`](../tests/test_integrations.py) asserts them against the CLI.
7. **The environment variables** netgraph reads: `NETGRAPH_DOT`, `NETGRAPH_YAML_LOADER`,
   `NO_COLOR`, and the ones the compose file documents in
   [`.env.example`](../.env.example).
8. **The container image reference** `ghcr.io/blechschmidt/netgraph`, its entrypoint
   (`netgraph` itself), its working directory (`/inventory`) and the uid it runs as.

### What is internal

Not public, changeable in a patch release, and no entry required:

- **Every Python module under `netgraph.*`.** The package is a tool, not a library: there is
  no `__all__` you can rely on, no deprecation cycle, and `netgraph.render.dot._dot_string`
  is as internal as it looks. [`architecture.md`](architecture.md#using-it-as-a-library)
  describes using it from Python anyway and says the same thing: pin an exact version.
- **The rendered output itself** — the DOT source, the SVG markup, node ordering, colours,
  the exact wording of a diagnostic message. Diagrams are for humans, and improving one is
  not a breaking change. The `render -f json` document is the stable surface for anything
  that wants to *parse* a topology.
- **The `schema/` directory in this repository**, which is a regenerable artefact of
  `netgraph schema`.
- **The golden fixtures, the example inventories and the benchmark scripts.**

### How a breaking change is recorded

A change to any of the eight public surfaces above needs all four of these, in the same pull
request:

1. A `### Changed` or `### Removed` bullet under `## [Unreleased]` in
   [`CHANGELOG.md`](../CHANGELOG.md) that names the surface, the old shape and the new one.
2. A **migration line** in that bullet if an existing inventory, script or pipeline has to be
   edited — literally what to change it to. "Renamed `--foo` to `--bar`" is the entry;
   "replace `--foo=x` with `--bar=x`" is the migration.
3. A minor version bump, not a patch one, when the version is next cut.
4. A test that fails on the old behaviour, so the change is deliberate rather than a
   regression somebody will later "fix" back.

For the schema specifically, §12 of [`schema.md`](schema.md#12-compatibility-policy) also has
to say the same thing, because that document is normative and the changelog is not.

## Cutting a release

Everything below happens on `main`, in this order. Nothing here needs a local PyPI token or a
local Docker login — the workflow publishes, and it authenticates with OIDC.

### 1. Prepare the version

```bash
# 1. Decide the number from the Unreleased section: any Changed/Removed entry -> minor.
# 2. Set it in pyproject.toml.
# 3. Rename '## [Unreleased]' to '## [X.Y.Z] - YYYY-MM-DD' and open a fresh Unreleased
#    above it, then update the two link definitions at the bottom of CHANGELOG.md.
# 4. Regenerate the committed artefacts that name the version:
python tools/gen_example_report.py   # docs/example-report/ carries it on every page
```

Check it locally before pushing anything — this is the same code the workflow's first job
runs, so a failure here is a failure there:

<!-- norun: writes release-notes.md into the reader's checkout -->
```bash
python tools/release.py check --ref "v$(python tools/release.py version)"
```

It prints the version, the line the changelog section starts on and the number of lines of
notes it extracted, or exits `1` with what to fix.

### 2. Dry run

Push the commit, then run the release workflow manually:

```text
Actions -> release -> Run workflow -> main
```

`workflow_dispatch` runs every step of the real thing except the irreversible ones: it
builds, checks with `twine check --strict`, installs the wheel and the sdist into clean
virtualenvs on Linux, macOS and Windows, builds the container image for both architectures,
and publishes to **TestPyPI** instead of PyPI. It creates no tag, no GitHub release, no GHCR
tag and no attestation. A green dry run means the only thing left untested is the upload
itself.

### 3. Tag

```bash
git tag -a "v0.2.0" -m "netgraph 0.2.0"
git push origin "v0.2.0"
```

The tag is what triggers the real release. It must be on a commit whose `pyproject.toml`
already carries that version — the first job refuses the run otherwise, before anything is
built.

### 4. Watch it, and what to do if it fails

The workflow is ordered so that the reversible work happens first and the irreversible work
happens last:

| Job | Reversible? | If it fails |
|---|---|---|
| `guard` | yes | Fix the version or the changelog, delete the tag, re-tag. |
| `ci` | yes | Fix the code. Same as above. |
| `build` | yes | Fix the packaging. Same as above. |
| `verify` (Linux, macOS, Windows) | yes | Same. This is the last chance. |
| `pypi` | **no** | The version is burnt. Do not reuse it: fix, bump the patch, release again. |
| `image` | mostly | A tag can be re-pushed; a digest cannot be unpublished. |
| `github-release` | yes | Re-run the job; it creates or updates the release from the same artefacts. |

Deleting and re-pushing a tag is only safe before `pypi` has run. Afterwards the version
exists in the world and the fix is a new version — PyPI does not allow re-uploading a file,
and it should not.

## What the release workflow does

[`.github/workflows/release.yml`](../.github/workflows/release.yml), triggered by a `v*` tag
and by `workflow_dispatch`.

1. **`guard`** — `tools/release.py check` on the tag: the tag matches
   `pyproject.toml`, the version is spelled the PEP 440 way, and `CHANGELOG.md` has a dated,
   non-empty section for it. The section is extracted here and carried forward as an
   artefact, so the GitHub release body and the changelog cannot disagree.
2. **`ci`** — the whole of [`ci.yml`](../.github/workflows/ci.yml), called as a reusable
   workflow. The same gate a pull request passes: the full matrix, the examples, the
   container. Not a subset, because "it was green on main" is not the same statement as "it
   is green at this tag".
3. **`build`** — `python -m build` for the sdist and the wheel, `twine check --strict`, and a
   CycloneDX SBOM of the wheel's dependency closure.
4. **`verify`** — on `ubuntu-latest`, `macos-14` and `windows-latest`: a fresh virtualenv,
   `pip install` the wheel, and `netgraph --version` from the installed console script. Then
   the same again from the **sdist**, which additionally proves the sdist builds — an sdist
   that unpacks but does not build is the classic release-day surprise. Neither install uses
   the checkout, so a missing package or a missing data file fails here.
5. **`pypi`** — [Trusted Publishing](https://docs.pypi.org/trusted-publishers/): the runner
   exchanges its OIDC token for a short-lived upload token, so this repository stores no PyPI
   credential of any kind and there is nothing to leak or rotate. On `workflow_dispatch` the
   target is TestPyPI instead, and `skip-existing` is on there because a dry run of an
   already-dry-run version is not an error.
6. **`provenance`** — `actions/attest-build-provenance` over the sdist and the wheel, so
   `gh attestation verify` can tie a downloaded file to this repository, this workflow and
   this commit.
7. **`image`** — `linux/amd64` and `linux/arm64` to `ghcr.io/blechschmidt/netgraph`, tagged
   `X.Y.Z`, `X.Y` and `latest`, with an SPDX SBOM and a provenance attestation of the image
   digest.
8. **`github-release`** — the release, with the changelog section as the body, the sdist, the
   wheel and both SBOMs attached, and the image digest recorded in the notes.

Every job declares its own `permissions` and every third-party action is pinned to a commit
SHA. Three jobs ask for more than `contents: read` and each says why in a comment: `pypi`
needs `id-token: write` for the OIDC exchange, `provenance` and `image` need
`attestations: write`, and `github-release` needs `contents: write` to create the release.
The `permissions` block of a job that publishes is the whole of its blast radius, so it is
written per job rather than once at the top of the file.

### Pinning

`release.yml` pins every action to a commit SHA with the tag in a trailing comment:

```yaml
uses: pypa/gh-action-pypi-publish@7f25271a4aa483500f742f9492b2ab5648d61011  # v1.12.4
```

`ci.yml` does not, and the difference is deliberate: a compromised action in `ci.yml` can
read a checkout that is already public, while one in `release.yml` runs in a job holding a
token that can publish to PyPI and push to GHCR under this project's name. To bump a pin,
resolve the tag and replace both the SHA and the comment:

```bash
git ls-remote --tags https://github.com/pypa/gh-action-pypi-publish v1.12.4
```

[`tests/test_release.py`](../tests/test_release.py) fails if any `uses:` in `release.yml`
names a tag or a branch instead of a 40-character SHA, or if the trailing comment is missing.

## Registering the trusted publisher

Once per repository, before the first release. On PyPI, under the project's *Publishing*
settings (or as a *pending* publisher if the name is not yet claimed):

| Field | Value |
|---|---|
| Owner | `blechschmidt` |
| Repository | `netgraph` |
| Workflow name | `release.yml` |
| Environment | `pypi` |

Repeat on TestPyPI with the environment `testpypi`. The two GitHub environments of those
names are what make the mapping specific: without them any workflow in the repository could
mint an upload token, and with them only a job that names the environment can — which is why
`pypi` and `testpypi` are the only jobs that do.

Nothing needs to be registered for GHCR: `GITHUB_TOKEN` with `packages: write` is enough, and
the package inherits the repository's visibility.

## How this is kept honest

[`tests/test_release.py`](../tests/test_release.py) asserts that the version in
`pyproject.toml` is spelled correctly and has a matching, dated, non-empty changelog section;
that the tag check rejects a mismatch, a missing section, an empty section and an undated
heading; that `netgraph --version` and `netgraph version --json` report the package, Python
and Graphviz versions; and that the release workflow pins its actions, keeps its permissions
per job, and names the environments the trusted publisher expects. So a release that would
fail at the gate fails on the pull request instead.
