"""Shell completion for bash, zsh and fish.

Click generates the mechanical half for free: the subcommand names, the option
names, and the values of every ``click.Choice``. What it cannot know is
anything that depends on *this* inventory — which is exactly the half worth
having, because an element name is the one argument a user cannot guess. The
completers below close that gap:

``--neighbors-of``, ``show NAME``
    The elements of the tree named by ``-i``, loaded on demand. A completer
    runs inside an interactive shell, so it never raises and never prints: a
    tree that does not load yet (the file being edited is half-written, which
    is precisely when completion is asked for) simply offers nothing.
``--disable``
    Rule ids with their summaries, plus the ``NG-*`` aliases once the typed
    prefix looks like one, and the ``*`` wildcard.
``--profile``
    The names of the ``[profile.<name>]`` blocks in the inventory's
    ``netgraph.toml``, each described by the settings it overrides. A profile
    exists only in *this* tree, so this is the one option Click could never
    complete on its own.
``--kind``, ``-f/--format``, ``--layer``, ``export FORMAT``
    Static value spaces, but each candidate is offered *with its description*,
    which zsh and fish display next to it. ``click.Choice`` alone would list
    six bare words and make the user go and read ``--help``.

Every completer answers from the same registries the commands themselves use,
so a format, kind, layer or rule added elsewhere completes without a line
changing here.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Final

import click
from click.shell_completion import CompletionItem, get_completion_class

from netgraph.errors import NetgraphError, count_text
from netgraph.export import EXPORTERS
from netgraph.models import DOCUMENT_KINDS
from netgraph.models.fielddocs import KIND_NOTES
from netgraph.render import RENDERERS, Layer
from netgraph.rules import RULES, WILDCARD

__all__ = [
    "PROG_NAME",
    "SHELLS",
    "complete_element",
    "complete_export_format",
    "complete_format",
    "complete_kind",
    "complete_layer",
    "complete_namespace",
    "complete_node",
    "complete_profile",
    "complete_rule",
    "completion_script",
]

#: The shells netgraph ships a completion script for. Click can generate for
#: exactly these three; anything else needs its own generator, not a flag.
SHELLS: Final[tuple[str, ...]] = ("bash", "zsh", "fish")

#: The name the console script is installed under. It decides the name of the
#: environment variable the generated script sets, so the two must agree.
PROG_NAME: Final = "netgraph"

#: What each layer draws, for the shells that show a description per candidate.
_LAYER_HELP: Final[dict[str, str]] = {
    Layer.PHYSICAL.value: "the cabling record: patch panels and every cable segment",
    Layer.L1.value: "physical topology: what is plugged into what",
    Layer.L2.value: "the same topology annotated with VLANs",
    Layer.L3.value: "IP subnets and the elements addressed in them",
    Layer.OVERLAY.value: "tunnels, their endpoints and what they run inside",
    Layer.ROUTING.value: "BGP sessions and OSPF adjacencies, clustered by VRF",
    Layer.RACK.value: "rack elevations: what is bolted where, and what is free",
}


# --------------------------------------------------------------------------- #
# The script
# --------------------------------------------------------------------------- #


def completion_script(shell: str, command: click.Command, *, prog_name: str = PROG_NAME) -> str:
    """The completion script for ``shell``, ready to be sourced.

    Args:
        shell: One of :data:`SHELLS`.
        command: The command tree to complete, i.e. :data:`netgraph.cli.cli`.
        prog_name: The name the script is installed under. Only worth changing
            for a wrapper that renames the executable.

    Raises:
        NetgraphError: ``shell`` is not one netgraph can generate for.
    """
    completion = get_completion_class(shell)
    if completion is None or shell not in SHELLS:
        raise NetgraphError(
            f"no completion script for {shell!r}; netgraph can generate for {', '.join(SHELLS)}"
        )
    variable = f"_{prog_name.replace('-', '_').upper()}_COMPLETE"
    source = completion(command, {}, prog_name, variable).source()
    return source if source.endswith("\n") else source + "\n"


# --------------------------------------------------------------------------- #
# Static value spaces
# --------------------------------------------------------------------------- #


def complete_kind(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Element kinds, each with the sentence that distinguishes it.

    The kinds on offer are the ones the *parameter* accepts, so ``--kind`` on a
    filter (which cannot select a cable, there being no cable node in a graph)
    and ``--kind`` on ``schema`` (which can) stay correct without two lists.
    """
    return _items(
        ((kind, _kind_help(kind)) for kind in _accepted(param, DOCUMENT_KINDS)), incomplete
    )


def _kind_help(kind: str) -> str:
    """The first sentence of the kind's note, as prose rather than Markdown."""
    note = KIND_NOTES.get(kind, "")
    return note.partition(". ")[0].rstrip(".").replace("`", "")


def complete_format(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Output formats, described by the renderer registry itself."""
    return _items(
        ((name, renderer.description) for name, renderer in RENDERERS.items()), incomplete
    )


def complete_export_format(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """``netgraph export`` formats, described by the export registry itself."""
    return _items(
        ((name, exporter.description) for name, exporter in EXPORTERS.items()), incomplete
    )


def complete_layer(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """The layer views, described by what each one draws."""
    return _items(((layer.value, _LAYER_HELP[layer.value]) for layer in Layer), incomplete)


def complete_rule(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Validation rule ids, with their one-line summaries.

    The ``NG-*`` aliases are offered only once the typed prefix looks like one:
    listing both vocabularies for an empty prefix would double the candidate
    list with synonyms of what is already in it.
    """
    if incomplete.upper().startswith("N"):
        return _items(
            ((alias, rule.summary) for rule in RULES for alias in rule.aliases),
            incomplete,
            fold_case=True,
        )
    return _items(
        [(WILDCARD, "every rule at once"), *((rule.id, rule.summary) for rule in RULES)],
        incomplete,
        fold_case=True,
    )


# --------------------------------------------------------------------------- #
# The inventory
# --------------------------------------------------------------------------- #


def complete_element(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Every element of the inventory named by ``-i``, cables included.

    For ``show``, which resolves any element by fully-qualified or unique short
    name.
    """
    return _element_items(ctx, incomplete, nodes_only=False)


def complete_node(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """The elements that become graph nodes, i.e. everything but cables.

    For ``--neighbors-of``: a cable is an *edge*, so completing one would offer
    a name the filter then rejects.
    """
    return _element_items(ctx, incomplete, nodes_only=True)


def complete_namespace(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Every namespace holding an element, and every ancestor of one.

    For ``--namespace`` and ``--collapse``, both of which match a namespace and
    everything below it: ``sites`` is therefore a legal value even when no
    element sits directly in it, so it has to be offered. Ordered outermost
    first, which is the order a reader narrows in.
    """
    elements = _load_elements(_inventory_path(ctx), nodes_only=True)
    counts: dict[str, int] = {}
    for fqn in elements:
        namespace = fqn.rpartition("/")[0]
        while namespace:
            counts[namespace] = counts.get(namespace, 0) + 1
            namespace = namespace.rpartition("/")[0]
    ordered = sorted(counts, key=lambda namespace: (namespace.count("/"), namespace))
    return _items(
        ((namespace, count_text(counts[namespace], "element")) for namespace in ordered), incomplete
    )


def complete_profile(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """The profiles declared by the inventory's ``netgraph.toml``.

    Like every completer here it answers with nothing rather than failing: a
    configuration file with a typo in it is exactly the state a user is in when
    they reach for <TAB>, and a traceback in the middle of a command line is
    not an improvement on an empty list.
    """
    from netgraph.config import load_config  # imported late: completion must start fast
    from netgraph.settings import profile_summaries

    root = _inventory_path(ctx)
    try:
        config = load_config(root if root.is_dir() else root.parent)
    except Exception:  # see the docstring: a completer never fails, it offers nothing
        return []
    return _items(profile_summaries(config.profiles), incomplete)


def _element_items(
    ctx: click.Context, incomplete: str, *, nodes_only: bool
) -> list[CompletionItem]:
    """Names from the inventory, fully qualified plus the unambiguous short ones.

    Both spellings resolve — ``sites/hq/sw1`` and, while it is unique, ``sw1`` —
    so both are offered, and the help text carries the kind so a list of twenty
    names is still readable.
    """
    elements = _load_elements(_inventory_path(ctx), nodes_only=nodes_only)
    short_names: dict[str, list[str]] = {}
    for fqn in elements:
        short_names.setdefault(fqn.rpartition("/")[2], []).append(fqn)

    candidates = [(fqn, kind) for fqn, kind in elements.items()]
    candidates.extend(
        (short, elements[matches[0]])
        for short, matches in short_names.items()
        if len(matches) == 1 and matches[0] != short
    )
    return _items(candidates, incomplete)


def _inventory_path(ctx: click.Context) -> Path:
    """The ``-i`` of the command line being completed, or the current directory.

    The option belongs to the group, so it is parsed into an ancestor context;
    during completion that parse is resilient, and an ``-i`` naming a path that
    does not exist yet leaves ``None`` behind rather than failing.
    """
    node: click.Context | None = ctx
    while node is not None:
        value = node.params.get("inventory")
        if isinstance(value, (str, Path)):
            return Path(value)
        node = node.parent
    return Path.cwd()


def _load_elements(root: Path, *, nodes_only: bool) -> dict[str, str]:
    """Fully-qualified name -> kind, or nothing at all if the tree will not load.

    Completion runs on every ``<TAB>``, in a shell that shows the user whatever
    a completer writes to a stream and treats a traceback as a broken install.
    A half-written document is the normal state of an inventory while it is
    being edited, so anything going wrong here means "no suggestions", never an
    error: the load errors are collected on the inventory rather than raised,
    and the wider catch covers the tree not existing yet.
    """
    from netgraph.loader import load_tree  # imported late: completion must start fast

    try:
        inventory = load_tree(root)
    except Exception:  # see the docstring: a completer never fails, it offers nothing
        return {}
    return {
        fqn: element.kind
        for fqn, element in inventory.elements.items()
        if not (nodes_only and fqn in inventory.cables)
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _accepted(param: click.Parameter, default: Sequence[str]) -> Sequence[str]:
    """The choices ``param`` declares, or ``default`` when it declares none."""
    if isinstance(param.type, click.Choice):
        return [str(choice) for choice in param.type.choices]
    return default


def _items(
    candidates: Iterable[tuple[str, str]], incomplete: str, *, fold_case: bool = False
) -> list[CompletionItem]:
    """Keep the candidates ``incomplete`` is a prefix of, in the order given."""
    prefix = incomplete.upper() if fold_case else incomplete
    return [
        CompletionItem(value, help=help_text or None)
        for value, help_text in _unique(candidates)
        if (value.upper() if fold_case else value).startswith(prefix)
    ]


def _unique(candidates: Iterable[tuple[str, str]]) -> Iterator[tuple[str, str]]:
    seen: set[str] = set()
    for value, help_text in candidates:
        if value not in seen:
            seen.add(value)
            yield value, help_text
