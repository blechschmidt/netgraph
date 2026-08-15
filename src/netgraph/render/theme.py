"""Themes: the layer between an element's own style and the built-in palette.

:mod:`netgraph.models.theme` says what a theme *document* may contain. This
module turns one into something a renderer can ask questions of, and answers the
only question that matters: given a thing about to be drawn, which declared
values apply to it and where did each one come from?

The ladder
----------

Four layers, most specific first. The first one that sets a *field* wins that
field; the rest of the block keeps falling through, so a theme that sets only a
fill does not wipe out the shape.

1. **The element's own** ``spec.style``. Somebody wrote it about this one thing.
2. **The theme's rules**, most specific first. Specificity is the number of
   conditions a selector states (:attr:`~netgraph.models.theme.ThemeSelector.clauses`)
   — ``{kind: router, role: core}`` beats ``{kind: router}`` — and equal
   specificity is broken by declaration order, **later wins**, which is the rule
   every stylesheet language settled on and the one a reader guesses right.
3. **The icon theme**, which supplies ``icon`` and nothing else.
4. **The built-in palette** (:mod:`netgraph.render.palette`), which supplies a
   fill, a stroke and a shape for every kind, so the ladder always terminates.

Every resolved value carries which rung it came from, because the editor's style
inspector has to say "this navy is the theme's, not yours" for its "reset to
theme" action to mean anything.

Where a theme comes from
------------------------

``--theme NAME`` picks one of :data:`BUNDLED_THEMES`; ``--theme PATH`` reads a
file; ``--theme none`` turns one off, the same three spellings ``--icons``
takes, for the same reason. A ``netgraph.toml`` may set the default under
``[render] theme``, and may declare its own rules inline in a ``[theme]`` table
— those are appended after the named theme's, so an inventory's own file has the
last word on a tie without having to restate anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final

from netgraph.errors import RenderError, SchemaError
from netgraph.models.style import Style
from netgraph.models.theme import (
    LABEL_PRESENT,
    THEME_KIND,
    ThemeRule,
    ThemeSelector,
)
from netgraph.models.theme import (
    Theme as ThemeDocument,
)

__all__ = [
    "BUNDLED_THEMES",
    "NO_THEME",
    "THEME_KIND",
    "THEME_SUFFIXES",
    "StyleTarget",
    "Theme",
    "load_theme",
    "resolve_theme",
    "theme_choices",
]

#: What ``--theme`` is given to turn a theme back off, e.g. to override one that
#: came from ``netgraph.toml``. Spelled out so "no theme" is sayable, exactly as
#: ``--icons none`` is.
NO_THEME: Final = "none"

#: Extensions a theme file may have. A theme is YAML, like everything else a
#: user writes for netgraph.
THEME_SUFFIXES: Final[tuple[str, ...]] = (".yaml", ".yml")

#: Directory holding the themes that ship with netgraph, one file each.
_BUNDLED_DIR: Final = Path(__file__).resolve().parent / "themes"


@dataclass(frozen=True, slots=True)
class StyleTarget:
    """The facts a theme selector asks about one drawn thing.

    Flattened out of :class:`~netgraph.render.graph.Node` and
    :class:`~netgraph.render.graph.Edge` so that selector matching has one shape
    to work with, and so a caller with neither — a test, the editor's preview of
    an element that is not drawn yet — can still ask what a theme would do.
    """

    #: ``router``, ``cable``, ``subnet`` … whatever the diagram calls this thing.
    kind: str
    #: ``metadata.name``, i.e. the fully-qualified name without its namespace.
    name: str = ""
    #: The directory the declaring document was found in; ``""`` at the root.
    namespace: str = ""
    #: ``metadata.labels`` of the declaring document, empty for a derived node.
    labels: Mapping[str, str] = field(default_factory=dict)
    #: The element's own ``spec.style``, when it declares one.
    style: Style | None = None

    @property
    def role(self) -> str | None:
        """The ``role`` label, which selectors may key off directly."""
        return self.labels.get("role")


def _glob(value: str, patterns: Sequence[str]) -> bool:
    """Does ``value`` match any of ``patterns``?

    ``**`` crosses a ``/`` and ``*`` does not, which is what makes
    ``namespace: sites/*`` mean one level and ``sites/**`` mean the tree. A
    namespace is a path, and a glob that ignored the separator would make the
    narrower of the two unsayable.
    """
    for pattern in patterns:
        if "**" in pattern:
            if fnmatchcase(value, pattern.replace("**", "*")):
                return True
        elif fnmatchcase(value, pattern) and value.count("/") == pattern.count("/"):
            return True
    return False


def _matches(selector: ThemeSelector, target: StyleTarget) -> bool:
    """Does every clause the selector states hold for ``target``?"""
    if selector.kind and target.kind not in selector.kind:
        return False
    if selector.name and not _glob(target.name, selector.name):
        return False
    if selector.namespace and not _glob(target.namespace, selector.namespace):
        return False
    if selector.role and target.role not in selector.role:
        return False
    for key, wanted in selector.label.items():
        value = target.labels.get(key)
        if value is None or (wanted != LABEL_PRESENT and value != wanted):
            return False
    return True


@dataclass(frozen=True, slots=True)
class Theme:
    """A named, ordered set of styling rules, ready to be asked about elements."""

    #: What ``--theme`` was given, for diagnostics and for the provenance the
    #: editor's inspector shows.
    name: str
    #: The rules in declaration order. Precedence is computed from this, not
    #: stored in it, so two themes concatenate by concatenating their rules.
    rules: tuple[ThemeRule, ...] = ()
    #: One clause for help text.
    description: str = ""

    @classmethod
    def from_document(cls, document: ThemeDocument, *, name: str | None = None) -> Theme:
        return cls(
            name=name or document.name,
            rules=tuple(document.spec.rules),
            description=document.description,
        )

    def extend(self, other: Theme | None) -> Theme:
        """This theme's rules, then ``other``'s.

        Concatenation *is* the composition: precedence reads the list back to
        front on a tie, so the appended rules win one without needing to be
        marked. That is what lets a ``netgraph.toml``'s inline ``[theme]`` table
        adjust a bundled theme rather than replace it.
        """
        if other is None or not other.rules:
            return self
        joined = f"{self.name}+{other.name}" if self.name else other.name
        return Theme(name=joined, rules=(*self.rules, *other.rules), description=self.description)

    @property
    def is_empty(self) -> bool:
        return not self.rules

    def matching(self, target: StyleTarget) -> tuple[tuple[int, ThemeRule], ...]:
        """The rules that apply to ``target``, most specific first.

        Each entry is ``(index, rule)``: the index is the rule's position in the
        file, which is what the provenance report names so a user can go and
        look at the line that did it.

        The sort is stable and the key is ``(-clauses, -index)``, which spells
        the documented precedence exactly: more conditions first, and among
        equals the later declaration.
        """
        hits = [
            (index, rule) for index, rule in enumerate(self.rules) if _matches(rule.select, target)
        ]
        hits.sort(key=lambda entry: (-entry[1].select.clauses, -entry[0]))
        return tuple(hits)


def theme_choices() -> Iterator[str]:
    """Bundled theme names, for help text, with the off switch last."""
    yield from BUNDLED_THEMES
    yield NO_THEME


def _load_file(path: Path) -> Theme:
    """Read and parse one theme file.

    Every failure names the file, because a theme is passed on a command line
    and a diagnostic that only said "invalid theme" would send the user looking
    in the inventory.
    """
    # Local imports: they pull in the YAML parser, and this module is imported
    # by every render path whether or not a theme is in use.
    from netgraph.loader.documents import YamlSyntaxError, decode_text, parse_documents
    from netgraph.models.document import parse_theme

    try:
        text = decode_text(path.read_bytes(), path)
        # The same strict loader the inventory is read with: no custom tags, no
        # duplicate keys, no implicit booleans. A theme is written by the same
        # hand as the manifests and must not be parsed by looser rules.
        bodies = [
            document.data
            for document in parse_documents(text, path=path, relative=PurePosixPath(path.name))
            if document.data is not None
        ]
    except OSError as exc:
        raise RenderError(f"theme {path} cannot be read: {exc}") from exc
    except YamlSyntaxError as exc:
        raise RenderError(f"theme {path} is not valid YAML: {exc}") from exc

    if not bodies:
        raise RenderError(f"theme {path} is empty; a theme declares 'kind: {THEME_KIND}'")
    if len(bodies) > 1:
        raise RenderError(
            f"theme {path} holds {len(bodies)} documents; a theme file declares exactly one"
        )
    try:
        document = parse_theme(bodies[0], source=str(path))
    except SchemaError as exc:
        raise RenderError(f"theme {path} is not usable: {exc}") from exc
    # Named by what the *document* calls itself rather than by the file it was
    # found in. The name is provenance — the editor's inspector says "rule 4
    # of theme blueprint" — and a file somebody renamed on the way into a
    # repository must not change what a diagram says about itself.
    return Theme.from_document(document)


def _bundled() -> Mapping[str, Theme]:
    """Every theme shipped inside the package, keyed by file stem."""
    if not _BUNDLED_DIR.is_dir():  # pragma: no cover - a broken install
        return MappingProxyType({})
    found = {path.stem: _load_file(path) for path in sorted(_BUNDLED_DIR.glob("*.yaml"))}
    return MappingProxyType(found)


#: Themes reachable by name; anything else ``--theme`` accepts is a file path.
BUNDLED_THEMES: Final[Mapping[str, Theme]] = _bundled()


def load_theme(spec: str | Theme | None) -> Theme | None:
    """Resolve what ``--theme`` was given to a theme, or ``None`` for no theme.

    Accepts a bundled theme name, ``"none"``, a path to a theme file, or an
    already-resolved :class:`Theme` (so a caller may pass its own).

    Raises:
        RenderError: The name is neither a bundled theme nor a readable theme
            file, or the file does not parse.
    """
    if spec is None or isinstance(spec, Theme):
        return spec
    name = spec.strip()
    if not name or name == NO_THEME:
        return None
    bundled = BUNDLED_THEMES.get(name)
    if bundled is not None:
        return bundled
    return _file_theme(name)


def _file_theme(name: str) -> Theme:
    path = Path(name).expanduser()
    if not path.exists():
        raise RenderError(
            f"unknown theme {name!r}: it is neither a built-in theme "
            f"({', '.join(BUNDLED_THEMES) or 'none are bundled'}) nor a file that exists"
        )
    if path.is_dir():
        raise RenderError(
            f"theme {name!r} is a directory; a theme is a single YAML file declaring "
            f"'kind: {THEME_KIND}'. (An *icon* theme is a directory — see --icons.)"
        )
    if path.suffix.lower() not in THEME_SUFFIXES:
        raise RenderError(
            f"theme {name!r} is not a YAML file; expected one of {', '.join(THEME_SUFFIXES)}"
        )
    return _load_file(path)


def resolve_theme(named: str | Theme | None, *, inline: Theme | None = None) -> Theme | None:
    """The theme a rendering uses: the named one, then the inventory's own rules.

    ``inline`` is the ``[theme]`` table of ``netgraph.toml``. It is appended
    rather than merged, so it wins a tie against the named theme without having
    to restate the rules it agrees with.
    """
    base = load_theme(named)
    if base is None:
        return inline
    return base.extend(inline)


def rules_from(entries: Iterable[Any], *, name: str) -> Theme:
    """A theme built from already-parsed rule mappings, e.g. a ``[theme]`` table.

    Routed through :func:`~netgraph.models.document.parse_theme` rather than
    validating the rules directly, so a bad colour in ``netgraph.toml`` is
    reported by the same code, with the same wording, as a bad colour in a
    ``theme.yaml``.

    Raises:
        SchemaError: A rule is not a rule.
    """
    from netgraph.models.document import parse_theme
    from netgraph.models.theme import default_envelope

    document = parse_theme({**default_envelope(name), "spec": {"rules": list(entries)}})
    return Theme.from_document(document, name=name)
