"""Icon themes: the pictures a renderer draws instead of a plain shape.

A theme is a *directory of image files named after element kinds* —
``router.svg``, ``switch.png``, ``computer.svg`` and so on. That is the whole
format. It is deliberately not a manifest: a directory listing is something a
user can produce with a file manager, and a missing file means "no icon for that
kind", not a broken theme.

One theme ships with netgraph, :data:`CISCO`, drawn in the network-diagram idiom
Cisco made standard — a router is a cylinder with arrows on it, a switch a flat
slab, a subnet a cloud. The art is netgraph's own (MIT, like the rest of the
package); Cisco's own icon library is copyrighted and is not redistributed here.
A user who has that library can point ``--icons`` straight at a directory of it,
provided the files are named for the kinds they stand for.

Which file, for which output
----------------------------

A kind may have several files — ``router.svg`` *and* ``router.png`` — and which
one is picked depends on where the DOT is going, because Graphviz's ability to
read an image depends on the renderer it is feeding:

* Its **SVG** output can reference an SVG icon directly, so netgraph prefers the
  vector file there and gets a crisp icon at any zoom.
* Its **cairo**-based outputs (``png``, ``pdf``) can only read an SVG when
  Graphviz was built against librsvg, which many builds are not. Raster icons
  are read by every build, so those formats prefer ``.png``.
* Plain ``-f dot`` output is a file someone else will run through Graphviz with
  a ``-T`` netgraph cannot see, so it takes the portable choice — raster first.

That is what :func:`suffix_order` encodes; :data:`RASTER_FIRST` is the safe
default and :data:`VECTOR_FIRST` the better one where it is known to work.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from netgraph.errors import RenderError
from netgraph.render.graph import PATCHPANEL_KIND, SUBNET_KIND, TUNNEL_KIND

__all__ = [
    "BUNDLED_THEMES",
    "CISCO",
    "ICON_KINDS",
    "ICON_SUFFIXES",
    "NO_ICONS",
    "RASTER_FIRST",
    "VECTOR_FIRST",
    "IconTheme",
    "icon_theme",
    "suffix_order",
    "theme_choices",
]

#: Every kind a theme may hold a picture for: the six element kinds that become
#: nodes, the derived layer-3 subnet node, and the tunnel. ``cable`` is not here
#: — a cable is an edge, and an edge has no icon. Neither is
#: :data:`~netgraph.render.aggregate.AGGREGATE_KIND`: a collapsed namespace is
#: not a *thing* with a picture, it is a box holding several, and the folder
#: shape it falls back to says so better than any glyph would.
#:
#: The tunnel glyph is a **conduit**, one for every tunnel type (see entry 6 of
#: ``docs/follow-ups.md``): a bore with a payload entering one end and leaving
#: the other. It draws the encapsulation, which is what every tunnel type has in
#: common, and deliberately draws nothing about confidentiality — a lock or an
#: open padlock would put netgraph's guess about a security property into a
#: picture, and a reader who did not recognise the glyph would read its absence
#: as "nothing to say". That stays a colour and a word, on the edge and in
#: ``W127``.
ICON_KINDS: Final[tuple[str, ...]] = (
    "router",
    "switch",
    "hub",
    "computer",
    "server",
    "adapter",
    PATCHPANEL_KIND,
    SUBNET_KIND,
    TUNNEL_KIND,
)

#: Image formats a theme file may be in, and that Graphviz can load. The order
#: is not a preference — see :func:`suffix_order` for that.
ICON_SUFFIXES: Final[tuple[str, ...]] = (".svg", ".png", ".jpg", ".jpeg", ".gif")

#: Preference for output whose consumer is unknown or cannot read SVG.
RASTER_FIRST: Final[tuple[str, ...]] = (".png", ".jpg", ".jpeg", ".gif", ".svg")

#: Preference where an SVG icon is known to work, and is the better picture.
VECTOR_FIRST: Final[tuple[str, ...]] = (".svg", ".png", ".jpg", ".jpeg", ".gif")

#: What ``--icons`` is given to turn a theme back off, e.g. to override a
#: setting that came from somewhere else. Spelled out so "no icons" is sayable.
NO_ICONS: Final = "none"

#: Directory holding the themes that ship with netgraph, one subdirectory each.
_BUNDLED_DIR: Final = Path(__file__).resolve().parent / "iconsets"


@dataclass(frozen=True, slots=True)
class IconTheme:
    """A named directory of icons, keyed by element kind."""

    #: What ``--icons`` was given, for diagnostics.
    name: str
    #: Directory the image files live in.
    directory: Path
    #: One clause for help text.
    description: str = ""

    def file_for(self, kind: str, *, prefer: Sequence[str] = RASTER_FIRST) -> str | None:
        """The file name inside :attr:`directory` to draw ``kind`` with.

        Returns the bare name rather than a path: it is emitted as an ``<IMG
        SRC>`` alongside a single ``imagepath`` graph attribute, so the one
        machine-specific string in a rendering stays in one place.

        ``None`` when the theme has no picture for ``kind``, which is not an
        error — the renderer falls back to the built-in shape for that node, and
        a partial theme is a legitimate thing to have.
        """
        for suffix in prefer:
            candidate = self.directory / f"{kind}{suffix}"
            if candidate.is_file():
                return candidate.name
        return None

    def files(
        self, kinds: Iterable[str], *, prefer: Sequence[str] = RASTER_FIRST
    ) -> Mapping[str, str]:
        """The file name for each of ``kinds`` the theme can draw."""
        resolved = {
            kind: name
            for kind in dict.fromkeys(kinds)
            if (name := self.file_for(kind, prefer=prefer)) is not None
        }
        return MappingProxyType(resolved)

    def kinds(self) -> tuple[str, ...]:
        """The kinds this theme has a picture for, in :data:`ICON_KINDS` order."""
        return tuple(kind for kind in ICON_KINDS if self.file_for(kind) is not None)

    @property
    def is_empty(self) -> bool:
        """Does the theme draw nothing at all?"""
        return not self.kinds()


#: The theme that ships with netgraph.
CISCO: Final = IconTheme(
    name="cisco",
    directory=_BUNDLED_DIR / "cisco",
    description="Cisco-style network topology icons, drawn for netgraph",
)

#: Themes reachable by name; anything else ``--icons`` accepts is a directory.
BUNDLED_THEMES: Final[Mapping[str, IconTheme]] = MappingProxyType({CISCO.name: CISCO})


def suffix_order(target: str) -> tuple[str, ...]:
    """Which image format to prefer when a rendering is destined for ``target``.

    ``target`` is an output format name. See the module docstring for why only
    Graphviz's own SVG output gets the vector icons.
    """
    return VECTOR_FIRST if target == "svg" else RASTER_FIRST


def icon_theme(spec: str | IconTheme | None) -> IconTheme | None:
    """Resolve what ``--icons`` was given to a theme, or ``None`` for no icons.

    Accepts a bundled theme name, ``"none"``, a path to a directory of icons, or
    an already-resolved :class:`IconTheme` (so a caller may pass its own).

    Raises:
        RenderError: The name is neither a bundled theme nor a usable directory.
    """
    if spec is None or isinstance(spec, IconTheme):
        return spec
    name = spec.strip()
    if not name or name == NO_ICONS:
        return None
    bundled = BUNDLED_THEMES.get(name)
    if bundled is not None:
        return bundled
    return _directory_theme(name)


def _directory_theme(name: str) -> IconTheme:
    """A theme read from a directory the user pointed at.

    Every failure here is a mistake the user can fix, so each one says what was
    looked for rather than only what was not found.
    """
    directory = Path(name).expanduser()
    if not directory.exists():
        raise RenderError(
            f"unknown icon theme {name!r}: it is neither a built-in theme "
            f"({_bundled_names()}) nor a directory that exists"
        )
    if not directory.is_dir():
        raise RenderError(
            f"icon theme {name!r} is a file, not a directory; a theme is a directory of "
            f"images named after element kinds, e.g. {_example_files()}"
        )
    theme = IconTheme(name=name, directory=directory, description="user-supplied icons")
    if theme.is_empty:
        raise RenderError(
            f"icon theme directory {directory} holds no usable icon: expected files named "
            f"after the element kinds ({', '.join(ICON_KINDS)}) with one of the extensions "
            f"{', '.join(ICON_SUFFIXES)}, e.g. {_example_files()}"
        )
    return theme


def theme_choices() -> Iterator[str]:
    """Bundled theme names, for help text, with the off switch last."""
    yield from BUNDLED_THEMES
    yield NO_ICONS


def _bundled_names() -> str:
    return ", ".join(BUNDLED_THEMES)


def _example_files() -> str:
    return "router.svg, switch.png"
