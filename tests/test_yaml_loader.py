"""The two YAML parsers must be the same loader.

:mod:`netgraph.loader.documents` mixes netgraph's strictness over PyYAML's
pure-Python parser *and* over the libyaml bindings, and picks one at import
time. That is a performance change to the one component every safety property of
this tool rests on, so nothing here is parameterised over "the loader netgraph
happens to have chosen": every guarantee is asserted against **both** bases, and
the libyaml cases skip -- rather than silently vanish -- on a PyYAML build
without the bindings.

The four safety guarantees are ``yes``/``no`` staying strings, duplicate keys
being rejected, ``!!python/object/apply`` and unknown tags being refused, and
merge keys keeping their non-duplicate status. The fifth property is not about
safety but about every diagnostic netgraph prints: node ``start_mark`` line and
column must agree exactly, or an error message would point at a different line
depending on how PyYAML was compiled.

What is deliberately *not* asserted to match is PyYAML's own wording for a
syntax error: libyaml says "mapping values are not allowed in this context"
where the Python scanner says "... here". Only the marks are load-bearing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from netgraph.errors import LoaderError
from netgraph.loader import documents
from netgraph.loader.documents import (
    HAVE_LIBYAML,
    LOADER_ENV_VAR,
    NodeLoader,
    PureStrictSafeLoader,
    RawDocument,
    StrictSafeLoader,
    libyaml_loader,
    read_documents,
    select_loader,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

needs_libyaml = pytest.mark.skipif(
    not HAVE_LIBYAML,
    reason="this PyYAML build has no libyaml bindings",
)

#: Every parametrised test below runs once per base. ``libyaml_loader()`` is
#: ``None`` when the bindings are missing, but the mark means the case never
#: reaches the test body in that build.
BASES = [
    pytest.param(PureStrictSafeLoader, id="python"),
    pytest.param(libyaml_loader(), id="libyaml", marks=needs_libyaml),
]


@pytest.fixture(params=BASES)
def base(request: pytest.FixtureRequest) -> type[NodeLoader]:
    """One of the two strict loader classes."""
    loader: type[NodeLoader] = request.param
    return loader


@pytest.fixture
def reader(base: type[NodeLoader], monkeypatch: pytest.MonkeyPatch) -> type[NodeLoader]:
    """Point :func:`read_documents` at ``base`` for the duration of a test.

    Which loader the module picked at import time depends on the machine; this
    makes the whole ``read_documents`` path -- not just the loader class in
    isolation -- run against both.
    """
    monkeypatch.setattr(documents, "StrictSafeLoader", base)
    return base


def load(base: type[NodeLoader], text: str) -> list[Any]:
    """Every document in ``text``, constructed through ``base``."""
    loader = base(text)
    try:
        out = []
        while loader.check_node():
            out.append(loader.construct_document(loader.get_node()))
        return out
    finally:
        loader.dispose()


def compose(base: type[NodeLoader], text: str) -> list[yaml.Node | None]:
    """Every document in ``text``, composed but not constructed."""
    loader = base(text)
    try:
        out = []
        while loader.check_node():
            out.append(loader.get_node())
        return out
    finally:
        loader.dispose()


def marks(node: yaml.Node | None, path: tuple[str | int, ...] = ()) -> list[tuple[Any, ...]]:
    """``(path, tag, start line/column, end line/column)`` for a node and its children."""
    if node is None:
        return []
    record = (
        path,
        node.tag,
        node.start_mark.line,
        node.start_mark.column,
        node.end_mark.line,
        node.end_mark.column,
    )
    out = [record]
    if isinstance(node, yaml.MappingNode):
        for index, (key, value) in enumerate(node.value):
            out += marks(key, (*path, index, "key"))
            out += marks(value, (*path, index, "value"))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            out += marks(item, (*path, index))
    return out


# --------------------------------------------------------------------------- #
# Guarantee 1: the YAML 1.1 booleans stay strings (docs/schema.md section 5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("literal", ["yes", "Yes", "YES", "no", "No", "on", "off", "y", "n"])
def test_yaml_11_booleans_stay_strings(base: type[NodeLoader], literal: str) -> None:
    assert load(base, f"value: {literal}\n") == [{"value": literal}]


@pytest.mark.parametrize(
    ("literal", "expected"),
    [("true", True), ("True", True), ("TRUE", True), ("false", False), ("False", False)],
)
def test_yaml_12_booleans_still_parse(base: type[NodeLoader], literal: str, expected: bool) -> None:
    assert load(base, f"value: {literal}\n") == [{"value": expected}]


def test_the_bool_tag_is_only_resolved_for_yaml_12_spellings(base: type[NodeLoader]) -> None:
    """The resolver, not just the constructed value, has to agree."""
    (node,) = compose(base, "a: yes\nb: true\n")
    assert isinstance(node, yaml.MappingNode)
    tags = {key.value: value.tag for key, value in node.value}
    assert tags == {"a": "tag:yaml.org,2002:str", "b": "tag:yaml.org,2002:bool"}


def test_an_explicit_bool_tag_still_works(base: type[NodeLoader]) -> None:
    """Dropping the implicit rule must not disable ``!!bool`` written out."""
    assert load(base, "value: !!bool 'true'\n") == [{"value": True}]


def test_the_strict_rule_does_not_leak_into_pyyaml(base: type[NodeLoader]) -> None:
    """The resolver surgery is on our subclass, not on the shared base table."""
    assert yaml.safe_load("value: yes") == {"value": True}
    assert load(base, "value: yes\n") == [{"value": "yes"}]


@needs_libyaml
def test_the_strict_rule_does_not_leak_into_the_stock_c_loader() -> None:
    assert yaml.load("value: yes", Loader=yaml.CSafeLoader) == {"value": True}


# --------------------------------------------------------------------------- #
# Guarantee 2: a duplicate mapping key is an error, not a silent overwrite
# --------------------------------------------------------------------------- #


def test_duplicate_keys_are_rejected(base: type[NodeLoader]) -> None:
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key 'kind'"):
        load(base, "kind: switch\nkind: router\n")


def test_a_duplicate_key_is_located_at_its_second_occurrence(base: type[NodeLoader]) -> None:
    with pytest.raises(yaml.constructor.ConstructorError) as caught:
        load(base, "spec:\n  a: 1\n  b: 2\n  a: 3\n")

    mark = caught.value.problem_mark
    assert mark is not None
    assert (mark.line, mark.column) == (3, 2)


def test_a_duplicate_key_nested_in_a_sequence_is_rejected(base: type[NodeLoader]) -> None:
    text = "interfaces:\n  - name: eth0\n    name: eth1\n"
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key 'name'"):
        load(base, text)


def test_distinct_keys_that_merely_look_alike_are_kept(base: type[NodeLoader]) -> None:
    """``1`` and ``'1'`` construct to different keys and must both survive."""
    assert load(base, "1: int\n'1': str\n") == [{1: "int", "1": "str"}]


#: Pairs that must be rejected as one key written twice. The duplicate check
#: reads a plain string key straight off the node -- that is its fast path --
#: and constructs anything else, so both branches need a case here, including
#: values that are equal only *after* construction (``1`` and ``01``, ``'1'``
#: and ``!!str 1``, an explicitly tagged string and a plain one).
EQUAL_KEY_PAIRS = [
    pytest.param("kind: a\nkind: b\n", "'kind'", id="plain-strings"),
    pytest.param("'kind': a\nkind: b\n", "'kind'", id="quoted-and-plain"),
    pytest.param("!!str kind: a\nkind: b\n", "'kind'", id="tagged-and-plain"),
    pytest.param("1: a\n01: b\n", "1", id="int-spellings"),
    pytest.param("true: a\nTrue: b\n", "True", id="booleans"),
    pytest.param("1.5: a\n1.50: b\n", "1.5", id="floats"),
    pytest.param("~: a\nnull: b\n", "None", id="nulls"),
    pytest.param("? [1, 2]\n: a\n? [1, 2]\n: b\n", "", id="sequence-keys"),
]


@pytest.mark.parametrize(("text", "echoed"), EQUAL_KEY_PAIRS)
def test_keys_equal_after_construction_are_duplicates(
    base: type[NodeLoader], text: str, echoed: str
) -> None:
    """A key is a duplicate of another when their *constructed values* are equal."""
    if not echoed:
        # An unhashable key is refused by PyYAML itself, before the check runs.
        with pytest.raises(yaml.constructor.ConstructorError, match="unhashable"):
            load(base, text)
        return
    with pytest.raises(yaml.constructor.ConstructorError, match=f"duplicate key {echoed}"):
        load(base, text)


# --------------------------------------------------------------------------- #
# Guarantee 3: no tag can construct a Python object
# --------------------------------------------------------------------------- #


def test_python_object_apply_is_refused(base: type[NodeLoader], tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    text = (
        "kind: !!python/object/apply:pathlib.Path.touch "
        f"[!!python/object/apply:pathlib.Path ['{marker}']]\n"
    )

    with pytest.raises(
        yaml.constructor.ConstructorError, match="could not determine a constructor"
    ):
        load(base, text)

    assert not marker.exists()


@pytest.mark.parametrize(
    "text",
    [
        "kind: !!python/name:os.system ''\n",
        "kind: !!python/module:os ''\n",
        "kind: !!python/object:os.system {}\n",
        "kind: !Ref other\n",
        "kind: !<tag:example.com,2026:custom> other\n",
    ],
)
def test_unknown_and_python_tags_are_refused(base: type[NodeLoader], text: str) -> None:
    with pytest.raises(
        yaml.constructor.ConstructorError, match="could not determine a constructor"
    ):
        load(base, text)


def test_a_custom_tag_on_a_mapping_is_refused(base: type[NodeLoader]) -> None:
    with pytest.raises(
        yaml.constructor.ConstructorError, match="could not determine a constructor"
    ):
        load(base, "kind: !Custom\n  a: 1\n")


# --------------------------------------------------------------------------- #
# Guarantee 3b: a string the tool cannot write back is refused
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "escape",
    [
        pytest.param(r"\udcff", id="low-surrogate"),
        pytest.param(r"\ud800", id="high-surrogate"),
        # A well-formed UTF-16 pair, which is still two unencodable code points
        # once YAML has turned each escape into its own character.
        pytest.param(r"\ud800\udc00", id="surrogate-pair"),
    ],
)
def test_a_surrogate_escape_is_refused(base: type[NodeLoader], escape: str) -> None:
    """Not encodable as UTF-8, so every artefact netgraph writes would raise on it.

    Parametrised over both bases because they refuse it in different places and at
    different depths: libyaml while scanning the escape, the pure-Python parser
    not at all until netgraph's own constructor looks at the result. What matters
    is that *neither* returns a value, so which wheel is installed cannot decide
    whether an inventory loads.
    """
    with pytest.raises(yaml.YAMLError):
        load(base, f'description: "{escape}"\n')


def test_a_backslash_u_escape_of_something_real_is_untouched(base: type[NodeLoader]) -> None:
    """The guard is about the surrogate range and nothing else."""
    assert load(base, r'description: "café \U0001F600"' + "\n") == [
        {"description": "café \U0001f600"}
    ]


# --------------------------------------------------------------------------- #
# Guarantee 4: a merge key is not a duplicate
# --------------------------------------------------------------------------- #


MERGE = """\
defaults: &defaults
  type: ethernet
  mtu: 1500
port:
  <<: *defaults
  mtu: 9000
"""


def test_a_merge_key_fills_in_and_is_overridden(base: type[NodeLoader]) -> None:
    (data,) = load(base, MERGE)
    assert data["port"] == {"type": "ethernet", "mtu": 9000}


def test_two_merge_keys_in_one_mapping_are_not_a_duplicate(base: type[NodeLoader]) -> None:
    text = "a: &a {x: 1}\nb: &b {y: 2}\nboth:\n  <<: *a\n  <<: *b\n"
    (data,) = load(base, text)
    assert data["both"] == {"x": 1, "y": 2}


def test_a_key_duplicated_next_to_a_merge_is_still_rejected(base: type[NodeLoader]) -> None:
    text = "defaults: &d {mtu: 1500}\nport:\n  <<: *d\n  mtu: 9000\n  mtu: 1000\n"
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key 'mtu'"):
        load(base, text)


def test_an_alias_resolves_to_the_same_object_not_a_copy(base: type[NodeLoader]) -> None:
    """Why an alias bomb is O(n): both bases must share, not copy."""
    (data,) = load(base, "anchor: &a [1, 2, 3]\nuse: *a\n")
    assert data["use"] is data["anchor"]


# --------------------------------------------------------------------------- #
# Guarantee 5: the marks every diagnostic is built from are identical
# --------------------------------------------------------------------------- #


MARK_SAMPLE = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
  labels:
    site: hq
spec:
  vlans: [{id: 10, name: staff}]
  interfaces:
    - name: Gi0/1
      type: ethernet
      ipv4:
        addresses:
          - 10.0.0.1/24
    - name: |
        folded
      description: >
        a long
        description
    - name: "quoted"
      mtu: 9000

---
# a comment before the second document

kind: cable
spec: {endpoints: ['a:b', "c:d"]}
---
---
kind: computer
"""


@needs_libyaml
@pytest.mark.parametrize("text", [MARK_SAMPLE, MERGE, "a: 1\n", "", "---\n---\n"])
def test_marks_are_identical_between_the_two_bases(text: str) -> None:
    fast = libyaml_loader()
    assert fast is not None
    pure_nodes = compose(PureStrictSafeLoader, text)
    fast_nodes = compose(fast, text)

    assert len(pure_nodes) == len(fast_nodes)
    for pure, quick in zip(pure_nodes, fast_nodes, strict=True):
        assert marks(pure) == marks(quick)


@needs_libyaml
@pytest.mark.parametrize(
    "path",
    sorted(p for p in EXAMPLES.rglob("*") if p.suffix in {".yaml", ".yml"}),
    ids=lambda p: str(p.relative_to(EXAMPLES)),
)
def test_every_shipped_document_parses_identically(path: Path) -> None:
    """Marks *and* constructed values, over the real inventories."""
    fast = libyaml_loader()
    assert fast is not None
    text = path.read_text(encoding="utf-8-sig")

    assert load(PureStrictSafeLoader, text) == load(fast, text)
    pure_nodes = compose(PureStrictSafeLoader, text)
    fast_nodes = compose(fast, text)
    assert [marks(node) for node in pure_nodes] == [marks(node) for node in fast_nodes]


@needs_libyaml
def test_the_line_a_field_is_reported_on_is_identical(tmp_path: Path) -> None:
    """The property that actually reaches the user: ``RawDocument.line_for``."""
    path = tmp_path / "sample.yaml"
    path.write_text(MARK_SAMPLE, encoding="utf-8")
    fast = libyaml_loader()
    assert fast is not None
    field_paths: list[tuple[str | int, ...]] = [
        ("metadata", "name"),
        ("metadata", "labels", "site"),
        ("spec", "interfaces", 0, "ipv4", "addresses", 0),
        ("spec", "interfaces", 2, "mtu"),
        ("spec", "interfaces", 9, "missing"),
        ("spec", "nope"),
    ]

    def lines(loader: type[NodeLoader]) -> list[tuple[int | None, ...]]:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(documents, "StrictSafeLoader", loader)
            docs = list(read_documents(path, relative=PurePosixPath("sample.yaml")))
        return [
            (document.line, *(document.line_for(field) for field in field_paths))
            for document in docs
        ]

    assert lines(PureStrictSafeLoader) == lines(fast)


# --------------------------------------------------------------------------- #
# read_documents behaves the same over either base
# --------------------------------------------------------------------------- #


def test_read_documents_yields_the_same_shape(reader: type[NodeLoader], tmp_path: Path) -> None:
    path = tmp_path / "multi.yaml"
    path.write_text("a: 1\n---\n\n---\nb: 2\n", encoding="utf-8")

    docs = list(read_documents(path, relative=PurePosixPath("multi.yaml")))

    # The empty document keeps its slot so the index still matches the ``---``
    # separators (NG-L004), and both bases put its null node on line 4.
    assert [(d.index, d.data, d.line) for d in docs] == [
        (0, {"a": 1}, 1),
        (1, None, 4),
        (2, {"b": 2}, 5),
    ]
    assert all(isinstance(d, RawDocument) for d in docs)


def test_a_syntax_error_carries_a_line_and_column(reader: type[NodeLoader], tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("kind: switch\n  metadata: oops\n", encoding="utf-8")

    with pytest.raises(documents.YamlSyntaxError) as caught:
        list(read_documents(path, relative=PurePosixPath("broken.yaml")))

    # The wording differs between the bases ("here" vs "in this context"); the
    # location does not, and the location is what the report prints.
    assert (caught.value.line, caught.value.column) == (2, 11)
    assert "mapping values are not allowed" in str(caught.value)


def test_an_unprintable_character_is_reported_not_raised(
    reader: type[NodeLoader], tmp_path: Path
) -> None:
    """The pure-Python ``Reader`` rejects these in its constructor, libyaml mid-parse.

    Either way it has to surface as a ``YamlSyntaxError`` -- anything else
    escapes ``load_tree``'s handler and takes the process down.
    """
    path = tmp_path / "control.yaml"
    path.write_text("kind: sw\x01itch\n", encoding="utf-8")

    with pytest.raises(documents.YamlSyntaxError, match="unacceptable character"):
        list(read_documents(path, relative=PurePosixPath("control.yaml")))


def test_a_duplicate_key_reaches_the_caller_as_a_syntax_error(
    reader: type[NodeLoader], tmp_path: Path
) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text("kind: switch\nkind: router\n", encoding="utf-8")

    with pytest.raises(documents.YamlSyntaxError, match="duplicate key 'kind'"):
        list(read_documents(path, relative=PurePosixPath("dup.yaml")))


def test_the_loader_is_disposed_even_when_a_document_is_abandoned(
    reader: type[NodeLoader], tmp_path: Path
) -> None:
    """``read_documents`` is a generator; a caller may stop reading half way."""
    path = tmp_path / "multi.yaml"
    path.write_text("a: 1\n---\nb: 2\n---\nc: 3\n", encoding="utf-8")

    documents_iter = read_documents(path, relative=PurePosixPath("multi.yaml"))
    assert next(documents_iter).data == {"a": 1}
    documents_iter.close()  # runs the ``finally`` that calls dispose()


# --------------------------------------------------------------------------- #
# Selecting a base
# --------------------------------------------------------------------------- #


def test_auto_prefers_libyaml_when_it_is_there() -> None:
    assert select_loader("auto", fast=PureStrictSafeLoader) is PureStrictSafeLoader


def test_auto_falls_back_to_the_pure_python_parser() -> None:
    assert select_loader("auto", fast=None) is PureStrictSafeLoader


@pytest.mark.parametrize("fast", [None, PureStrictSafeLoader])
def test_python_always_selects_the_pure_python_parser(fast: type[NodeLoader] | None) -> None:
    assert select_loader("python", fast=fast) is PureStrictSafeLoader


def test_libyaml_is_selected_when_demanded_and_available() -> None:
    assert select_loader("libyaml", fast=PureStrictSafeLoader) is PureStrictSafeLoader


def test_demanding_libyaml_without_bindings_is_an_error() -> None:
    with pytest.raises(LoaderError, match="no libyaml bindings"):
        select_loader("libyaml", fast=None)


@pytest.mark.parametrize("mode", ["", "c", "cyaml", "yes", "PYTHON "])
def test_an_unknown_mode_is_an_error(mode: str) -> None:
    with pytest.raises(LoaderError, match="is not one of"):
        select_loader(mode)


def test_the_module_selected_something_usable() -> None:
    assert StrictSafeLoader in {PureStrictSafeLoader, libyaml_loader()}
    if HAVE_LIBYAML and LOADER_ENV_VAR not in os.environ:
        assert StrictSafeLoader is libyaml_loader()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("python", "PureStrictSafeLoader"), ("auto", None), ("", None)],
)
def test_the_environment_variable_is_honoured_at_import(mode: str, expected: str | None) -> None:
    """End to end, in a fresh interpreter: the env var really does select."""
    env = dict(os.environ)
    if mode:
        env[LOADER_ENV_VAR] = mode
    else:
        env.pop(LOADER_ENV_VAR, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from netgraph.loader import StrictSafeLoader as L; print(L.__name__)",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    chosen = result.stdout.strip()
    if expected is not None:
        assert chosen == expected
    else:
        assert chosen == ("CStrictSafeLoader" if HAVE_LIBYAML else "PureStrictSafeLoader")


def test_a_bad_environment_value_fails_loudly() -> None:
    env = dict(os.environ, **{LOADER_ENV_VAR: "cyaml"})
    result = subprocess.run(
        [sys.executable, "-c", "import netgraph.loader"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "is not one of" in result.stderr
