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
syntax error differs between them — but everything netviz depends on is
identical, and ``tests/test_yaml_loader.py`` pins that down: resolved tags,
constructed values, refused tags, duplicate-key rejection, merge-key handling
and, above all, the ``start_mark`` line/column of every node, which is what
every diagnostic this tool prints is built from.

Set ``NETVIZ_YAML_LOADER=python`` to force the pure-Python parser (CI does, so
the fallback is exercised rather than assumed) or ``=libyaml`` to require the
fast one and fail loudly if PyYAML was built without it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol, cast

import yaml
from yaml.constructor import ConstructorError, SafeConstructor

from netviz.errors import LoaderError, clip_text, echo_value

__all__ = [
    "BOOL_TAG",
    "HAVE_LIBYAML",
    "LOADER_ENV_VAR",
    "LOADER_MODES",
    "MAX_NESTING_DEPTH",
    "MERGE_TAG",
    "STR_TAG",
    "NodeLoader",
    "PureStrictSafeLoader",
    "RawDocument",
    "StrictSafeLoader",
    "YamlSyntaxError",
    "decode_text",
    "libyaml_loader",
    "parse_documents",
    "read_documents",
    "scan_tokens",
    "select_loader",
]

BOOL_TAG: Final = "tag:yaml.org,2002:bool"
MERGE_TAG: Final = "tag:yaml.org,2002:merge"
STR_TAG: Final = "tag:yaml.org,2002:str"

#: Environment variable selecting the parser; see the module docstring.
LOADER_ENV_VAR: Final = "NETVIZ_YAML_LOADER"

#: The accepted values of :data:`LOADER_ENV_VAR`.
LOADER_MODES: Final = ("auto", "python", "libyaml")

#: §5: only the YAML 1.2 spellings are booleans; ``yes``/``no``/``on``/``off``
#: stay strings and are therefore rejected by the strict boolean model type.
_BOOL_RE: Final = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")

#: The UTF-16 surrogate range, which is not encodable as UTF-8. See
#: :meth:`_StrictLoaderMixin.construct_yaml_str`.
_SURROGATE_RE: Final = re.compile("[\ud800-\udfff]")


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
        """Wrap a whole YAML file, which netviz has already decoded to text."""

    def check_node(self) -> bool:
        """Is another document available?"""

    def get_node(self) -> yaml.Node | None:
        """Compose the next document into a node tree."""

    def construct_document(self, node: yaml.Node | None) -> Any:
        """Build the Python object for a composed node tree."""

    def dispose(self) -> None:
        """Release the parser's state."""


class _StrictLoaderMixin(SafeConstructor):
    """The strictness netviz adds on top of PyYAML's safe loader.

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

    def construct_yaml_str(self, node: yaml.ScalarNode) -> str:
        """Build a string, refusing one that cannot be encoded back to UTF-8.

        A lone surrogate — ``"\\udcff"`` — is a code point UTF-8 has no encoding
        for, so a name or description carrying one is a value every output netviz
        writes would raise on, from a rendered diagram to a JSON export.

        libyaml refuses the escape while scanning; the pure-Python scanner builds
        the character and hands it over. That divergence is the whole reason this
        override exists: which parser is in use depends on the PyYAML wheel, and it
        must not decide whether a document loads. The check is confined to
        double-quoted scalars because that is the only way in — the source bytes
        were decoded as strict UTF-8, which cannot itself produce a surrogate.
        """
        value: str = super().construct_yaml_str(node)  # type: ignore[no-untyped-call]
        if node.style == '"' and _SURROGATE_RE.search(value):
            raise ConstructorError(
                "while constructing a string",
                node.start_mark,
                "found a lone surrogate escape, which is not encodable as UTF-8",
                node.start_mark,
            )
        return value

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


def _register_strict_string_constructor(loader: type[Any]) -> None:
    """Point the ``str`` tag at the mixin's constructor on ``loader``.

    PyYAML dispatches through a class-level table of tag -> *function*, filled in
    once by :class:`~yaml.constructor.SafeConstructor` itself. Overriding
    ``construct_yaml_str`` as a method therefore changes nothing on its own: the
    table still holds the base implementation. This points it at the override.

    Untyped throughout because PyYAML's stubs name the ten concrete loader
    classes and bind the constructor's first parameter to one of them, which is
    the one thing a mixin is not.
    """
    loader.add_constructor(STR_TAG, _StrictLoaderMixin.construct_yaml_str)


class PureStrictSafeLoader(_StrictLoaderMixin, yaml.SafeLoader):
    """The strict loader over PyYAML's pure-Python parser. Always available."""


PureStrictSafeLoader.add_implicit_resolver(BOOL_TAG, _BOOL_RE, list("tTfF"))  # type: ignore[no-untyped-call]
_register_strict_string_constructor(PureStrictSafeLoader)

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
    _register_strict_string_constructor(CStrictSafeLoader)
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

    #: The constructed Python object; ``None`` for an empty document (``NV-L004``).
    data: Any
    #: Absolute path of the file the document came from.
    path: Path
    #: The same file relative to the inventory root, POSIX style.
    relative: PurePosixPath
    #: 0-based position of the document within its file (``NV-L005``).
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

    def mark_for(self, field_path: Sequence[str | int]) -> tuple[int, int] | None:
        """1-based line and column of the value at ``field_path``.

        ``("spec", "interfaces", 0, "mtu")`` walks mappings by key and sequences
        by index. A path that does not exist in the document — a missing
        mandatory key, say — degrades to the deepest node that does.

        The mark is the *value* node's, not the key's, which is what an editor
        or a CI annotation wants to underline: ``mtu: 900`` points at ``900``.
        """
        node = self.node
        if node is None:
            return None
        for part in field_path:
            child = _child_node(node, part)
            if child is None:
                break
            node = child
        return node.start_mark.line + 1, node.start_mark.column + 1

    def line_for(self, field_path: Sequence[str | int]) -> int | None:
        """Line of the value at ``field_path``, or of the closest ancestor found."""
        mark = self.mark_for(field_path)
        return None if mark is None else mark[0]


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
    (``NV-L004``). It is a generator rather than a plain iterator on purpose:
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
    yield from parse_documents(decode_text(path.read_bytes(), path), path=path, relative=relative)


def decode_text(content: bytes, path: Path) -> str:
    """Turn the bytes of an inventory file into the text the parser sees.

    Split out of :func:`read_documents` because a caller that needs the *bytes*
    for something else — :mod:`netviz.loader.cache` hashes them — must not
    decode them a second, subtly different way. Two decisions live here:

    * **UTF-8, tolerating a byte-order mark.** An editor on Windows writes one
      and YAML has no idea what it is, so it would otherwise become part of the
      first key.
    * **Line endings are folded to ``\\n``**, exactly as Python's text mode
      would. Both parsers accept CRLF, but a scalar's own line breaks and every
      column in a diagnostic would otherwise depend on which platform wrote the
      file. The scan is skipped when there is no carriage return at all, which
      is every file on every platform that is not Windows.

    Raises:
        YamlSyntaxError: The bytes are not valid UTF-8.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise YamlSyntaxError(f"file is not valid UTF-8: {exc.reason}", path=path) from exc
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
    _guard_depth(text, path)
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


#: Deepest structure netviz will hand to a YAML parser.
#:
#: Not a limit anyone writing an inventory can reach — the schema is seven
#: levels deep — and not a limit either parser needs help with at ordinary
#: depths. It exists because the two parsers fail *differently* past their own:
#: the pure-Python composer recurses once per level and raises a catchable
#: ``RecursionError`` after a few hundred, while libyaml's composer recurses in
#: C and takes the process down with a segmentation fault at around thirty
#: thousand. Which of the two is in use depends on the PyYAML wheel, so an
#: inventory that is a diagnostic on one machine would be a killed process on
#: another. Refusing here makes them agree.
#:
#: The number is therefore chosen to sit *below* the lower of the two ceilings,
#: not between them: the pure-Python composer spends about two Python frames per
#: level, so with CPython's default limit of 1000 it gives out somewhere past 450
#: — and lower than that when netviz is called from a stack that is already
#: deep, as it is under pytest. At 256 a document exactly at the limit still
#: loads on both parsers with room to spare, which is what makes "refused" and
#: "accepted" mean the same thing everywhere. Anything past it is refused by the
#: guard before either composer recurses at all.
MAX_NESTING_DEPTH: Final = 256

_OPENING_TOKENS: Final = (
    yaml.FlowSequenceStartToken,
    yaml.FlowMappingStartToken,
    yaml.BlockSequenceStartToken,
    yaml.BlockMappingStartToken,
)
_CLOSING_TOKENS: Final = (
    yaml.FlowSequenceEndToken,
    yaml.FlowMappingEndToken,
    yaml.BlockEndToken,
)


def _guard_depth(text: str, path: Path) -> None:
    """Refuse a document nested deeper than :data:`MAX_NESTING_DEPTH`.

    Measured with the *scanner*, which is iterative in both implementations —
    it tracks a flow level as an integer — rather than with the composer, which
    is the recursive half and the one the depth is being kept away from.

    The count of ``[`` and ``{`` is a C-speed string scan and bounds the depth
    from above, so a document with no more openers than the limit cannot exceed
    it and is never scanned. Every example inventory this repository ships has
    fewer than 110 openers *in total*, so in practice the scan runs only for a
    document that is already trying something.

    Raises:
        YamlSyntaxError: The document nests too deeply. A stream the scanner
            itself refuses is left alone: the parse that follows reports the
            syntax error properly, and a guard has no business producing a
            worse diagnostic than the thing it guards.
    """
    if text.count("[") + text.count("{") <= MAX_NESTING_DEPTH:
        return
    depth = 0
    try:
        for token in scan_tokens(text):
            if isinstance(token, _OPENING_TOKENS):
                depth += 1
                if depth > MAX_NESTING_DEPTH:
                    raise YamlSyntaxError(
                        f"the document nests more than {MAX_NESTING_DEPTH} levels deep; "
                        f"no netviz document is, and a YAML parser handed one that deep "
                        f"may not survive it",
                        path=path,
                        line=token.start_mark.line + 1,
                        column=token.start_mark.column + 1,
                    )
            elif isinstance(token, _CLOSING_TOKENS):
                depth -= 1
    except yaml.YAMLError:
        return


def scan_tokens(text: str) -> Iterator[yaml.Token]:
    """Tokenise ``text`` with the selected loader.

    Exposed because two callers need the token stream rather than the documents:
    :func:`_guard_depth` here, and
    :func:`netviz.fmt.scalars.scalar_lines`, which has to know which lines are
    the continuation of a scalar. Both want the *scanner*, which is iterative in
    both implementations and produces identical marks, and both want the fast
    one where PyYAML has it.

    The cast is the price of :class:`NodeLoader` being a Protocol: PyYAML's stubs
    name the ten concrete loader classes, and the whole point of the protocol is
    that netviz's loader is none of them.
    """
    yield from yaml.scan(text, Loader=cast(Any, StrictSafeLoader))


def _open(text: str, path: Path) -> NodeLoader:
    """Instantiate the selected loader with YAML errors translated."""
    try:
        return StrictSafeLoader(text)
    except yaml.YAMLError as exc:
        raise _syntax_error(exc, path) from exc
    except _FOREIGN as exc:
        raise _foreign_error(exc, path) from exc


def _check_node(loader: NodeLoader, path: Path) -> bool:
    """``Composer.check_node`` with YAML errors translated."""
    try:
        return bool(loader.check_node())
    except yaml.YAMLError as exc:
        raise _syntax_error(exc, path) from exc
    except _FOREIGN as exc:
        raise _foreign_error(exc, path) from exc


def _get_node(loader: NodeLoader, path: Path) -> yaml.Node | None:
    """``Composer.get_node`` with YAML errors translated."""
    try:
        return loader.get_node()
    except yaml.YAMLError as exc:
        raise _syntax_error(exc, path) from exc
    except _FOREIGN as exc:
        raise _foreign_error(exc, path) from exc


def _construct(loader: NodeLoader, node: yaml.Node | None, path: Path) -> Any:
    """Build the Python object for ``node`` with YAML errors translated."""
    try:
        return loader.construct_document(node)
    except yaml.YAMLError as exc:
        raise _syntax_error(exc, path) from exc
    except _FOREIGN as exc:
        raise _foreign_error(exc, path, node) from exc


#: What a YAML implementation raises that is *not* a :class:`yaml.YAMLError`.
#:
#: Two of these are reachable from an ordinary hostile document and neither is a
#: bug in netviz:
#:
#: * ``ValueError`` — PyYAML's constructors call :func:`int` and :func:`float` on
#:   whatever the resolver matched, and CPython refuses to convert an integer
#:   literal of more than 4300 digits (:pep:`0` / CVE-2020-10735). Eight
#:   kilobytes of digits in a ``mtu:`` is therefore a ``ValueError`` raised from
#:   inside the parser.
#: * ``RecursionError`` — the pure-Python composer recurses once per level of
#:   nesting, so a few thousand ``[`` exhausts the interpreter stack. libyaml
#:   does not, which is exactly why this has to be handled rather than assumed
#:   away: which parser is in use depends on the PyYAML wheel.
#:
#: Both are translated into the same :class:`YamlSyntaxError` every other
#: malformed document produces, because a caller of :func:`load_tree` is
#: promised a diagnostic and not a traceback.
_FOREIGN: Final = (ValueError, RecursionError)


def _foreign_error(exc: Exception, path: Path, node: yaml.Node | None = None) -> YamlSyntaxError:
    """Turn a non-YAML exception out of the parser into a syntax error."""
    if isinstance(exc, RecursionError):
        message = (
            "the document nests deeper than this YAML parser can follow; the usual cause is "
            "a runaway run of '[' or '{'"
        )
    else:
        message = (
            "a scalar could not be read as the value its form implies: "
            f"{clip_text(str(exc))}. Quote it if it is meant to be text."
        )
    mark = getattr(node, "start_mark", None)
    return YamlSyntaxError(
        message,
        path=path,
        line=None if mark is None else mark.line + 1,
        column=None if mark is None else mark.column + 1,
    )


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
