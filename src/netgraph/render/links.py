"""``--link-template``: linking a drawn element back to the YAML that declares it.

A diagram in a wiki is a dead end. The reader who wants to know *why* a link is
drawn the way it is has to go and find the file, and the file is exactly what
netgraph knows: every element carries the document, and usually the line, it was
loaded from. A template turns that into a URL::

    --link-template 'https://git.example.com/net/blob/main/{file}#L{line}'

The template is an operator's string, not an inventory's, so it is trusted to
name a scheme and a host. The *values* substituted into it are not: they come
from a document, so each one is percent-encoded on the way in
(:func:`expand`). ``/`` is deliberately left alone — a file path and a
fully-qualified name are both hierarchical, and encoding their separators would
produce a URL that resolves to nothing.

Placeholders are validated when the template is parsed rather than when it is
expanded. A typo in a flag should be a usage error before an inventory is
loaded, not four hundred broken links in a committed SVG.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from string import Formatter
from typing import Final
from urllib.parse import quote

from netgraph.errors import RenderError

__all__ = ["LINK_FIELDS", "LinkTemplate"]

#: Every placeholder a template may use, with what it expands to. Anything else
#: is a usage error; see :meth:`LinkTemplate.parse`.
LINK_FIELDS: Final[Mapping[str, str]] = {
    "file": "path of the declaring document, relative to the inventory root",
    "line": "1-based line the document starts on",
    "name": "fully-qualified name, e.g. sites/hq/sw-core",
    "namespace": "namespace alone, e.g. sites/hq ('' at the root)",
    "kind": "element kind, e.g. switch",
}

#: Characters left un-encoded in a substituted value. A path separator is
#: structure, not content: encoding it would break the URL it is part of.
_SAFE: Final = "/"


@dataclass(frozen=True, slots=True)
class LinkTemplate:
    """A validated ``--link-template``, ready to expand per element."""

    #: The template as given, with its placeholders still in it.
    template: str
    #: The placeholders it actually uses. An element that cannot supply one of
    #: these gets no link at all — see :meth:`expand`.
    fields: frozenset[str]

    @classmethod
    def parse(cls, template: str) -> LinkTemplate:
        """Validate ``template`` and record which placeholders it uses.

        Only bare named placeholders are accepted. A conversion (``{name!r}``),
        a format spec (``{line:04d}``), an index (``{name[0]}``), an attribute
        (``{name.upper}``) and a positional field (``{}``, ``{0}``) are all
        refused: each one would either quote the value, pad it, or reach into
        an object this interface does not promise, and none of them makes a
        better URL than the plain substitution does.

        Raises:
            RenderError: The template is not a well-formed format string, or
                names something other than :data:`LINK_FIELDS`.
        """
        used: set[str] = set()
        try:
            parsed = list(Formatter().parse(template))
        except ValueError as exc:
            raise RenderError(
                f"--link-template is not a valid format string: {exc}. "
                "Write a literal brace as '{{' or '}}'."
            ) from exc

        for _literal, field, spec, conversion in parsed:
            if field is None:
                continue
            if conversion is not None or spec:
                raise RenderError(
                    f"--link-template placeholder {{{field}}} may not carry a conversion or a "
                    "format spec; write it as a bare placeholder such as '{line}'"
                )
            if not field:
                raise RenderError(
                    "--link-template does not take positional placeholders; "
                    f"name the value instead: {_known()}"
                )
            root = field.replace("[", ".").partition(".")[0]
            if root != field:
                raise RenderError(
                    f"--link-template placeholder {{{field}}} may not index or attribute-access "
                    f"a value; write it as a bare placeholder such as '{{{root}}}'"
                )
            if field not in LINK_FIELDS:
                raise RenderError(f"unknown --link-template placeholder {{{field}}}; {_known()}")
            used.add(field)
        return cls(template=template, fields=frozenset(used))

    def expand(
        self,
        *,
        file: str | None = None,
        line: int | None = None,
        name: str = "",
        namespace: str = "",
        kind: str = "",
    ) -> str | None:
        """The URL for one element, or ``None`` when it cannot be built.

        An element the loader could not place — a derived subnet node, or a
        document whose parser reported no line — is left unlinked rather than
        linked into the void: a URL that 404s is worse than a shape that is not
        clickable, because only one of the two is obviously broken.
        """
        values: dict[str, str] = {}
        for field in self.fields:
            value = _value(field, file=file, line=line, name=name, namespace=namespace, kind=kind)
            if value is None:
                return None
            values[field] = quote(value, safe=_SAFE)
        # Every field was validated at parse time and every one of them is in
        # ``values``, so this cannot raise.
        return self.template.format_map(values)


def _value(
    field: str, *, file: str | None, line: int | None, name: str, namespace: str, kind: str
) -> str | None:
    """One placeholder's value, or ``None`` when the element has none.

    The root namespace is an empty string, which is a legitimate value rather
    than a missing one: ``{namespace}`` in a template simply contributes
    nothing for an element declared at the top of the tree.
    """
    if field == "file":
        return file
    if field == "line":
        return str(line) if line is not None else None
    if field == "name":
        return name or None
    if field == "namespace":
        return namespace
    return kind or None


def _known() -> str:
    """The placeholder list, for an error message."""
    return "known placeholders are " + ", ".join(f"{{{field}}}" for field in LINK_FIELDS)
