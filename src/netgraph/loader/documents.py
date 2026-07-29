"""Safe YAML reading with source-location tracking.

Three things set this apart from a bare :func:`yaml.safe_load_all`:

* **Safety.** Only :data:`StrictSafeLoader` is ever used, so no constructor can
  instantiate arbitrary Python objects from an inventory file. On top of the
  stock safe loader it rejects duplicate mapping keys (a silent overwrite would
  make the diagram disagree with the file) and the YAML 1.1 ``yes``/``no``
  booleans (§5, the "Norway problem").
* **Provenance.** Every document keeps the node tree it was built from, so a
  schema error deep inside a document can be reported with the line it occurs
  on (§2.2) instead of just the file name.
* **Speed.** Parsing dominates the runtime of a large inventory, so the libyaml
  bindings are used when PyYAML was built with them and the pure-Python parser
  otherwise. Both are the *same* loader as far as safety goes: the strictness
  lives in :class:`_StrictLoaderMixin` and is mixed over either base.

The two bases are not interchangeable for free — PyYAML's own wording for a
syntax error differs between them — but everything netgraph depends on is
identical, and ``tests/test_yaml_loader.py`` pins that down: resolved tags,
constructed values, refused tags, duplicate-key rejection, merge-key handling
and, above all, the ``start_mark`` line/column of every node, which is what
every diagnostic this tool prints is built from.

Set ``NETGRAPH_YAML_LOADER=python`` to force the pure-Python parser (CI does, so
the fallback is exercised rather than assumed) or ``=libyaml`` to require the
fast one and fail loudly if PyYAML was built without it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

import yaml
from yaml.constructor import ConstructorError, SafeConstructor

from netgraph.errors import LoaderError, echo_value

__all__ = [
    "BOOL_TAG",
    "HAVE_LIBYAML",
    "LOADER_ENV_VAR",
    "LOADER_MODES",
    "MERGE_TAG",
    "STR_TAG",
    "NodeLoader",
    "PureStrictSafeLoader",
    "RawDocument",
    "StrictSafeLoader",
    "YamlSyntaxError",
    "libyaml_loader",
    "parse_documents",
    "read_documents",
    "select_loader",
]

BOOL_TAG: Final = "tag:yaml.org,2002:bool"
MERGE_TAG: Final = "tag:yaml.org,2002:merge"
STR_TAG: Final = "tag:yaml.org,2002:str"

#: Environment variable selecting the parser; see the module docstring.
LOADER_ENV_VAR: Final = "NETGRAPH_YAML_LOADER"

#: The accepted values of :data:`LOADER_ENV_VAR`.
LOADER_MODES: Final = ("auto", "python", "libyaml")

#: §5: only the YAML 1.2 spellings are booleans; ``yes``/``no``/``on``/``off``
#: stay strings and are therefore rejected by the strict boolean model type.
_BOOL_RE: Final = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


class YamlSyntaxError(LoaderError):
    """Raised when a file is not well-formed YAML or uses an unsupported tag."""

    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.path = path
        self.line = line
        self.column = column
        super().__init__(message)


#: ``first character -> [(tag, pattern)]``; PyYAML's stubs type this as ``Any``.
_ImplicitResolvers = dict["str | None", list[tuple[str, "re.Pattern[str]"]]]


def _resolvers_without_bool() -> _ImplicitResolvers:
    """A copy of the safe resolver table with the YAML 1.1 boolean rule removed."""
    stock: _ImplicitResolvers = yaml.SafeLoader.yaml_implicit_resolvers
    return {
        first_char: [entry for entry in entries if entry[0] != BOOL_TAG]
        for first_char, entries in stock.items()
    }


class NodeLoader(Protocol):
    """The slice of PyYAML's loader API :func:`read_documents` drives.

    Both :class:`PureStrictSafeLoader` and the libyaml-backed loader satisfy it,
    but they share no common PyYAML base class — ``CSafeLoader`` derives from
    ``CParser``, not from ``Composer`` — so the contract is structural.

    One ordering rule is not expressible here and matters: :meth:`check_node`
    must be called before every :meth:`get_node`. The pure-Python composer
    tolerates the other order; ``CParser`` raises ``UnboundLocalError`` out of
    its own Cython source, because the stream-start event has not been consumed
    yet. :func:`read_documents` drives them in that order.
    """

    def __init__(self, stream: str) -> None:
        """Wrap a whole YAML file, which netgraph has already decoded to text."""

    def check_node(self) -> bool:
        """Is another document available?"""

    def get_node(self) -> yaml.Node | None:
        """Compose the next document into a node tree."""

    def construct_document(self, node: yaml.Node | None) -> Any:
        """Build the Python object for a composed node tree."""

    def dispose(self) -> None:
        """Release the parser's state."""


class _StrictLoaderMixin(SafeConstructor):
    """The strictness netgraph adds on top of PyYAML's safe loader.

    Mixed over :class:`yaml.SafeLoader` and :class:`yaml.CSafeLoader` alike, so
    the two parsers cannot drift apart in what they accept. It derives from
    :class:`~yaml.constructor.SafeConstructor` — which both bases already carry,
    later in their MRO — purely so ``super()`` and ``self.construct_object``
    resolve for a type checker; it adds no constructor of its own.

    Custom tags (``!!python/object``, ``!Ref``, ...) are already refused by the
    safe constructor; never mix this over :class:`yaml.Loader`.
    """

    # Own the table so that ``add_implicit_resolver`` below cannot leak the
    # strict boolean rule into ``yaml.SafeLoader`` itself.
    # (PyYAML declares this as an instance variable, so it cannot be a ClassVar.)
    yaml_implicit_resolvers: _ImplicitResolvers = _resolvers_without_bool()

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        """Build a mapping, refusing a key that appears twice in the same block."""
        if isinstance(node, yaml.MappingNode):
            self._reject_duplicate_keys(node)
        mapping: dict[Any, Any] = super().construct_mapping(node, deep=deep)
        return mapping

    def _reject_duplicate_keys(self, node: yaml.MappingNode) -> None:
        # Runs before ``flatten_mapping``: keys pulled in by a ``<<`` merge are
        # meant to be overridden by explicit ones and are not duplicates.
        seen: set[Any] = set()
        pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
        for key_node, _ in pairs:
            if key_node.tag == MERGE_TAG:
                continue
            try:
                # Nearly every key in an inventory is a plain string scalar,
                # whose constructed value *is* ``node.value`` -- PyYAML's
                # ``construct_yaml_str`` returns it unchanged. Taking it
                # directly saves a call per key on the hottest loop in the
                # loader; anything else goes through the constructor.
                if type(key_node) is yaml.ScalarNode and key_node.tag == STR_TAG:
                    key = key_node.value
                else:
                    key = self.construct_object(key_node, deep=True)
                if key in seen:
                    raise ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {echo_value(key)}",
                        key_node.start_mark,
                    )
                seen.add(key)
            except TypeError:
                # An unhashable key -- a sequence or a mapping. PyYAML refuses
                # it a moment later with its own diagnostic; there is nothing
                # useful to say about it here.
                continue


class PureStrictSafeLoader(_StrictLoaderMixin, yaml.SafeLoader):
    """The strict loader over PyYAML's pure-Python parser. Always available."""


PureStrictSafeLoader.add_implicit_resolver(BOOL_TAG, _BOOL_RE, list("tTfF"))  # type: ignore[no-untyped-call]

#: Was PyYAML built with the libyaml bindings? Most wheels are; not all.
HAVE_LIBYAML: Final = hasattr(yaml, "CSafeLoader")

_libyaml_loader: type[NodeLoader] | None = None

if HAVE_LIBYAML:

    class CStrictSafeLoader(_StrictLoaderMixin, yaml.CSafeLoader):
        """The same strict loader over the libyaml bindings.

        Worth roughly 7-8x on the parse step of a large inventory; see entry 1 of
        ``docs/follow-ups.md`` for the measurement and ``tools/bench_pipeline.py``
        for the harness that produced it.

        ``CParser`` scans, parses and composes in C but resolves implicit tags
        and constructs objects through the very same Python
        :class:`~yaml.resolver.Resolver` and
        :class:`~yaml.constructor.SafeConstructor` as the pure-Python path, which
        is why the mixin's boolean surgery and duplicate-key check apply here
        unchanged.
        """

    CStrictSafeLoader.add_implicit_resolver(BOOL_TAG, _BOOL_RE, list("tTfF"))  # type: ignore[no-untyped-call]
    _libyaml_loader = CStrictSafeLoader


def libyaml_loader() -> type[NodeLoader] | None:
    """The libyaml-backed strict loader, or ``None`` if PyYAML has no bindings."""
    return _libyaml_loader


def select_loader(
    mode: str, *, fast: type[NodeLoader] | None = _libyaml_loader
) -> type[NodeLoader]:
    """Pick a loader class for ``mode``, one of :data:`LOADER_MODES`.

    ``fast`` is a parameter rather than a module lookup so the selection can be
    tested on a build that has the bindings *and* on one that pretends not to.

    Raises:
        LoaderError: ``mode`` is not a known mode, or libyaml was demanded on a
            build without it. Both are operator mistakes, and quietly doing
            something else would defeat the point of asking.
    """
    if mode not in LOADER_MODES:
        raise LoaderError(
            f"{LOADER_ENV_VAR}={echo_value(mode)} is not one of {', '.join(LOADER_MODES)}",
        )
    if mode == "python":
        return PureStrictSafeLoader
    if fast is not None:
        return fast
    if mode == "libyaml":
        raise LoaderError(
            f"{LOADER_ENV_VAR}=libyaml was requested but this PyYAML build has no libyaml "
            f"bindings; reinstall PyYAML with them, or use {LOADER_ENV_VAR}=auto to fall "
            "back to the pure-Python parser",
        )
    return PureStrictSafeLoader


#: The loader every document in this process is parsed with. Chosen once, at
#: import time: the choice cannot change mid-run, and re-reading the environment
#: per file would make two files in one inventory parseable by different rules.
StrictSafeLoader: Final[type[NodeLoader]] = select_loader(
    os.environ.get(LOADER_ENV_VAR, "auto").strip().lower() or "auto",
)


@dataclass(frozen=True, slots=True)
class RawDocument:
    """One YAML document, with the node tree it was constructed from."""

    #: The constructed Python object; ``None`` for an empty document (``NG-L004``).
    data: Any
    #: Absolute path of the file the document came from.
    path: Path
    #: The same file relative to the inventory root, POSIX style.
    relative: PurePosixPath
    #: 0-based position of the document within its file (``NG-L005``).
    index: int
    #: Root node, kept so field paths can be mapped back onto line numbers.
    node: yaml.Node | None = None

    @property
    def source(self) -> str:
        """Provenance in ``sites/hq/sw.yaml#0`` notation (§2.2)."""
        return f"{self.relative.as_posix()}#{self.index}"

    @property
    def line(self) -> int | None:
        """1-based line the document starts on."""
        return None if self.node is None else self.node.start_mark.line + 1

    def line_for(self, field_path: Sequence[str | int]) -> int | None:
        """Line of the value at ``field_path``, or of the closest ancestor found.

        ``("spec", "interfaces", 0, "mtu")`` walks mappings by key and sequences
        by index. A path that does not exist in the document — a missing
        mandatory key, say — degrades to the deepest node that does.
        """
        node = self.node
        if node is None:
            return None
        for part in field_path:
            child = _child_node(node, part)
            if child is None:
                break
            node = child
        return node.start_mark.line + 1


def _child_node(node: yaml.Node, part: str | int) -> yaml.Node | None:
    """The node addressed by one path component, or ``None``."""
    if isinstance(node, yaml.MappingNode):
        pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
        for key_node, value_node in pairs:
            if isinstance(key_node, yaml.ScalarNode) and key_node.value == str(part):
                return value_node
        return None
    if isinstance(node, yaml.SequenceNode) and isinstance(part, int):
        items: list[yaml.Node] = node.value
        if -len(items) <= part < len(items):
            return items[part]
    return None


def read_documents(path: Path, *, relative: PurePosixPath) -> Generator[RawDocument, None, None]:
    """Parse every document in ``path`` with the strict safe loader.

    Documents are yielded lazily in file order, empty ones included so that the
    document index keeps matching the ``---`` separators; callers skip them
    (``NG-L004``). It is a generator rather than a plain iterator on purpose:
    abandoning it part-way -- ``close()``, or simply letting it fall out of
    scope -- still disposes the parser.

    Args:
        path: Absolute path of the file to read.
        relative: The same file relative to the inventory root.

    Raises:
        YamlSyntaxError: The file is not well-formed YAML, uses an unsupported
            tag, repeats a mapping key, or is not valid UTF-8.
        OSError: The file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise YamlSyntaxError(
            f"file is not valid UTF-8: {exc.reason}",
            path=path,
        ) from exc
    yield from parse_documents(text, path=path, relative=relative)


def parse_documents(
    text: str, *, path: Path, relative: PurePosixPath
) -> Generator[RawDocument, None, None]:
    """Parse every document in ``text``, exactly as :func:`read_documents` does.

    The two differ only in where the bytes came from, which is what lets a
    document stream that was never a file -- pasted into the web interface,
    piped in on stdin -- be loaded under the same rules, with the same
    strictness and the same line numbers.

    Args:
        text: The whole YAML stream, already decoded.
        path: The name the stream is reported under. It is used for
            diagnostics only and is never opened, so a caller with nothing on
            disk may pass a stand-in.
        relative: The same name relative to the inventory root.

    Raises:
        YamlSyntaxError: The stream is not well-formed YAML, uses an
            unsupported tag, or repeats a mapping key.
    """
    # Constructing the loader can itself fail: the pure-Python ``Reader`` scans
    # the whole string for unprintable characters up front, where libyaml only
    # trips over one when it reaches it. Translating here keeps the two paths
    # reporting the same kind of error instead of one of them raising a bare
    # ``ReaderError`` past ``load_tree``.
    loader = _open(text, path)
    try:
        index = 0
        while _check_node(loader, path):
            node = _get_node(loader, path)
            yield RawDocument(
                data=_construct(loader, node, path),
                path=path,
                relative=relative,
                index=index,
                node=node,
            )
            index += 1
    finally:
        loader.dispose()


def _open(text: str, path: Path) -> NodeLoader:
    """Instantiate the selected loader with YAML errors translated."""
    try:
        return StrictSafeLoader(text)
    except yaml.YAMLError as exc:
        raise _syntax_error(exc, path) from exc


def _check_node(loader: NodeLoader, path: Path) -> bool:
    """``Composer.check_node`` with YAML errors translated."""
    try:
        return bool(loader.check_node())
    except yaml.YAMLError as exc:
        raise _syntax_error(exc, path) from exc


def _get_node(loader: NodeLoader, path: Path) -> yaml.Node | None:
    """``Composer.get_node`` with YAML errors translated."""
    try:
        return loader.get_node()
    except yaml.YAMLError as exc:
        raise _syntax_error(exc, path) from exc


def _construct(loader: NodeLoader, node: yaml.Node | None, path: Path) -> Any:
    """Build the Python object for ``node`` with YAML errors translated."""
    try:
        return loader.construct_document(node)
    except yaml.YAMLError as exc:
        raise _syntax_error(exc, path) from exc


def _syntax_error(exc: yaml.YAMLError, path: Path) -> YamlSyntaxError:
    """Turn a PyYAML error into a :class:`YamlSyntaxError` carrying its mark."""
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    context = getattr(exc, "context", None)
    message = f"{context}: {problem}" if context else str(problem)
    return YamlSyntaxError(
        message.strip(),
        path=path,
        line=None if mark is None else mark.line + 1,
        column=None if mark is None else mark.column + 1,
    )
