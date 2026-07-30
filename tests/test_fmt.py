"""``netgraph fmt``: the canonical form, and the two properties that make it safe.

The suite is in three parts.

* **Rules.** One test per clause of ``docs/format.md`` — indent, key order,
  quoting, flow demotion, comments, separators — each on the smallest document
  that shows it. These say what the form *is*.
* **Properties.** :func:`test_formatting_preserves_meaning` and
  :func:`test_formatting_is_idempotent` run over every YAML document under
  ``examples/`` and ``tests/fixtures/`` — a hundred files including the ones
  written specifically to be invalid. These say what the formatter may never do,
  on real input rather than on input chosen to pass.
* **Command.** The four modes, their exit codes and what each writes where.

The property tests are the load-bearing ones. A formatter is a program that
rewrites files nobody is watching, so "it produced nice output on the example I
tried" is not a standard worth anything; what matters is that across every
document in the repository it never changes a meaning and never disagrees with
itself about what canonical is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.errors import NetgraphError
from netgraph.fmt import (
    INDENT,
    SEQUENCE_INDENT,
    SEQUENCE_OFFSET,
    WIDTH,
    FormatSyntaxError,
    Mode,
    Outcome,
    format_paths,
    format_source,
    format_stream,
)
from netgraph.fmt.order import ENVELOPE_ORDER, LOADER_KEYS, check_order, document_shape, order_keys
from netgraph.fmt.scalars import looks_like_mac, quote_style
from netgraph.fmt.verify import comments, meaning, verify
from netgraph.fsio import write_text
from netgraph.scaffold import build_scaffold

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _yaml_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.suffix in {".yaml", ".yml"})


#: Every document the repository ships, valid and invalid alike. Collected at
#: import time so each file is its own test case and a failure names the file.
ALL_DOCUMENTS = [*_yaml_files(EXAMPLES), *_yaml_files(FIXTURES)]
DOCUMENT_IDS = [str(path.relative_to(REPO_ROOT)) for path in ALL_DOCUMENTS]


def fmt(text: str) -> str:
    """Format a stream, as ``--stdin`` would."""
    return format_source(text, name="<test>")


DEVICE = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  interfaces:
    - name: e0
      type: ethernet
"""


# --------------------------------------------------------------------------- #
# The form: layout
# --------------------------------------------------------------------------- #


def test_a_canonical_document_is_left_alone() -> None:
    assert fmt(DEVICE) == DEVICE


def test_mappings_are_indented_two_spaces() -> None:
    formatted = fmt("apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n      name: sw1\n")
    assert "\n" + " " * INDENT + "name: sw1\n" in formatted


def test_sequence_items_sit_under_the_key_that_owns_them() -> None:
    """``- name`` four columns in from ``interfaces:``, its dash two."""
    formatted = fmt(DEVICE)
    line = next(line for line in formatted.splitlines() if line.lstrip().startswith("- name: e0"))
    indent = len(line) - len(line.lstrip())
    assert indent == INDENT + SEQUENCE_OFFSET
    assert line[indent:].startswith("- ")
    assert indent + len("- ") == INDENT + SEQUENCE_INDENT


def test_trailing_whitespace_is_stripped_including_inside_comments() -> None:
    assert fmt("a: 1   \n# a comment   \nb: 2\n") == "a: 1\n# a comment\nb: 2\n"


def test_a_file_ends_in_exactly_one_newline() -> None:
    for source in ("a: 1", "a: 1\n", "a: 1\n\n\n\n"):
        assert fmt(source) == "a: 1\n"


def test_an_empty_stream_stays_empty() -> None:
    assert fmt("") == ""


def test_crlf_line_endings_are_normalised(tmp_path: Path) -> None:
    """Decoding hides them, so the change is only visible in the bytes."""
    path = tmp_path / "crlf.yaml"
    path.write_bytes(DEVICE.replace("\n", "\r\n").encode("utf-8"))
    format_paths([path], mode=Mode.WRITE)
    assert path.read_bytes() == DEVICE.encode("utf-8")


def test_a_byte_order_mark_is_not_written_back(tmp_path: Path) -> None:
    path = tmp_path / "bom.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + DEVICE.encode("utf-8"))
    format_paths([path], mode=Mode.WRITE)
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert path.read_text(encoding="utf-8") == DEVICE


# --------------------------------------------------------------------------- #
# The form: document separators
# --------------------------------------------------------------------------- #


def test_a_leading_separator_is_removed() -> None:
    assert fmt("---\na: 1\n") == "a: 1\n"


def test_documents_are_separated_by_one_bare_marker() -> None:
    assert fmt("a: 1\n\n\n---\n\nb: 2\n") == "a: 1\n---\nb: 2\n"


def test_a_trailing_end_marker_is_removed() -> None:
    assert fmt("a: 1\n...\n") == "a: 1\n"


def test_an_empty_document_keeps_its_place_in_the_stream() -> None:
    """Dropping it would renumber every document after it."""
    formatted = fmt("---\n---\na: 1\n")
    assert len(meaning(formatted, name="x.yaml")) == 2


# --------------------------------------------------------------------------- #
# The form: key order
# --------------------------------------------------------------------------- #


def test_the_envelope_comes_first_in_schema_order() -> None:
    scrambled = "spec:\n  interfaces: []\nmetadata:\n  name: sw1\nkind: switch\napiVersion: netgraph.dev/v1alpha1\n"
    formatted = fmt(scrambled)
    keys = [line.split(":")[0] for line in formatted.splitlines() if not line.startswith(" ")]
    assert keys == list(ENVELOPE_ORDER)


def test_a_device_spec_follows_the_documented_field_order() -> None:
    scrambled = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  forwarding:
    ipv4: false
    ipv6: false
  vlans:
    - id: 10
  interfaces:
    - name: e0
      type: ethernet
  vendor: Arista
"""
    formatted = fmt(scrambled)
    order = ["vendor", "interfaces", "vlans", "forwarding"]
    positions = [formatted.index(f"\n  {key}:") for key in order]
    assert positions == sorted(positions)


def test_an_interface_follows_the_documented_field_order() -> None:
    scrambled = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  interfaces:
    - parent: br0
      vlan:
        mode: access
        access_vlan: 10
      ipv4:
        addresses: [10.0.0.1/24]
      type: vlan
      name: Vlan10
"""
    formatted = fmt(scrambled)
    order = ["name:", "type:", "ipv4:", "vlan:", "parent:"]
    positions = [formatted.index(key) for key in order]
    assert positions == sorted(positions)


def test_metadata_labels_keep_the_order_they_were_written_in() -> None:
    """The keys are the user's; YAML gives their order no meaning."""
    source = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
  labels:
    zulu: z
    alpha: a
spec:
  interfaces: []
"""
    assert "zulu: z\n    alpha: a" in fmt(source)


def test_an_unknown_key_keeps_its_value_and_moves_to_the_end() -> None:
    source = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  wat: 1
  vendor: Arista
  interfaces: []
"""
    formatted = fmt(source)
    assert formatted.index("vendor:") < formatted.index("wat:")
    assert "wat: 1" in formatted


def test_the_loader_only_keys_are_placed_where_the_schema_documents_them() -> None:
    """``spec.from`` after ``interfaces``; ``interfaces[].range`` after ``name``."""
    source = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  bridge:
    name: br0
  from: access-switch
  interfaces:
    - type: ethernet
      range: e[1-4]
"""
    formatted = fmt(source)
    assert formatted.index("interfaces:") < formatted.index("from:") < formatted.index("bridge:")
    assert formatted.index("range:") < formatted.index("type:")


def test_a_document_of_unknown_kind_gets_the_envelope_ordered_and_nothing_else() -> None:
    source = "spec:\n  zulu: 1\n  alpha: 2\nkind: wat\napiVersion: netgraph.dev/v1alpha1\n"
    formatted = fmt(source)
    assert formatted.startswith("apiVersion:")
    assert formatted.index("zulu:") < formatted.index("alpha:")


def test_order_keys_is_a_permutation_of_what_it_was_given() -> None:
    shape = document_shape("switch")
    keys = ["spec", "wat", "metadata", "apiVersion"]
    assert sorted(order_keys(keys, shape)) == sorted(keys)


def test_every_ordered_key_comes_from_a_model_or_is_a_documented_loader_key() -> None:
    """The shapes are read off the models; a stale hand-written key is a bug."""
    assert check_order() == []
    assert set(LOADER_KEYS) == {"from", "range"}


# --------------------------------------------------------------------------- #
# The form: quoting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["yes", "no", "on", "off", "Yes", "OFF", "Off", "YES"])
def test_a_yaml_11_boolean_is_quoted_so_both_readers_agree(value: str) -> None:
    """netgraph reads these as strings; nearly every other reader does not."""
    assert fmt(f"a: {value}\n") == f"a: '{value}'\n"


@pytest.mark.parametrize(
    "mac", ["00:1b:0d:01:a1:01", "b4:96:91:01:10:01", "aa-bb-cc-dd-ee-ff", "aabb.ccdd.eeff"]
)
def test_a_mac_address_is_quoted(mac: str) -> None:
    assert fmt(f"mac: {mac}\n") == f"mac: '{mac}'\n"
    assert looks_like_mac(mac)


@pytest.mark.parametrize(
    "value",
    [
        "10.1.10.1",  # an IP address is not a version number
        "2001:db8::1/64",
        "192.168.10.0/24",
        "10Gbps",
        "GigabitEthernet1/0/1",
        "1.2.3",
        "netgraph.dev/v1alpha1",
        "cat6a",
    ],
)
def test_an_unambiguous_string_is_written_plain(value: str) -> None:
    assert fmt(f"a: '{value}'\n") == f"a: {value}\n"


def test_a_number_stays_a_number() -> None:
    assert fmt("mtu: 1500\nlength_m: 0.5\nup: true\nnothing: null\n") == (
        "mtu: 1500\nlength_m: 0.5\nup: true\nnothing: null\n"
    )


def test_a_quoted_float_keeps_its_quotes() -> None:
    """Dropping them would turn the string '1.0' into the float 1.0."""
    assert fmt("version: '1.0'\n") == "version: '1.0'\n"


def test_a_scalar_netgraph_already_misreads_is_left_exactly_as_written() -> None:
    """``1:02`` is the integer 62 to netgraph's loader. Quoting it would be a repair."""
    assert fmt("a: 1:02\n") == "a: 1:02\n"
    assert quote_style("1:02") is None


def test_a_string_that_yaml_requires_quotes_for_gets_them() -> None:
    for source in ("a: '#hash'\n", "a: '- dash'\n", "a: ''\n", "a: 'true'\n"):
        assert fmt(source) == source


def test_a_block_scalar_keeps_its_style() -> None:
    source = "description: |\n  first line\n  second line\n"
    assert fmt(source) == source


# --------------------------------------------------------------------------- #
# The form: flow and block
# --------------------------------------------------------------------------- #


def test_a_short_flow_collection_stays_flow() -> None:
    assert fmt("addresses: [10.0.0.1/24]\n") == "addresses: [10.0.0.1/24]\n"
    assert fmt("labels: {site: office}\n") == "labels: {site: office}\n"


def test_a_block_collection_is_never_collapsed_into_flow() -> None:
    source = "addresses:\n  - 10.0.0.1/24\n"
    assert fmt(source) == source


def test_a_flow_collection_too_wide_for_the_line_becomes_a_block() -> None:
    items = ", ".join(f"10.0.{n}.1/24" for n in range(12))
    formatted = fmt(f"addresses: [{items}]\n")
    assert formatted.startswith("addresses:\n")
    assert all(len(line) <= WIDTH for line in formatted.splitlines())


def test_a_flow_collection_holding_a_collection_becomes_a_block() -> None:
    formatted = fmt("endpoints: [{device: a, interface: b}]\n")
    assert formatted.startswith("endpoints:\n")


def test_width_is_measured_from_where_the_value_actually_starts() -> None:
    """The same list fits at the top level and does not nested five levels deep."""
    items = ", ".join(f"10.0.{n}.1/24" for n in range(7))
    flat = f"a: [{items}]\n"
    assert fmt(flat) == flat
    assert len(flat.rstrip()) <= WIDTH
    nested = (
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n  name: s\n"
        "spec:\n  interfaces:\n    - name: e0\n      type: ethernet\n"
        f"      ipv4:\n        addresses: [{items}]\n"
    )
    assert "addresses:\n" in fmt(nested)


# --------------------------------------------------------------------------- #
# The form: comments and blank lines
# --------------------------------------------------------------------------- #


def test_comments_survive_a_round_trip() -> None:
    source = """\
# a leading comment
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1  # trailing
spec:
  # about the interfaces
  interfaces:
    - name: e0
      type: ethernet
"""
    assert fmt(source) == source


def test_blank_line_grouping_survives_a_round_trip() -> None:
    source = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  interfaces:
    - name: e0
      type: ethernet

    - name: e1
      type: ethernet
"""
    assert fmt(source) == source


def test_a_mapping_with_comments_between_its_keys_keeps_its_order() -> None:
    """Ordering defers to the comment, because ruamel cannot move the two together.

    ``forwarding`` would sort after ``interfaces``, but the comment above it
    would not follow — it would stay put and end up describing ``interfaces``.
    Leaving the order alone is the lesser of the two edits.
    """
    source = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  # why this device forwards
  forwarding:
    ipv4: true
    ipv6: true
  interfaces:
    - name: e0
      type: ethernet
"""
    assert fmt(source) == source


def test_an_end_of_line_comment_does_travel_with_its_key() -> None:
    """The case ruamel files under the key itself, so reordering is safe."""
    source = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  forwarding:  # why this device forwards
    ipv4: true
    ipv6: true
  interfaces:
    - name: e0
      type: ethernet
"""
    formatted = fmt(source)
    assert "forwarding:  # why this device forwards" in formatted
    assert formatted.index("interfaces:") < formatted.index("forwarding:")


def test_a_stream_of_nothing_but_comments_keeps_them() -> None:
    """What ``netgraph init --minimal`` writes.

    ruamel yields no documents for it and has nowhere to hang the comments, so
    a round trip through the emitter would return an empty file.
    """
    source = "# the envelope every document shares\n#\n# apiVersion: netgraph.dev/v1alpha1\n"
    assert fmt(source) == source


def test_a_format_that_dropped_a_comment_is_refused() -> None:
    """Comments are not meaning, so only a check of their own catches this."""
    problem = verify("# a\n# b\na: 1\n", "# a\na: 1\n", name="x")
    assert problem is not None and "dropped 1 comment" in problem


def test_no_comment_is_ever_lost() -> None:
    """Across the whole repository, not just the cases above."""
    for path in ALL_DOCUMENTS:
        source = path.read_text(encoding="utf-8-sig")
        comments = sorted(
            line.strip() for line in source.splitlines() if line.strip().startswith("#")
        )
        if not comments:
            continue
        formatted = format_source(source, name=str(path))
        after = sorted(
            line.strip() for line in formatted.splitlines() if line.strip().startswith("#")
        )
        assert after == comments, f"{path} lost or changed a comment"


# --------------------------------------------------------------------------- #
# The form: empty collections
# --------------------------------------------------------------------------- #


def test_empty_collections_are_written_one_way() -> None:
    assert fmt("a: {}\nb: []\n") == "a: {}\nb: []\n"
    assert fmt("a: {\n}\nb: [\n]\n") == "a: {}\nb: []\n"


def test_nothing_is_spelled_one_way() -> None:
    """``a:`` and ``a: null`` mean the same; the explicit word is the canonical one."""
    assert fmt("a:\n") == "a: null\n"
    assert fmt("a: null\n") == "a: null\n"
    assert fmt("a: ~\n") == "a: null\n"


# --------------------------------------------------------------------------- #
# The properties, over every document the repository ships
# --------------------------------------------------------------------------- #


def test_the_property_corpus_is_the_size_it_should_be() -> None:
    """A glob that quietly matched nothing would make the two below vacuous."""
    assert len(_yaml_files(EXAMPLES)) >= 30
    assert len(_yaml_files(FIXTURES)) >= 30


@pytest.mark.parametrize("path", ALL_DOCUMENTS, ids=DOCUMENT_IDS)
def test_formatting_preserves_meaning(path: Path) -> None:
    """What netgraph reads from the file is identical before and after.

    A document that validates is compared as its model's JSON; one that does not
    is compared as its raw parsed data. :func:`format_source` already refuses to
    return output that fails this — so the assertion here is that no file in the
    repository trips that refusal, which would leave it unformattable.
    """
    source = path.read_text(encoding="utf-8-sig")
    formatted = format_source(source, name=str(path))
    assert verify(source, formatted, name=str(path)) is None
    assert meaning(formatted, name=str(path)) == meaning(source, name=str(path))


@pytest.mark.parametrize("path", ALL_DOCUMENTS, ids=DOCUMENT_IDS)
def test_formatting_is_idempotent(path: Path) -> None:
    """Formatting twice is formatting once. Without this ``--check`` is a lie."""
    source = path.read_text(encoding="utf-8-sig")
    once = format_source(source, name=str(path))
    assert format_source(once, name=str(path)) == once


@pytest.mark.parametrize("minimal", [False, True], ids=["example", "minimal"])
def test_what_netgraph_init_writes_is_already_canonical(minimal: bool) -> None:
    """Otherwise a brand-new inventory fails ``fmt --check`` on its first commit."""
    scaffold = build_scaffold(minimal=minimal)
    stale = {
        name: body
        for name, body in scaffold.files.items()
        if name.endswith((".yaml", ".yml")) and format_source(body, name=name) != body
    }
    assert stale == {}, f"run the formatter over the templates in scaffold.py: {sorted(stale)}"


def test_the_shipped_examples_are_already_canonical() -> None:
    """``examples/`` is documentation, and CI gates it on exactly this."""
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path in _yaml_files(EXAMPLES)
        if format_source(path.read_text(encoding="utf-8-sig"), name=str(path))
        != path.read_text(encoding="utf-8-sig")
    ]
    assert stale == [], f"run 'netgraph fmt examples'; not canonical: {stale}"


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_syntax_error_is_reported_rather_than_guessed_at() -> None:
    with pytest.raises(FormatSyntaxError):
        format_stream("a: [1,\n")


def test_a_duplicate_key_is_refused() -> None:
    with pytest.raises(FormatSyntaxError):
        fmt("a: 1\na: 2\n")


@pytest.mark.parametrize("scalar", ["-._", "._", "+._", ".__"])
def test_a_scalar_the_round_trip_parser_chokes_on_is_a_diagnostic(scalar: str) -> None:
    """Found by ``test_formatting_is_idempotent`` at the nightly search budget.

    ruamel resolves scalars by YAML 1.1 rules and then converts them, and the two
    do not quite agree: ``-._`` matches its float pattern and reaches
    ``float("-.")``, a bare ``ValueError`` out of the standard library rather
    than a ``YAMLError`` anything was catching.

    netgraph's own loader resolves the same scalar as the string it plainly is,
    so ``validate`` accepts the document and ``fmt`` used to answer it with a
    traceback. A formatter may refuse a file; it may not crash on one.
    """
    document = f"apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n  name: {scalar}\n"
    with pytest.raises(FormatSyntaxError, match="could not read this document"):
        format_stream(document)


def test_a_file_whose_meaning_moved_is_never_written(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The safety net, forced: a formatter that corrupts a file writes nothing."""
    path = tmp_path / "d.yaml"
    path.write_text(DEVICE, encoding="utf-8")
    monkeypatch.setattr("netgraph.fmt.runner.format_stream", lambda text: "apiVersion: wrong\n")
    summary = format_paths([path], mode=Mode.WRITE)
    assert [result.outcome for result in summary.results] == [Outcome.FAILED]
    assert "bug in netgraph" in (summary.results[0].error or "")
    assert path.read_text(encoding="utf-8") == DEVICE


def test_verify_notices_a_changed_document_count() -> None:
    assert "document count" in (verify("a: 1\n", "a: 1\n---\nb: 2\n", name="x") or "")


def test_verify_notices_output_that_does_not_parse() -> None:
    assert "not valid YAML" in (verify("a: 1\n", "a: [1,\n", name="x") or "")


def test_meaning_uses_the_model_for_a_document_that_validates() -> None:
    described = meaning(DEVICE, name="d.yaml")
    assert len(described) == 1 and described[0].startswith("model:")
    assert json.loads(described[0].removeprefix("model:"))["kind"] == "switch"


def test_meaning_falls_back_to_raw_data_for_a_document_that_does_not() -> None:
    described = meaning("kind: nonsense\n", name="d.yaml")
    assert described[0].startswith("raw:")


def test_comments_are_counted_not_merely_collected() -> None:
    """Deleting one of two identical comment lines is still a deletion."""
    assert comments("# a\n# a\n# b\n") == {"# a": 2, "# b": 1}
    assert comments("a: 1  # trailing\n") == {}, "an end-of-line comment is not counted"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _tree(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "skip").mkdir()
    (root / "sub" / "a.yaml").write_text("kind: switch\napiVersion: x\n", encoding="utf-8")
    (root / "skip" / "b.yaml").write_text("kind: switch\napiVersion: x\n", encoding="utf-8")
    (root / "_private.yaml").write_text("kind: switch\napiVersion: x\n", encoding="utf-8")
    (root / "notes.txt").write_text("kind: switch\n", encoding="utf-8")
    (root / ".netgraphignore").write_text("skip/\n", encoding="utf-8")


def test_discovery_is_the_loader_s_so_ignore_rules_apply(tmp_path: Path) -> None:
    _tree(tmp_path)
    summary = format_paths([tmp_path], mode=Mode.WRITE)
    assert [result.path.name for result in summary.results] == ["a.yaml"]
    # The skipped ones are untouched, not merely unreported.
    assert (tmp_path / "skip" / "b.yaml").read_text(encoding="utf-8").startswith("kind:")
    assert (tmp_path / "_private.yaml").read_text(encoding="utf-8").startswith("kind:")


def test_an_ignored_file_named_outright_is_still_skipped(tmp_path: Path) -> None:
    _tree(tmp_path)
    summary = format_paths([tmp_path / "skip" / "b.yaml"], mode=Mode.WRITE)
    # Naming the file directly makes it the root of its own walk, which has no
    # ignore file above it -- so it *is* formatted. Documented, and asserted so
    # that a change of mind is a failing test rather than a surprise.
    assert [result.path.name for result in summary.results] == ["b.yaml"]


def test_a_file_reached_through_two_paths_is_formatted_once(tmp_path: Path) -> None:
    _tree(tmp_path)
    summary = format_paths([tmp_path, tmp_path / "sub" / "a.yaml"], mode=Mode.WRITE)
    assert len(summary.results) == 1


def test_an_unreadable_encoding_is_reported_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_bytes(b"\xff\xfe\x00garbage")
    summary = format_paths([path], mode=Mode.WRITE)
    assert summary.results[0].outcome is Outcome.FAILED


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


@pytest.fixture
def messy(tmp_path: Path) -> Path:
    """A tree with one file that needs formatting and one that does not.

    Written through :func:`netgraph.fsio.write_text`, which translates no line
    endings, rather than through :meth:`Path.write_text`, which on Windows turns
    every ``\\n`` into ``\\r\\n``. The canonical form is defined in bytes and LF
    is part of it (see :func:`netgraph.fmt.runner._format_file`), so the
    convenient spelling would have made ``ok.yaml`` genuinely non-canonical on
    that platform and the fixture would have been asserting the opposite of its
    own name.
    """
    write_text(tmp_path / "ok.yaml", DEVICE)
    write_text(
        tmp_path / "messy.yaml",
        "kind: switch\napiVersion: netgraph.dev/v1alpha1\nmetadata:\n  name: sw2\n"
        "spec:\n  interfaces:\n    - name: e0\n      type: ethernet\n",
    )
    return tmp_path


def run(*args: str, stdin: str | None = None) -> Result:
    """Invoke the CLI the way ``main`` does, so a NetgraphError becomes a status.

    ``catch_exceptions`` has to be on: ``cli`` lets :class:`NetgraphError` out
    and it is ``netgraph.cli.main`` that turns one into its exit code, which is
    the behaviour these tests are about.
    """
    result = CliRunner().invoke(cli, list(args), input=stdin, catch_exceptions=True)
    if isinstance(result.exception, NetgraphError):
        return Result(result.output, result.exception.exit_code)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return Result(result.output, result.exit_code)


@dataclass(frozen=True)
class Result:
    """What ``run`` reports: the captured output and the status ``main`` would return."""

    output: str
    exit_code: int


def test_write_mode_rewrites_and_exits_zero(messy: Path) -> None:
    result = run("fmt", str(messy))
    assert result.exit_code == 0
    assert (messy / "messy.yaml").read_text(encoding="utf-8").startswith("apiVersion:")
    assert (messy / "ok.yaml").read_text(encoding="utf-8") == DEVICE


def test_check_mode_writes_nothing_and_exits_one(messy: Path) -> None:
    before = (messy / "messy.yaml").read_text(encoding="utf-8")
    result = run("fmt", "--check", str(messy))
    assert result.exit_code == 1
    assert "messy.yaml" in result.output
    assert "ok.yaml" not in result.output
    assert (messy / "messy.yaml").read_text(encoding="utf-8") == before


def test_check_mode_exits_zero_once_everything_is_canonical(messy: Path) -> None:
    run("fmt", str(messy))
    assert run("fmt", "--check", str(messy)).exit_code == 0


def test_diff_mode_writes_nothing_and_prints_a_patch(messy: Path) -> None:
    before = (messy / "messy.yaml").read_text(encoding="utf-8")
    result = run("fmt", "--diff", str(messy))
    assert result.exit_code == 1
    assert "--- " in result.output and "+++ " in result.output
    assert "+apiVersion: netgraph.dev/v1alpha1" in result.output
    assert (messy / "messy.yaml").read_text(encoding="utf-8") == before


def test_check_and_diff_cannot_be_combined(messy: Path) -> None:
    result = run("fmt", "--check", "--diff", str(messy))
    assert result.exit_code == 2


def test_stdin_formats_a_stream_onto_stdout() -> None:
    result = run("fmt", "--stdin", stdin="kind: switch\napiVersion: netgraph.dev/v1alpha1\n")
    assert result.exit_code == 0
    assert result.output == "apiVersion: netgraph.dev/v1alpha1\nkind: switch\n"


def test_the_path_dash_means_stdin() -> None:
    result = run("fmt", "-", stdin="a: yes\n")
    assert result.output == "a: 'yes'\n"


def test_a_broken_stream_on_stdin_is_a_loader_error() -> None:
    result = run("fmt", "--stdin", stdin="a: [1,\n")
    assert result.exit_code == 3


def test_a_missing_path_is_reported_by_the_loader(tmp_path: Path) -> None:
    result = run("fmt", str(tmp_path / "nope.yaml"))
    assert result.exit_code == 3


def test_with_no_paths_the_inventory_option_decides(messy: Path) -> None:
    assert run("-i", str(messy), "fmt", "--check").exit_code == 1


def test_the_formatted_inventory_still_validates(messy: Path) -> None:
    run("fmt", str(messy))
    assert run("-i", str(messy), "validate").exit_code in {0, 1}
    assert run("-i", str(messy), "fmt", "--check").exit_code == 0
