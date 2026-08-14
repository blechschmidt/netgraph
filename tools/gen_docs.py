#!/usr/bin/env python3
"""Regenerate the machine-derived regions of the documentation.

Some of what the docs must say is already stated precisely in the code: the
flags of a command live in its ``click`` decorators, the rule catalogue lives in
:data:`netgraph.rules.RULES`. Writing those out by hand means keeping two lists
in step, and the second list is the one that quietly goes stale.

So the pages carry *regions* instead::

    <!-- generated: options render -->
    | Flag | Value | Default | Meaning |
    ...
    <!-- /generated -->

Everything between the markers is produced from the source of truth by this
script; everything outside them is prose written by a human. The kinds of region
are:

``synopsis <command path>``
    The usage line, from ``Command.collect_usage_pieces``.
``options <command path>``
    One row per option, with its value placeholder, default and help text.
``arguments <command path>``
    One row per positional argument.
``command-index base=<prefix>``
    The table of every command, linked to its page under ``<prefix>``.
``rule-index``
    Every validation rule, with severity, schema alias and a deep link.
``keybindings``
    Every command the web interface has, and the keys that reach it, from
    :data:`netgraph.web.bindings.BINDINGS` — which is also what the page fetches
    to build its palette and its shortcut sheet.

Usage::

    python tools/gen_docs.py            # rewrite the regions in place
    python tools/gen_docs.py --check    # exit 1 if any region is out of date

``tests/test_docs.py`` runs the ``--check`` path, so a flag added to the CLI
without regenerating the docs fails the suite instead of shipping a reference
that has drifted.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import click  # noqa: E402

from netgraph.cli import cli  # noqa: E402
from netgraph.rules import RULES  # noqa: E402
from netgraph.web.bindings import markdown_table  # noqa: E402

DOCS: Final = REPO_ROOT / "docs"
COMMANDS: Final = DOCS / "commands"

#: ``<!-- generated: <spec> -->`` … ``<!-- /generated -->``.
REGION_RE: Final = re.compile(
    r"(?P<open><!-- generated: (?P<spec>[^>]*?) -->\n)"
    r"(?P<body>.*?)"
    r"(?P<close><!-- /generated -->)",
    re.DOTALL,
)

#: The page each command is documented on, relative to ``docs/commands/``.
#: Sub-commands share their group's page.
PAGE: Final[dict[str, str]] = {
    "init": "init.md",
    "import": "import.md",
    "drift": "drift.md",
    "validate": "validate.md",
    "fmt": "fmt.md",
    "edit": "edit.md",
    "edit apply": "edit.md",
    "edit set": "edit.md",
    "edit unset": "edit.md",
    "edit create": "edit.md",
    "edit delete": "edit.md",
    "edit rename": "edit.md",
    "edit move": "edit.md",
    "edit connect": "edit.md",
    "edit disconnect": "edit.md",
    "edit add-interface": "edit.md",
    "edit remove-interface": "edit.md",
    "plan": "plan.md",
    "apply": "apply.md",
    "diff": "diff.md",
    "render": "render.md",
    "layout": "layout.md",
    "path": "path.md",
    "watch": "watch.md",
    "web": "web.md",
    "list": "list.md",
    "ipam": "ipam.md",
    "export": "export.md",
    "report": "report.md",
    "show": "show.md",
    "rules": "rules.md",
    "schema": "schema.md",
    "config": "config.md",
    "config show": "config.md",
    "cache": "cache.md",
    "cache info": "cache.md",
    "cache clear": "cache.md",
    "completion": "completion.md",
    "version": "version.md",
}

#: One line per command for the index tables, in the order a reader meets them.
INDEX_ORDER: Final = [
    "init",
    "import",
    "drift",
    "validate",
    "fmt",
    "edit set",
    "edit unset",
    "edit create",
    "edit delete",
    "edit rename",
    "edit move",
    "edit connect",
    "edit disconnect",
    "edit add-interface",
    "edit remove-interface",
    "edit apply",
    "plan",
    "diff",
    "apply",
    "render",
    "layout",
    "watch",
    "web",
    "path",
    "list",
    "ipam",
    "export",
    "report",
    "show",
    "rules",
    "schema",
    "config show",
    "cache info",
    "cache clear",
    "completion",
    "version",
]

#: What each command is for, in one clause. ``short_help`` is a sentence aimed
#: at ``--help``; an index table wants something shorter and comparable.
SUMMARY: Final[dict[str, str]] = {
    "init": "Scaffold a new inventory, ready to validate and render.",
    "import": "Build a first inventory from output captured on live devices.",
    "drift": "Compare a live network against the declared inventory.",
    "validate": "Check the inventory; the gate for CI and pre-commit.",
    "fmt": "Rewrite inventory YAML into the canonical form.",
    "edit set": "Set a field on an element, in place, comments and all.",
    "edit unset": "Remove a field from an element.",
    "edit create": "Declare a new element and place its document.",
    "edit delete": "Remove an element, and what cannot survive it.",
    "edit rename": "Rename an element and every reference to it.",
    "edit move": "Move an element's document to another file.",
    "edit connect": "Cable two interfaces together.",
    "edit disconnect": "Remove a cable.",
    "edit add-interface": "Add an interface to an element.",
    "edit remove-interface": "Remove an interface from an element.",
    "edit apply": "Apply operations given as JSON; the programmatic face.",
    "plan": "Diff two inventory states into a reviewable changeset.",
    "apply": "Execute a plan against the inventory files.",
    "diff": "Draw the difference between two inventory states as one diagram.",
    "render": "Draw the graph as SVG, PNG, PDF, DOT, Mermaid, JSON or HTML.",
    "layout": "Store the diagram's arrangement, so a hand-placed node stays put.",
    "watch": "Re-render on every save, optionally serving the result.",
    "web": "Edit the YAML and see the diagram side by side in a browser.",
    "path": "Trace how two elements reach each other, hop by hop.",
    "list": "Tabulate devices, cables, tunnels, VLANs, BSSs or subnets.",
    "ipam": "Report utilisation, free space, overlaps and aggregates.",
    "export": "Emit hosts files, DNS zones, Ansible, Prometheus, cable lists.",
    "report": "Write the as-built documentation: a page per site and per device.",
    "show": "Print one element as it was resolved, expansions included.",
    "rules": "List the validation rules and their ids.",
    "schema": "Write the JSON Schema for editor completion.",
    "config show": "Show the resolved settings and where each value came from.",
    "cache info": "Report where the parse cache is and what is in it.",
    "cache clear": "Delete this inventory's cached documents.",
    "completion": "Print the shell completion script.",
    "version": "Report the netgraph, Python and Graphviz versions in use.",
}


# --------------------------------------------------------------------------- #
# Walking the CLI
# --------------------------------------------------------------------------- #


def command_paths() -> Iterator[str]:
    """Every invocable command, as a space-separated path below ``netgraph``."""

    def walk(command: click.Command, path: tuple[str, ...]) -> Iterator[str]:
        if path:
            yield " ".join(path)
        if isinstance(command, click.Group):
            for name, sub in sorted(command.commands.items()):
                yield from walk(sub, (*path, name))

    yield from walk(cli, ())


def lookup(path: str) -> click.Command:
    """Resolve ``"config show"`` to the command object it names."""
    command: click.Command = cli
    for part in path.split():
        assert isinstance(command, click.Group), f"{path}: {part} is not below a group"
        found = command.commands.get(part)
        assert found is not None, f"{path}: no command named {part}"
        command = found
    return command


def context_for(path: str) -> click.Context:
    """A context chain matching ``path``, which ``click`` needs for metavars."""
    context = click.Context(cli, info_name="netgraph")
    command: click.Command = cli
    for part in path.split():
        assert isinstance(command, click.Group)
        command = command.commands[part]
        context = click.Context(command, parent=context, info_name=part)
    return context


def flags_of(path: str) -> list[str]:
    """Every option string of a command, long and short, primary and secondary.

    Positional arguments are excluded: ``Argument.opts`` holds the parameter's
    *name*, which is not something a reader ever types.
    """
    return [
        opt
        for param in lookup(path).params
        if isinstance(param, click.Option)
        for opt in (*param.opts, *param.secondary_opts)
    ]


def arguments_of(path: str) -> list[str]:
    """The metavar of each positional argument, as the synopsis spells it."""
    context = context_for(path)
    return [
        param.make_metavar(context) if _takes_context() else param.make_metavar()  # type: ignore[call-arg]
        for param in lookup(path).params
        if isinstance(param, click.Argument)
    ]


# --------------------------------------------------------------------------- #
# Rendering a parameter
# --------------------------------------------------------------------------- #


def cell(text: str) -> str:
    """Collapse a help string into one table cell."""
    return re.sub(r"\s+", " ", text).replace("|", "\\|").strip()


def value_of(param: click.Parameter, context: click.Context) -> str:
    """The placeholder a value goes in, or ``—`` for a switch."""
    if isinstance(param, click.Option) and param.is_flag:
        return "—"
    metavar = param.make_metavar(context) if _takes_context() else param.make_metavar()  # type: ignore[call-arg]
    if metavar == "INTEGER RANGE":
        # Click's own placeholder says only "a number in some range". The bounds
        # are the useful half, and they are on the type.
        metavar = f"INTEGER, {bounds_of(param)}"
    # A choice metavar is ``[a|b|c]``, and an unescaped bar ends a table cell.
    return "`" + metavar.replace("|", "\\|") + "`"


def bounds_of(param: click.Parameter) -> str:
    """``1-4094``, ``>= 0`` — whichever the range actually constrains."""
    kind = param.type
    minimum = getattr(kind, "min", None)
    maximum = getattr(kind, "max", None)
    if minimum is not None and maximum is not None:
        return f"{minimum}-{maximum}"
    if minimum is not None:
        return f">= {minimum}"
    if maximum is not None:
        return f"<= {maximum}"
    return "any"


def _takes_context() -> bool:
    """``make_metavar`` grew a ``ctx`` parameter in click 8.2."""
    import inspect

    return "ctx" in inspect.signature(click.Parameter.make_metavar).parameters


def default_of(param: click.Parameter) -> str:
    """How the default is worth showing to a reader."""
    # ``show_default`` is an option's; an argument has no such attribute.
    shown = getattr(param, "show_default", None)
    if isinstance(shown, str) and shown:
        return shown
    default = param.default
    if callable(default):
        # ``default=Path.cwd`` and friends; the option carries the prose form.
        return "—"
    if isinstance(param, click.Option) and param.is_flag:
        if param.secondary_opts:
            if default is None:
                return "—"
            return f"`{param.opts[0] if default else param.secondary_opts[0]}`"
        return "off" if not default else "on"
    if default is None:
        return "—"
    if isinstance(default, tuple):
        if not default or default[0] is None:
            return "—"
        return ", ".join(f"`{item}`" for item in default)
    if isinstance(default, bool):
        return "on" if default else "off"
    if default == "" or str(default).startswith("Sentinel."):
        return "—"
    return f"`{default}`"


def option_rows(path: str) -> Iterator[str]:
    command, context = lookup(path), context_for(path)
    for param in command.params:
        if not isinstance(param, click.Option):
            continue
        names = ", ".join(f"`{opt}`" for opt in (*param.opts, *param.secondary_opts))
        meaning = cell(param.help or "")
        if param.multiple and "epeatable" not in meaning:
            meaning = f"{meaning} Repeatable." if meaning else "Repeatable."
        yield f"| {names} | {value_of(param, context)} | {default_of(param)} | {meaning} |"


def argument_rows(path: str) -> Iterator[str]:
    command, context = lookup(path), context_for(path)
    for param in command.params:
        if not isinstance(param, click.Argument):
            continue
        required = "yes" if param.required else "no"
        repeat = "any number" if param.nargs == -1 else str(param.nargs)
        yield f"| {value_of(param, context)} | {required} | {repeat} | {default_of(param)} |"


# --------------------------------------------------------------------------- #
# The regions
# --------------------------------------------------------------------------- #


def synopsis(path: str) -> str:
    command, context = lookup(path), context_for(path)
    pieces = command.collect_usage_pieces(context)
    prefix = "netgraph [GLOBAL OPTIONS]" if path else "netgraph"
    return "```text\n" + " ".join([prefix, path, *pieces]).strip() + "\n```\n"


def options_table(path: str) -> str:
    rows = list(option_rows(path))
    if not rows:
        return "*No options of its own; the global options apply.*\n"
    header = ["| Flag | Value | Default | Meaning |", "|---|---|---|---|"]
    return "\n".join([*header, *rows]) + "\n"


def arguments_table(path: str) -> str:
    rows = list(argument_rows(path))
    if not rows:
        return "*Takes no positional arguments.*\n"
    header = ["| Argument | Required | Count | Default |", "|---|---|---|---|"]
    return "\n".join([*header, *rows]) + "\n"


def command_index(base: str) -> str:
    lines = ["| Command | What it does | Reference |", "|---|---|---|"]
    for path in INDEX_ORDER:
        page = PAGE[path]
        lines.append(
            f"| [`netgraph {path}`]({base}{page}) | {SUMMARY[path]} | [{page}]({base}{page}) |"
        )
    return "\n".join(lines) + "\n"


def rule_index() -> str:
    """Every rule, with the ``NG-*`` alias the specification calls it by."""
    lines = ["| Id | Schema id | Severity | Rule |", "|---|---|---|---|"]
    for rule in RULES:
        aliases = ", ".join(f"`{alias}`" for alias in rule.aliases) or "—"
        anchor = f"validation-rules.md#{rule.anchor}"
        lines.append(
            f"| [`{rule.id}`]({anchor}) | {aliases} | {rule.severity} | {cell(rule.title)} |"
        )
    return "\n".join(lines) + "\n"


def body_for(spec: str) -> str:
    """The content a region declares it holds."""
    kind, _, rest = spec.partition(" ")
    rest = rest.strip()
    if kind == "synopsis":
        return synopsis(rest)
    if kind == "options":
        return options_table(rest)
    if kind == "arguments":
        return arguments_table(rest)
    if kind == "command-index":
        prefix, _, base = rest.partition("=")
        assert prefix == "base", f"command-index takes base=<prefix>, not {rest!r}"
        return command_index(base)
    if kind == "rule-index":
        return rule_index()
    if kind == "keybindings":
        return markdown_table()
    raise SystemExit(f"unknown generated region {spec!r}")


def regenerate(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return match["open"] + body_for(match["spec"]) + match["close"]

    return REGION_RE.sub(replace, text)


def pages() -> list[Path]:
    """Every Markdown file that may hold a region."""
    return sorted(
        path
        for path in [*DOCS.rglob("*.md"), REPO_ROOT / "README.md", REPO_ROOT / "CONTRIBUTING.md"]
        if path.is_file()
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the generated doc regions.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 when a committed region is out of date.",
    )
    args = parser.parse_args(argv)

    stale: list[Path] = []
    for path in pages():
        current = path.read_text(encoding="utf-8")
        wanted = regenerate(current)
        if current == wanted:
            continue
        if args.check:
            stale.append(path)
        else:
            path.write_text(wanted, encoding="utf-8")
            print(f"updated {path.relative_to(REPO_ROOT)}")

    if stale:
        for path in stale:
            print(
                f"{path.relative_to(REPO_ROOT)}: generated region is out of date", file=sys.stderr
            )
        print("run 'python tools/gen_docs.py'", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through tests/test_docs.py
    raise SystemExit(main())
