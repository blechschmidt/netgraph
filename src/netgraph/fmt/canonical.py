"""Turning one YAML stream into its canonical form, comments and all.

This is the only place in netgraph that parses YAML with anything other than
:mod:`netgraph.loader.documents`. It has to be: the strict loader throws away
everything a formatter needs — comments, blank lines, quoting style, whether a
collection was written flow or block — because throwing them away is what makes
it fast. ``ruamel.yaml``'s round-trip parser keeps all of it. The two never
meet: nothing here is imported by ``validate`` or ``render``, and
:func:`format_stream` hands its output back to the *strict* loader to prove the
meaning survived (see :mod:`netgraph.fmt.verify`).

The pipeline is four steps.

1. **Load** the stream round-trip, so every document is a ``CommentedMap`` or
   ``CommentedSeq`` carrying its comments in ``.ca``.
2. **Normalise** it against the schema shape of its ``kind``
   (:mod:`netgraph.fmt.order`): reorder keys, restyle scalars
   (:mod:`netgraph.fmt.scalars`), and demote a flow collection that no longer
   fits inside :data:`WIDTH` to block.
3. **Emit** with one indent style and one document separator.
4. **Tidy** the bytes: no trailing whitespace, exactly one trailing newline.

Reordering keys and keeping comments are in tension, and the tension is
resolved in the comments' favour. ruamel files a comment under the item it
*follows*, so an end-of-line comment moves with its key but one written on its
own line above a key does not — it would stay put and end up describing
whatever landed beneath it. :func:`_interior_comments` detects that case and
:func:`_normalise_map` then leaves the mapping's order alone. Every other rule
still applies to it; only the ordering defers.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Iterator
from typing import Any, Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from netgraph.fmt.order import MappingShape, SequenceShape, Shape, document_shape, order_keys
from netgraph.fmt.scalars import (
    canonical_string,
    is_untouchable,
    plain,
    plain_survives,
    quote_style,
)

__all__ = [
    "INDENT",
    "SEQUENCE_INDENT",
    "SEQUENCE_OFFSET",
    "WIDTH",
    "FormatSyntaxError",
    "format_stream",
    "set_scalar",
]

#: Spaces per mapping level.
INDENT: Final = 2
#: Columns a sequence's *content* is indented from its parent mapping, and the
#: column its ``-`` sits at. Together they put ``- name: eth0`` two spaces in
#: from the key that owns it, which is what ``examples/`` already uses.
SEQUENCE_INDENT: Final = 4
SEQUENCE_OFFSET: Final = 2

#: The line width above which a flow collection is written as a block instead.
#: The same 100 columns ``[tool.ruff]`` gives Python in this repository, so one
#: number governs how wide a line may get whatever it holds.
WIDTH: Final = 100

#: The one document separator. ``---`` between documents, never before the
#: first, and no ``...`` at the end.
_SEPARATOR: Final = "---"

#: Emitting is given a very large width so that ruamel never breaks a line on
#: its own: :func:`_style` has already decided what goes flow and what goes
#: block, and a second, hidden wrapping rule would undo that decision by
#: folding a flow collection across lines instead of demoting it.
_EMIT_WIDTH: Final = 1 << 30


class FormatSyntaxError(Exception):
    """The stream is not well-formed YAML, so there is nothing to format."""


def _yaml() -> YAML:
    """A round-trip YAML configured for the canonical form.

    Built per call rather than shared: a ``YAML`` instance carries parser and
    emitter state, and reusing one across the thousands of documents a property
    test loads is how that state leaks from one file into the next.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=INDENT, sequence=SEQUENCE_INDENT, offset=SEQUENCE_OFFSET)
    yaml.width = _EMIT_WIDTH
    # One separator style: ``---`` between documents, never before the first.
    yaml.explicit_start = False
    yaml.explicit_end = False
    yaml.default_flow_style = False
    # One spelling of nothing. ruamel writes ``None`` as an empty value, so
    # ``key:`` and ``key: null`` come out differently despite meaning the same;
    # the explicit word is the one a reader cannot mistake for an oversight.
    yaml.representer.add_representer(type(None), _represent_null)
    return yaml


def _represent_null(representer: Any, _: None) -> Any:
    """Write ``None`` as ``null`` rather than as nothing at all."""
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


def format_stream(text: str) -> str:
    """Return ``text`` in canonical form.

    Raises:
        FormatSyntaxError: ``text`` is not well-formed YAML.
    """
    yaml = _yaml()
    try:
        documents = list(yaml.load_all(text))
    except YAMLError as exc:
        raise FormatSyntaxError(str(exc)) from exc
    if not documents:
        # A stream with no documents in it — empty, or nothing but comments,
        # which is what ``netgraph init --minimal`` writes. ruamel has nowhere
        # to hang those comments and drops them on the way out, so the emitter
        # is skipped entirely: with no structure there is nothing for it to
        # canonicalise that :func:`_tidy` cannot do on the text.
        return _tidy(text)
    for document in documents:
        _normalise(document, _shape_of(document), indent=0, inline=0)
    buffer = io.StringIO()
    try:
        yaml.dump_all(documents, buffer)
    except YAMLError as exc:  # pragma: no cover - the emitter accepts what it parsed
        raise FormatSyntaxError(str(exc)) from exc
    return _tidy(buffer.getvalue())


def _shape_of(document: Any) -> Shape:
    """The schema shape for a loaded document, chosen by its ``kind``."""
    kind = document.get("kind") if isinstance(document, CommentedMap) else None
    return document_shape(kind)


def _tidy(text: str) -> str:
    """The line-level rules, applied to the emitted stream.

    Four of them, all idempotent by construction:

    * no trailing whitespace on any line. ruamel emits none of its own, but it
      reproduces a comment exactly as it was written — trailing spaces included;
    * no blank line at the start of the stream, at its end, or straight after a
      ``---``, all of which are separators doing a blank line's job twice;
    * at most one blank line in a row, so that grouping stays a signal rather
      than a matter of degree;
    * exactly one trailing newline. An empty stream stays empty rather than
      becoming a lone newline.

    They are done on the text because that is what they are about. Blank lines
    live in ruamel's comment tokens, spread across whichever node happens to
    precede them, and reaching into that to express "no blank line after a
    separator" would be a much longer way of writing four ``if``\\ s.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    kept: list[str] = []
    for line in lines:
        if line:
            kept.append(line)
            continue
        # A blank line, dropped when what precedes it is nothing, another blank
        # line, or a document separator.
        if kept and kept[-1] and kept[-1] != _SEPARATOR:
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return "".join(f"{line}\n" for line in kept)


# ---------------------------------------------------------------------------
# Normalising the loaded tree
# ---------------------------------------------------------------------------


def _normalise(node: Any, shape: Shape, *, indent: int, inline: int) -> None:
    """Rewrite ``node`` in place into canonical form.

    Two columns are threaded through, because a collection has two possible
    positions and the choice between them is what is being decided. ``indent``
    is where the node's content sits when it is written as a block; ``inline``
    is where it would start if it were written flow, on the same line as the key
    or dash that introduces it. Only ``inline`` bears on :data:`WIDTH`; only
    ``indent`` is passed on to children.
    """
    if isinstance(node, CommentedMap):
        _normalise_map(node, shape, indent=indent, inline=inline)
    elif isinstance(node, CommentedSeq):
        _normalise_seq(node, shape, indent=indent, inline=inline)


def set_scalar(container: CommentedMap | CommentedSeq, key: Any, value: str) -> None:
    """Replace a scalar, defeating ruamel's style preservation.

    Assigning a bare :class:`str` over a value that has a quoting style makes
    ruamel silently re-apply that style — a deliberate feature, so that editing
    a document through the round-trip API does not restyle what it touches, and
    precisely wrong for a formatter whose job is to restyle. Writing ``None``
    first removes the old style from the slot, so the second assignment lands as
    written and the emitter decides the quoting (see
    :func:`~netgraph.fmt.scalars.canonical_string`).
    """
    if container[key] is not value:
        container[key] = None
        container[key] = value


def _interior_comments(node: CommentedMap) -> bool:
    """Does ``node`` contain a comment written on a line of its own?

    ruamel files a comment under the item it *follows*, not the one it precedes,
    and stores it in the same token as that item's end-of-line comment. An
    end-of-line comment therefore travels with its key when the key moves; a
    comment on its own line above a key does not — it stays with whatever
    preceded it and ends up describing whichever key lands beneath it.

    Rather than reach into ruamel's token strings to split the two apart and
    re-file them, :func:`_normalise_map` declines to reorder a mapping this
    returns ``True`` for. A comment inside a block is the author saying
    something about the order they chose, and a formatter that answers by
    leaving the comment pointing at the wrong key has made the document worse in
    exchange for a rule nobody asked it to enforce there.

    The search covers the whole subtree, not just ``node``'s own keys, because
    "the line above key K" is filed under whatever textually precedes it — which
    may be the last scalar of a list nested two levels inside the key before it.
    Freezing a little more than strictly necessary is the safe direction: the
    order it declines to impose is one the author already chose.
    """
    for descendant in _subtree(node):
        # ``ca.comment`` has two slots, and only the second is a line of its
        # own: slot 0 holds the end-of-line comment of the key that *opens* the
        # collection (``forwarding:  # why``, filed on the nested map rather
        # than on the key), slot 1 the comment lines above its first item.
        block = descendant.ca.comment
        if block is not None and len(block) > 1 and block[1]:
            return True
        for entry in descendant.ca.items.values():
            for token in entry[2:3]:
                for text in _token_text(token):
                    # The first line is the end-of-line comment of the item
                    # itself; a ``#`` after it starts a line of its own.
                    _, _, rest = text.partition("\n")
                    if "#" in rest:
                        return True
    return False


def _subtree(node: CommentedMap | CommentedSeq) -> Iterator[CommentedMap | CommentedSeq]:
    """``node`` and every collection below it."""
    stack: list[CommentedMap | CommentedSeq] = [node]
    while stack:
        current = stack.pop()
        yield current
        values = current.values() if isinstance(current, CommentedMap) else current
        stack.extend(value for value in values if isinstance(value, CommentedMap | CommentedSeq))


def _token_text(token: Any) -> list[str]:
    """The comment text of one ``ca.items`` slot, which may hold a list."""
    if token is None:
        return []
    if isinstance(token, list):
        return [str(item.value) for item in token if item is not None]
    return [str(token.value)]


def _normalise_map(node: CommentedMap, shape: Shape, *, indent: int, inline: int) -> None:
    order = order_keys(list(node), shape)
    if order != list(node) and _interior_comments(node):
        order = list(node)
    for key in order:
        # ``move_to_end`` reorders the mapping without touching ``node.ca``,
        # which is keyed by key rather than by position — so each key's comment
        # travels with it.
        node.move_to_end(key)
        value = node[key]
        if not isinstance(value, str):
            child = shape.child(key) if isinstance(shape, MappingShape) else shape
            # ``key: value`` — flow would start two columns past the key.
            _descend(value, child, parent=indent, inline=indent + len(str(key)) + 2)
    _style(node, inline=inline)
    _quote_scalars(node, list(node))


def _normalise_seq(node: CommentedSeq, shape: Shape, *, indent: int, inline: int) -> None:
    item_shape = shape.item if isinstance(shape, SequenceShape) else shape
    for value in node:
        if not isinstance(value, str):
            # ``- value`` — an item's content and its flow start are the same
            # column, since the dash sits in the sequence's own indent.
            _descend(value, item_shape, parent=indent, inline=indent, in_sequence=True)
    _style(node, inline=inline)
    _quote_scalars(node, range(len(node)))


def _quote_scalars(node: CommentedMap | CommentedSeq, keys: Iterable[Any]) -> None:
    """Settle the quoting of every scalar directly in ``node``.

    Runs *after* :func:`_style`, which is the whole reason it is a separate
    pass. Whether a plain scalar is legal depends on whether it lands in a flow
    collection or a block one — ``::1/128`` may not be written bare inside
    ``[...]`` — so the container's style has to be final before its contents can
    be quoted. Doing it in the other order would make the formatter disagree
    with itself on a second run: the first pass would quote for flow, the second
    would find the value already in a block and unquote it again.
    """
    in_flow = bool(node.fa.flow_style())
    for key in keys:
        value = node[key]
        if isinstance(value, str):
            set_scalar(node, key, canonical_string(value, in_flow=in_flow))


def _descend(
    value: Any, shape: Shape, *, parent: int, inline: int, in_sequence: bool = False
) -> None:
    """Recurse into ``value``, working out where its block content would sit.

    A mapping under a key steps in by :data:`INDENT`; a mapping that *is* a
    sequence item shares the item's column, because ``- `` already provided the
    step. A sequence steps in by :data:`SEQUENCE_INDENT`, which with
    :data:`SEQUENCE_OFFSET` is what puts ``- name: eth0`` under ``interfaces:``.
    """
    if isinstance(value, CommentedMap):
        indent = parent if in_sequence else parent + INDENT
    elif isinstance(value, CommentedSeq):
        indent = parent + SEQUENCE_INDENT
    else:
        return
    _normalise(value, shape, indent=indent, inline=inline)


def _style(node: CommentedMap | CommentedSeq, *, inline: int) -> None:
    """Settle whether ``node`` is written flow or block.

    Block is never promoted to flow. A hand-written block list is a deliberate
    shape — one interface per stanza, one comment per entry — and collapsing it
    would destroy exactly the grouping ``fmt`` promises to keep. What *is*
    canonicalised is the other direction: a flow collection that has outgrown
    :data:`WIDTH`, or that holds a comment or a nested collection, becomes a
    block, because there is no way to write those flow and stay readable.
    """
    width = _flow_width(node) if node.fa.flow_style() else None
    if width is not None and not _has_comments(node) and inline + width <= WIDTH:
        node.fa.set_flow_style()
    else:
        node.fa.set_block_style()


def _has_comments(node: CommentedMap | CommentedSeq) -> bool:
    """Does ``node`` carry a comment that only block style can hold?"""
    comments = node.ca
    return bool(comments.comment) or bool(comments.items) or bool(comments.end)


def _flow_width(node: CommentedMap | CommentedSeq) -> int | None:
    """Columns ``node`` would occupy written flow, or ``None`` if it cannot be.

    A nested collection returns ``None``: netgraph's schema has no such value,
    and guessing at the emitter's own nesting rules to save a line nobody writes
    would be a lot of arithmetic to get subtly wrong.
    """
    values = list(node.values()) if isinstance(node, CommentedMap) else list(node)
    keys = list(node) if isinstance(node, CommentedMap) else []
    if any(isinstance(value, CommentedMap | CommentedSeq) for value in values):
        return None
    # ``[`` + ``]``, or ``{`` + ``}``.
    width = 2
    for index, value in enumerate(values):
        if index:
            width += 2  # ", "
        if keys:
            width += len(_render(keys[index])) + 2  # "key: "
        width += len(_render(value))
    return width


#: How a non-string scalar is written, for width purposes.
_LITERALS: Final[tuple[tuple[Any, str], ...]] = ((None, "null"), (True, "true"), (False, "false"))


def _render(value: Any) -> str:
    """How wide ``value`` is once written.

    Quoting has not been applied yet when this runs — :func:`_quote_scalars`
    needs the style decision this feeds — so for a string it is predicted, on
    the assumption of flow, which is the case being considered. The prediction
    is the same one :func:`~netgraph.fmt.scalars.canonical_string` will make,
    which is what keeps the width a collection is measured at and the width it
    is written at the same number.
    """
    if isinstance(value, str):
        text = plain(value)
        quoted = not is_untouchable(value) and (
            quote_style(value) is not None or not plain_survives(text, in_flow=True)
        )
        return f"'{text}'" if quoted else text
    for literal, rendered in _LITERALS:
        if value is literal:
            return rendered
    return str(value)
