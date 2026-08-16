"""``kind: theme`` — a stylesheet for an inventory (§22.3 of ``docs/schema.md``).

An element's own ``spec.style`` (:mod:`netviz.models.style`) says how *that*
element is drawn. A theme says how a whole *class* of them is: every router
navy, everything under ``sites/dc-*`` on a slate background, everything labelled
``tier: core`` two points heavier. It is the layer that keeps a consistent
diagram from being forty copies of the same four lines.

Why it is not an inventory document
-----------------------------------

A theme is deliberately *not* one of the kinds the loader walks a tree for. It
describes a rendering, not a network, and the same inventory is legitimately
drawn several ways — an operations diagram, a printable black-and-white one, one
for a slide deck. Keeping it out of the tree is what makes ``--theme`` a switch
rather than an edit, and what stops a theme file dropped in a directory from
silently restyling everybody else's diagrams.

So it is loaded on its own, by :mod:`netviz.render.theme`, from the path or
the bundled name ``--theme`` was given. The envelope is the netviz one
regardless, because a file with ``apiVersion`` and ``kind`` at the top is what a
reader of this project already knows how to look at.

Selectors
---------

A rule names the elements it applies to with up to five clauses, all of which
must hold:

``kind``
    Element kinds: ``router``, ``cable``, ``tunnel``, and the derived node kinds
    a diagram also draws — ``subnet``, ``rack``, and ``namespace`` for the box a
    collapsed namespace becomes. Those are the words the *drawing* uses, which
    is not always the word an export uses for the same thing: a collapsed
    namespace is ``"type": "aggregate"`` in the JSON and ``kind: namespace``
    here, because ``type`` says what sort of node it is and ``kind`` says what
    it is a picture of.
``name`` / ``namespace``
    Globs, matched against ``metadata.name`` and against the directory the
    document was found in. ``namespace: sites/*`` catches one level;
    ``sites/**`` catches the tree, because a namespace is a path and ``*`` in a
    glob does not cross a separator here any more than it does in a shell.
``role``
    Shorthand for the ``role`` label, which is the one label every inventory
    seems to grow. ``role: [core, distribution]`` and ``label: {role: core}``
    mean the same thing; the first exists because it is what people write.
``label``
    Every entry must match. A value of ``"*"`` means "has this label, whatever
    it says", which is how a rule keys off a label's presence.

An omitted clause is not a wildcard *rule* — it is an absent condition. A rule
with no clauses at all matches everything, and is how a theme states its
background defaults.

Precedence is decided by :mod:`netviz.render.theme`, not here: this module's
job is to say what a theme document may contain, and to refuse everything else.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator, Field, model_validator

from netviz.errors import echo_value
from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.element import THEME_KIND
from netviz.models.metadata import Metadata
from netviz.models.scalars import API_VERSION, ApiVersion
from netviz.models.style import Style

__all__ = [
    "LABEL_PRESENT",
    "MAX_THEME_RULES",
    "THEME_KIND",
    "THEME_RULE",
    "SelectorClause",
    "Theme",
    "ThemeRule",
    "ThemeSelector",
    "ThemeSpec",
]

#: The rule every problem in a theme document is reported under.
THEME_RULE: Final = "NV-Z004"

#: What a ``label`` value is set to in order to match the *presence* of the
#: label rather than a particular value.
LABEL_PRESENT: Final = "*"

#: Most rules one theme may hold. Every rule is tested against every drawn
#: element, so a theme is O(rules x elements); a thousand is far past the point
#: where a human could predict what the file does.
MAX_THEME_RULES: Final = 1_000


def _clause(value: Any) -> Any:
    """Accept ``kind: router`` as well as ``kind: [router, switch]``.

    Every clause is a set of alternatives, and insisting on the list for the
    common single-value case would be ceremony — the same shorthand
    ``netviz.toml`` accepts for its repeatable options.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(value)
    raise field_error(
        f"{echo_value(value)} is not a selector clause; write a string or a list of them",
        rule=THEME_RULE,
    )


#: One selector clause: a string, or a list of alternatives any of which match.
SelectorClause = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=253)], ...],
    BeforeValidator(_clause),
]


class ThemeSelector(NetvizModel):
    """Which elements one theme rule applies to (§22.3).

    Every declared clause must hold. An empty selector matches everything.
    """

    #: Element or node kinds. Any one of them matches.
    kind: SelectorClause = ()
    #: Globs on ``metadata.name``. Any one of them matches.
    name: SelectorClause = ()
    #: Globs on the namespace the document was found in. ``**`` crosses ``/``.
    namespace: SelectorClause = ()
    #: Values of the ``role`` label. Shorthand for ``label: {role: ...}``.
    role: SelectorClause = ()
    #: Labels that must all be present, with the value each must have.
    #: :data:`LABEL_PRESENT` matches any value.
    label: dict[str, str] = Field(default_factory=dict)

    @property
    def clauses(self) -> int:
        """How many conditions this selector states; its specificity (§22.4)."""
        return (
            bool(self.kind)
            + bool(self.name)
            + bool(self.namespace)
            + bool(self.role)
            + len(self.label)
        )


class ThemeRule(NetvizModel):
    """One ``select`` / ``style`` pair."""

    #: Which elements this rule is about. Absent matches everything, which is
    #: how a theme declares the background it starts from.
    select: ThemeSelector = Field(default_factory=ThemeSelector)
    #: What to draw them as. The same block an element may carry itself.
    style: Style


class ThemeSpec(NetvizModel):
    """``spec`` of a ``theme`` document: the rules, in declaration order."""

    rules: list[ThemeRule] = Field(default_factory=list, max_length=MAX_THEME_RULES)

    @model_validator(mode="after")
    def _not_empty(self) -> ThemeSpec:
        """``NV-Z004``: a theme with no rules would style nothing.

        The same reasoning as an empty ``style`` block: it validates, it renders
        identically to no theme at all, and the writer gets no signal that the
        file they passed to ``--theme`` did nothing.
        """
        if not self.rules:
            raise field_error(
                "a theme declares at least one rule under 'spec.rules'",
                rule=THEME_RULE,
                path=("rules",),
            )
        return self


class Theme(NetvizModel):
    """A ``kind: theme`` document."""

    api_version: ApiVersion = Field(alias="apiVersion", serialization_alias="apiVersion")
    kind: Literal["theme"] = "theme"
    metadata: Metadata
    spec: ThemeSpec

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description or ""


def default_envelope(name: str) -> Mapping[str, Any]:
    """The envelope keys a theme file needs, for the scaffolding and the docs."""
    return {"apiVersion": API_VERSION, "kind": THEME_KIND, "metadata": {"name": name}}
