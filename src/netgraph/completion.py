"""Shell completion for bash, zsh, fish and PowerShell.

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
``--choose``
    The ``RULE=FIX`` pairs ``validate --fix`` accepts, narrowed to that rule's
    repairs once an ``=`` has been typed. Only the rules that admit more than
    one repair are offered: the rest need no choosing.
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

Click generates the script for bash, zsh and fish itself. PowerShell it does
not, so :class:`PowerShellComplete` supplies the missing half — the same
completers, reached through the same protocol, with the shell-side glue written
out below. It is registered with Click on import of this module, which is why
``netgraph completion powershell`` works at all.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Final

import click
from click.shell_completion import (
    CompletionItem,
    ShellComplete,
    add_completion_class,
    get_completion_class,
)

from netgraph.errors import NetgraphError, count_text
from netgraph.export import EXPORTERS
from netgraph.models import DOCUMENT_KINDS
from netgraph.models.fielddocs import KIND_NOTES
from netgraph.render import RENDERERS, Layer
from netgraph.rules import RULES, WILDCARD, resolve_rule_id

__all__ = [
    "PROG_NAME",
    "SHELLS",
    "PowerShellComplete",
    "complete_element",
    "complete_export_format",
    "complete_fix",
    "complete_format",
    "complete_kind",
    "complete_layer",
    "complete_namespace",
    "complete_node",
    "complete_profile",
    "complete_rule",
    "completion_script",
]

#: The shells netgraph ships a completion script for. Click generates the first
#: three; ``powershell`` is :class:`PowerShellComplete`, below. Anything else
#: needs its own generator, not a flag.
SHELLS: Final[tuple[str, ...]] = ("bash", "zsh", "fish", "powershell")

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
    Layer.POWER.value: "power: PDUs, PoE budgets and the feeds into every load",
    Layer.IDENTITY.value: "identity: users, groups and who is a member of what",
}


# --------------------------------------------------------------------------- #
# PowerShell
# --------------------------------------------------------------------------- #

#: What separates the three fields of one candidate on the wire. A tab, not the
#: comma Click's own scripts use: every ``help`` here is a prose summary written
#: for a human, and several of them contain commas.
_FIELD_SEPARATOR: Final = "\t"

#: The shell half. PowerShell has no ``compgen``: a native completer is a script
#: block registered against the command name, which is handed the parsed AST and
#: returns ``CompletionResult`` objects. So the glue is longer than the bash
#: equivalent, and it does three things.
#:
#: It flattens the AST into one word per line. Passing the raw command line and
#: re-splitting it in Python would mean reimplementing PowerShell's quoting
#: rules; taking ``StringConstantExpressionAst.Value`` instead gets the
#: *unquoted* word from the parser that already did the work, so ``-i 'my net'``
#: arrives as one argument with a space in it.
#:
#: It restores whatever the three environment variables held before, rather than
#: deleting them. A completer runs in the user's own session, and clearing a
#: variable somebody had set for their own reasons would be a side effect of
#: pressing Tab.
#:
#: And it quotes a candidate that needs it. A namespace comes from a directory
#: name, so ``sites/Building A`` is a legal completion; inserted bare it would
#: become two arguments.
#:
#: Interpolated with ``%``, so any literal percent sign here must be doubled.
#: There are none, deliberately -- ``%`` is also PowerShell's alias for
#: ``ForEach-Object``, and the two spellings would be indistinguishable.
_POWERSHELL_SOURCE: Final = """\
# PowerShell completion for %(prog_name)s.
#
# Load it in the current session:
#
#     %(prog_name)s completion powershell | Out-String | Invoke-Expression
#
# Or install it permanently, by putting that same line in your profile:
#
#     notepad $PROFILE
#
# Requires PowerShell 5.1 or newer. Completion is inventory-aware: it loads the
# tree named by -i, so '%(prog_name)s show <Tab>' offers your own element names.

Register-ArgumentCompleter -Native -CommandName %(prog_name)s, %(prog_name)s.exe -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $stringAst = [System.Management.Automation.Language.StringConstantExpressionAst]
    $words = @()
    foreach ($element in $commandAst.CommandElements) {
        if ($element -is $stringAst) {
            $words += $element.Value
        } else {
            $words += $element.Extent.Text
        }
    }
    # An empty $wordToComplete means the cursor sits after a space: a new,
    # still-empty word is being completed rather than the last one typed.
    if ([string]::IsNullOrEmpty($wordToComplete)) {
        $words += ''
    }

    $names = @('%(complete_var)s', 'COMP_WORDS', 'COMP_CWORD')
    $saved = @{}
    foreach ($name in $names) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name)
    }

    try {
        [Environment]::SetEnvironmentVariable('%(complete_var)s', 'powershell_complete')
        [Environment]::SetEnvironmentVariable('COMP_WORDS', ($words -join "`n"))
        [Environment]::SetEnvironmentVariable('COMP_CWORD', ($words.Count - 1))

        & $commandAst.CommandElements[0].Extent.Text 2>$null | ForEach-Object {
            $fields = $_ -split "`t", 3
            if ($fields.Count -lt 2) { return }
            $value = $fields[1]
            $tooltip = if ($fields.Count -ge 3 -and $fields[2].Trim()) { $fields[2] } else { $value }
            # A candidate holding a space is one argument, not two.
            $insert = if ($value -match '\\s') { "'" + $value.Replace("'", "''") + "'" } else { $value }
            [System.Management.Automation.CompletionResult]::new(
                $insert, $value, 'ParameterValue', $tooltip)
        }
    } finally {
        foreach ($name in $names) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name])
        }
    }
}
"""


class PowerShellComplete(ShellComplete):
    """Click's completion protocol, spoken to PowerShell.

    Click ships generators for bash, zsh and fish only, so this is the one shell
    netgraph has to supply itself. It is *only* the transport: every candidate
    still comes from the completers in this module, so ``--profile <Tab>`` reads
    the same ``netgraph.toml`` on Windows as it does anywhere else.

    Registered with Click on import (see the call below the class), which is what
    makes :func:`click.shell_completion.get_completion_class` know the name and
    therefore what makes ``_NETGRAPH_COMPLETE=powershell_complete`` do anything.
    """

    name = "powershell"
    source_template = _POWERSHELL_SOURCE

    def get_completion_args(self) -> tuple[list[str], str]:
        """The words typed so far, and the one being completed.

        ``COMP_WORDS`` is newline-separated because a newline is the one
        character no shell word can contain, which makes the split exact —
        splitting on whitespace would rejoin ``-i 'my net'`` into two arguments
        the parser then rejects.

        A missing or unparseable ``COMP_CWORD`` falls back to the last word
        rather than raising: a completer that throws is a shell that prints a
        traceback under the user's cursor.
        """
        words = os.environ.get("COMP_WORDS", "").split("\n")
        try:
            cword = int(os.environ["COMP_CWORD"])
        except (KeyError, ValueError):
            cword = len(words) - 1
        cword = max(0, min(cword, len(words) - 1))
        # ``words[0]`` is the program name, which Click supplies itself.
        return words[1:cword], words[cword]

    def format_completion(self, item: CompletionItem) -> str:
        """One candidate as ``type<TAB>value<TAB>help``.

        The help text is flattened onto one line: PowerShell shows a tooltip in
        a single-line strip, and a newline in it would be read as the end of the
        candidate rather than rendered.
        """
        help_text = " ".join((item.help or "").split()) or " "
        return _FIELD_SEPARATOR.join((item.type, item.value, help_text))


add_completion_class(PowerShellComplete)


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


def complete_fix(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """``RULE=FIX`` pairs for ``validate --choose``.

    Only the rules that admit more than one repair are offered, because they are
    the only ones there is anything to choose *for*: a rule with a single fix is
    applied without being asked about. Once a rule and an ``=`` have been typed,
    the candidates narrow to that rule's repairs and each carries what it does.
    """
    from netgraph.fixes import fixable_rules, spec_for

    rule, separator, _ = incomplete.partition("=")
    if separator:
        spec = spec_for(resolve_rule_id(rule.strip(), strict=False))
        if spec is None:
            return []
        return _items(
            ((f"{rule}={choice.key}", choice.summary) for choice in spec.choices),
            incomplete,
            fold_case=True,
        )
    return _items(
        (
            (f"{rule_id}=", spec.summary)
            for rule_id in fixable_rules()
            if (spec := spec_for(rule_id)) is not None and spec.is_ambiguous
        ),
        incomplete,
        fold_case=True,
    )


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
