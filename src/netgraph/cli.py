"""Command line entry point for netgraph.

One contract per command:

``init``
    Write a starter inventory that already validates and renders, with the
    editor wired to the JSON Schema, so the first document is written with
    completion rather than from memory.
``import``
    The same starting point, for a network that already exists: turn output an
    operator collected from live devices — ``lldpctl -f json``, ``ip -j addr
    show``, a cabling CSV — into that tree. Nothing is fetched and no credential
    is read; the command consumes what has already been printed.
``validate``
    Load, check, report. Exits non-zero when anything is an error, so it drops
    straight into CI.
``render``
    Turn the inventory into a diagram. Validation always runs first and a broken
    inventory is refused unless ``--force``, because the whole point of the tool
    is that the picture agrees with the files — a diagram silently drawn from an
    inventory with a dangling cable is worse than no diagram.
``path``
    Answer the question an engineer actually asks: how does A reach B, and what
    does the traffic cross on the way? Hop by hop, layer-aware, with the VLAN or
    the subnet in force at each step — and, with ``--highlight``, the same
    answer drawn over the whole inventory.
``watch``
    ``render`` on a loop, driven by the filesystem, optionally with a local
    preview served over HTTP. Whatever ``render`` would refuse, ``watch``
    reports without discarding the last diagram that did render.
``web``
    The same pipeline behind an interactive page: a YAML document stream in a
    text area, the diagram beside it, and an info box on every node and link.
    It edits a *stream*, not a tree, which is what lets it work on text that was
    never a folder — a paste, a pipe, a snippet from a ticket.
``list``
    Tabular inventory summaries for humans, with ``--output-format json|yaml``
    for everything else.
``show``
    The fully resolved configuration of one element, defaults materialised.
``rules``
    The validation rules, their severities and their schema aliases.
``schema``
    The JSON Schema of a document, for editor completion and inline errors.
``completion``
    The shell completion script, for bash, zsh or fish. The value completers
    behind it live in :mod:`netgraph.completion`; they are attached to the
    options here with ``shell_complete=``.

``render`` and ``watch`` share every filter and display option, applied by one
decorator (:func:`_graph_options`) so the two can never drift apart.

Output discipline: **data on stdout, commentary on stderr.** ``render`` writes
the diagram to stdout when no ``--output`` is given, so its findings and
progress notes go to stderr; ``netgraph render -f json | jq`` and
``netgraph validate > report.txt`` both do what they look like they do.
``watch`` puts everything on stderr: it produces no stdout data at all, and a
status line every few seconds is commentary by any measure.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, TypeVar

import click
import click.core
import yaml
from click.core import ParameterSource

from netgraph import __version__
from netgraph.completion import (
    SHELLS,
    complete_element,
    complete_format,
    complete_kind,
    complete_layer,
    complete_namespace,
    complete_node,
    complete_rule,
    completion_script,
)
from netgraph.config import Config, ValidationConfig, load_config
from netgraph.console import Align, Console
from netgraph.errors import NetgraphError, RenderError, compact_ids, format_path
from netgraph.importer import (
    DIALECTS,
    Draft,
    build_draft,
    build_files,
    read_inputs,
    write_files,
)
from netgraph.loader import (
    YAML_SUFFIXES,
    Inventory,
    LoadError,
    iter_inventory_files,
    load_stream,
    load_tree,
    read_documents,
)
from netgraph.models import DOCUMENT_KINDS, Element, format_bitrate
from netgraph.render import (
    FORMATS,
    LINK_FIELDS,
    RENDERERS,
    AggregateSpec,
    BundleMode,
    FilterSpec,
    Graph,
    Highlight,
    IconTheme,
    Layer,
    LinkTemplate,
    RenderOptions,
    UnknownElementError,
    advisories_for,
    aggregate_graph,
    build_graph,
    collapse_targets,
    filter_graph,
    icon_theme,
    is_binary_format,
    render,
    render_layers,
    resolve_tunnels,
    supports_highlight,
    supports_icons,
    supports_interaction,
    supports_layers,
    theme_choices,
)
from netgraph.render.dot import DOT_EXECUTABLE
from netgraph.report import FORMATS as REPORT_FORMATS
from netgraph.report import build_report, render_report
from netgraph.rules import RULES, Severity
from netgraph.scaffold import SCHEMA_FILE_NAME, build_scaffold, write_scaffold
from netgraph.schema import build_schema
from netgraph.subnets import subnets_of
from netgraph.trace import DEFAULT_MAX_HOPS, TraceError, TraceResult, render_trace, trace
from netgraph.trace import REPORT_FORMATS as TRACE_FORMATS
from netgraph.validate import Finding
from netgraph.validate import validate as run_validation
from netgraph.watch import (
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    CycleResult,
    InventoryFilter,
    LiveRender,
    PreviewServer,
    RenderRequest,
    Status,
    describe_exposure,
    file_changes,
    run_watch,
)
from netgraph.web import DEFAULT_PORT as WEB_PORT
from netgraph.web import WebServer

__all__ = ["cli", "main"]

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
    "max_content_width": 100,
}

#: Element kinds ``--kind`` can select. A cable is an edge, and a tunnel is one
#: too below the ``overlay`` layer — where it does become a node it is derived
#: from the elements it joins, exactly as a subnet is, so it is kept whenever one
#: of them survives rather than selected in its own right.
NODE_KINDS: Final[tuple[str, ...]] = (
    "switch",
    "router",
    "hub",
    "computer",
    "server",
    "adapter",
)

#: Exit status when an inventory is rejected. The task of every command that
#: checks an inventory is to answer "is this usable?", so they share one answer.
EXIT_INVALID: Final = 1

#: Colour per severity, used for both the group headings and the rule ids.
_SEVERITY_COLOUR: Final[dict[Severity, str]] = {
    Severity.ERROR: "red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}


@dataclass(slots=True)
class AppContext:
    """State shared between the command group and its subcommands."""

    #: Root of the YAML inventory tree (or a single YAML file).
    inventory: Path = field(default_factory=Path.cwd)
    #: Suppress non-essential output.
    quiet: bool = False
    #: Verbosity level, incremented once per ``-v``.
    verbosity: int = 0
    #: Force colour on or off; ``None`` auto-detects from the stream.
    color: bool | None = None

    def log(self, message: str, *, level: int = 1) -> None:
        """Write ``message`` to stderr when the verbosity level allows it."""
        if self.quiet or self.verbosity < level:
            return
        click.echo(message, err=True)

    # -- output ----------------------------------------------------------

    def console(self, *, err: bool = False) -> Console:
        """A console for this run, styled according to the global options."""
        return Console.create(color=self.color, quiet=self.quiet, err=err)

    # -- inventory -------------------------------------------------------

    def load(self, *, keep_provenance: bool = False) -> Inventory:
        """Load the inventory tree.

        Args:
            keep_provenance: Keep the per-field origin tables so a finding can
                be reported at the line and column that caused it. Costs memory
                for the lifetime of the inventory; see
                :func:`~netgraph.loader.load_tree`.

        Raises:
            LoaderError: The path does not exist or is not loadable at all.
                Problems *inside* the tree are collected on the inventory.
        """
        self.log(f"loading inventory from {self.inventory}", level=1)
        inventory = load_tree(self.inventory, keep_provenance=keep_provenance)
        self.log(
            f"loaded {len(inventory.elements)} element(s): "
            f"{len(inventory.devices)} device(s), {len(inventory.cables)} cable(s), "
            f"{len(inventory.adapters)} adapter(s), {len(inventory.tunnels)} tunnel(s)",
            level=1,
        )
        return inventory

    def config(self) -> Config:
        """Read ``netgraph.toml`` from the inventory root, if there is one.

        Raises:
            ConfigurationError: The file exists but cannot be used.
        """
        # ``load_config`` reads a *file* argument as TOML directly, so a
        # single-file inventory must be redirected to its directory.
        root = self.inventory if self.inventory.is_dir() else self.inventory.parent
        config = load_config(root)
        if config.path is not None:
            self.log(f"using configuration {config.path}", level=1)
        return config


@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.version_option(__version__, "-V", "--version", prog_name="netgraph")
@click.option(
    "-i",
    "--inventory",
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        path_type=Path,
    ),
    default=Path.cwd,
    show_default="current directory",
    help="Root folder of the YAML inventory tree, or a single YAML file.",
)
@click.option("-q", "--quiet", is_flag=True, help="Only report errors.")
@click.option("-v", "--verbose", count=True, help="Increase verbosity; repeatable.")
@click.option(
    "--color/--no-color",
    default=None,
    help="Force coloured output on or off. Auto-detected from the terminal by default.",
)
@click.pass_context
def cli(ctx: click.Context, inventory: Path, quiet: bool, verbose: int, color: bool | None) -> None:
    """Declare network elements in YAML and render them as network graphs."""
    ctx.obj = AppContext(inventory=inventory, quiet=quiet, verbosity=verbose, color=color)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


@cli.command("init")
@click.argument(
    "path",
    type=click.Path(file_okay=False, writable=True, path_type=Path),
    default=Path(),
    required=False,
)
@click.option(
    "--minimal",
    is_flag=True,
    default=False,
    help="Write the commented envelope template instead of the example topology.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Write into a directory that already holds something, overwriting files of the same name.",
)
@click.option(
    "--schema/--no-schema",
    "with_schema",
    default=True,
    show_default=True,
    help=(
        "Write the JSON Schema next to the tree and point each document at it with a "
        "yaml-language-server modeline, so an editor completes and checks as you type."
    ),
)
@click.pass_obj
def init_command(
    app: AppContext, path: Path, minimal: bool, force: bool, with_schema: bool
) -> None:
    """Scaffold a working inventory in PATH, or in the current directory.

    The tree that is written validates clean and renders at every layer, so the
    two commands printed at the end succeed before a single line has been
    edited. Existing files are never overwritten without --force.
    """
    console = app.console()
    written = write_scaffold(build_scaffold(minimal=minimal, schema=with_schema), path, force=force)

    console.info(f"created {_plural(len(written), 'file')} in {path}:")
    for file in written:
        console.info(f"  {_display_path(file, path)}")
    for line in _next_steps(path, with_schema=with_schema):
        console.info(line)


def _display_path(file: Path, root: Path) -> str:
    """``file`` as written inside ``root``, falling back to the path as given."""
    try:
        return file.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - every written file is under the root
        return str(file)


def _next_steps(path: Path, *, with_schema: bool) -> Iterator[str]:
    """What to run now, as a copy-pasteable block.

    ``cd`` is printed only when it is needed: the two netgraph commands below it
    are run from the inventory root, which is already the shell's directory when
    ``init`` was given no argument.
    """
    yield ""
    yield "next steps:"
    if path.resolve() != Path.cwd():
        yield f"  cd {path}"
    yield "  netgraph validate"
    yield "  netgraph render -f svg -o network.svg"
    if with_schema:
        yield ""
        yield (
            f"  each document points at {SCHEMA_FILE_NAME}; install a yaml-language-server "
            "(the VS Code YAML extension, nvim's yamlls) for completion and inline errors"
        )


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #


@cli.command("import")
@click.argument("inputs", nargs=-1, metavar="[NAME=]INPUT...")
@click.option(
    "--from",
    "dialect",
    type=click.Choice(DIALECTS),
    default="auto",
    show_default=True,
    help=(
        "Input dialect. 'auto' sniffs each input on its own, so one run may mix all three: "
        "lldp is 'lldpctl -f json', iproute is 'ip -j link show' or 'ip -j addr show', "
        "csv is 'device,port,device,port' cabling rows."
    ),
)
@click.option(
    "--host",
    metavar="NAME",
    default=None,
    help=(
        "Device every input was captured on. An lldp or iproute capture never names its own "
        "host. Without this the name comes from the file name, or from a 'NAME=path' argument."
    ),
)
@click.option(
    "-o",
    "--output",
    type=click.Path(file_okay=False, writable=True, path_type=Path),
    default=Path(),
    show_default="current directory",
    help="Inventory root to write the devices/ and cables/ tree into.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the tree to stdout and write nothing.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite files that are already in the output tree. Without it they are refused.",
)
@click.option(
    "--schema/--no-schema",
    "with_schema",
    default=True,
    show_default=True,
    help=(
        f"Point each generated document at {SCHEMA_FILE_NAME} with a yaml-language-server "
        "modeline, writing the schema when the tree does not already hold one."
    ),
)
@click.option(
    "--exclude",
    "excluded",
    multiple=True,
    metavar="PATTERN",
    help=(
        "Leave out interfaces whose name matches this glob. Applies to 'iproute' captures, "
        "where 'veth*' and 'docker*' are rarely part of a physical topology. Repeatable."
    ),
)
@click.pass_obj
def import_command(
    app: AppContext,
    inputs: tuple[str, ...],
    dialect: str,
    host: str | None,
    output: Path,
    dry_run: bool,
    force: bool,
    with_schema: bool,
    excluded: tuple[str, ...],
) -> None:
    """Bootstrap an inventory from output collected on live devices.

    Reads what an operator already ran — no host is contacted, no credential is
    read — and writes a devices/ and cables/ tree in the layout 'netgraph init'
    produces, commented so it is worth editing rather than regenerating. The
    result is validated straight away; a partly-observed network legitimately
    has findings, and they are the operator's to fill in.

    \b
    netgraph import --from lldp   -o net  captures/*.json
    netgraph import --from iproute --host pc1 -o net  link.json addr.json
    ip -j addr show | netgraph import --host pc1 -o net --dry-run -
    """
    tree_console = app.console()
    report = tree_console.to_stderr() if dry_run else tree_console

    entries = read_inputs(list(inputs), host=host)
    app.log(f"read {_plural(len(entries), 'input')}", level=1)
    draft = build_draft(entries, dialect=dialect, exclude=excluded)
    files = build_files(draft, schema=with_schema)

    _report_import_notes(report, draft)
    if not files:
        report.error(
            "nothing was imported: no device or cable could be built from "
            f"{_plural(len(entries), 'input')}"
        )
        raise click.exceptions.Exit(EXIT_INVALID)

    if dry_run:
        for path, content in files.items():
            tree_console.print(f"# ===== {path} =====")
            tree_console.print(content.rstrip("\n"))
            tree_console.print()
        report.info(
            f"dry run: {_plural(len(files), 'file')} would be written to {output}"
            + (f", plus {SCHEMA_FILE_NAME}" if with_schema else "")
        )
        inventory = load_stream("\n---\n".join(files.values()))
    else:
        if with_schema and not (output / Path(*PurePosixPath(SCHEMA_FILE_NAME).parts)).exists():
            files[SCHEMA_FILE_NAME] = (
                json.dumps(build_schema(), indent=2, ensure_ascii=False) + "\n"
            )
        written = write_files(files, output, force=force)
        report.info(f"wrote {_plural(len(written), 'file')} to {output}:")
        for file in written:
            report.info(f"  {_display_path(file, output)}")
        inventory = load_tree(output)

    report.info("")
    report.info(
        f"imported {_plural(len(draft.devices), 'device')} and "
        f"{_plural(len(draft.cables), 'cable')} from {_plural(len(entries), 'input')}"
    )
    _report_import_validation(report, inventory, root=output)


def _report_import_notes(console: Console, draft: Draft) -> None:
    """Everything an input said that did not become a document.

    Printed before the tree so that "why is this switch not here?" is answered
    on the same screen as the answer to "what did I get?".
    """
    if not draft.notes:
        return
    console.info(f"{_plural(len(draft.notes), 'note')} about what was not imported:")
    for note in draft.notes:
        console.info(f"  {note}")
    console.info("")


def _report_import_validation(console: Console, inventory: Inventory, *, root: Path) -> None:
    """Validate the generated tree and explain which findings are expected.

    An imported inventory is a *partial* one by construction: LLDP shows only
    the ports with a neighbour, ``ip`` shows only one host, and a device nobody
    captured exists solely because a neighbour named it. The validator is right
    to report the resulting gaps, and the operator has to be told that being
    right is not the same as the import having gone wrong — otherwise the first
    experience of the command is a screen of warnings that reads like failure.

    The configuration read is the *output* tree's, not the one the working
    directory happens to sit in: importing into a tree that already ignores a
    rule should not produce a report contradicting its own ``netgraph.toml``.
    Under ``--dry-run`` that directory may not exist yet, which is the one case
    the defaults are used.
    """
    settings = load_config(root).validation if root.is_dir() else ValidationConfig()
    findings = run_validation(inventory, settings)
    console.info("")
    _report_problems(console, inventory.errors, findings)

    expected = sorted({finding.rule for finding in findings} & _EXPECTED_IMPORT_RULES)
    if expected:
        console.info("")
        console.info(
            f"{', '.join(expected)} are expected of an imported tree: a port whose neighbour "
            "was never captured terminates no cable, and a device only a neighbour named has "
            "no configuration of its own. Capture the missing hosts and re-run, or fill the "
            "gaps in by hand — they are not errors in what was imported."
        )
    if inventory.errors or any(finding.severity.is_fatal for finding in findings):
        console.error(
            "the generated tree does not validate; the files were written so you can see "
            "what went wrong, but check them before building on them"
        )
        raise click.exceptions.Exit(EXIT_INVALID)


#: Rules an incomplete capture legitimately trips. Naming them explicitly is
#: what lets the report separate "expected, and yours to fill in" from "netgraph
#: produced something wrong", instead of leaving the reader to guess.
_EXPECTED_IMPORT_RULES: Final[frozenset[str]] = frozenset(
    {"I002", "W101", "W103", "W105", "W109", "W113", "W121"}
)


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


@cli.command("validate")
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Promote every warning to an error, so any finding fails the run.",
)
@click.option(
    "--disable",
    "disabled",
    multiple=True,
    metavar="RULE",
    shell_complete=complete_rule,
    help="Silence a rule by id (E001, NG-C002, ...). Repeatable.",
)
@click.option(
    "-F",
    "--output-format",
    type=click.Choice(REPORT_FORMATS),
    default="text",
    show_default=True,
    help="text is for reading; json, sarif and github are for CI.",
)
@click.pass_obj
def validate_command(
    app: AppContext, strict: bool, disabled: tuple[str, ...], output_format: str
) -> None:
    """Check the inventory for schema and semantic problems.

    Exits 1 when anything is reported as an error, 0 otherwise.

    The structured formats put their document on stdout and move the human
    summary to stderr, so ``netgraph validate -F sarif > report.sarif`` writes a
    file a code-scanning upload accepts while a person watching the run still
    sees what happened. ``--quiet`` drops that summary; it never drops the
    document.
    """
    # Only the structured formats report a line and a column, and paying for
    # them is a measurable amount of retained memory -- so the text path, which
    # locates a finding at its document, does not.
    inventory = app.load(keep_provenance=output_format != "text")
    findings = _run_validation(app, inventory, strict=strict, disabled=disabled)

    if output_format == "text":
        _report_problems(app.console(), inventory.errors, findings)
    else:
        report = build_report(inventory, findings)
        # The summary is commentary once the document is the output, so it goes
        # to stderr through ``info`` -- which is what ``--quiet`` silences.
        _report_problems(app.console(), inventory.errors, findings, commentary=True)
        document = render_report(report, output_format)
        # A clean inventory emits no workflow commands at all; printing the
        # empty string would put a stray blank line in the build log.
        if document:
            app.console().print(document)

    if _is_rejected(inventory, findings):
        raise click.exceptions.Exit(EXIT_INVALID)


def _run_validation(
    app: AppContext,
    inventory: Inventory,
    *,
    strict: bool,
    disabled: Sequence[str] = (),
) -> list[Finding]:
    """Apply the command-line overrides on top of ``netgraph.toml`` and validate."""
    settings = app.config().validation.with_overrides(
        # ``--strict`` may only turn strictness on; the file decides otherwise.
        strict=True if strict else None,
        ignore=disabled,
    )
    findings = run_validation(inventory, settings)
    app.log(f"validation produced {len(findings)} finding(s)", level=1)
    return findings


def _is_rejected(inventory: Inventory, findings: Iterable[Finding]) -> bool:
    """Does anything about this inventory fail the run?

    A load error always does: a document that did not parse is missing from the
    graph entirely, which no amount of severity configuration can make benign.
    """
    return bool(inventory.errors) or any(finding.severity.is_fatal for finding in findings)


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def _resolve_icons(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> IconTheme | None:
    """``--icons``: a built-in theme name, ``none``, or a directory of images.

    Resolving in a callback rather than in the command body means a theme that
    does not exist is reported as what it is — a usage error, with the option
    named and the built-in themes listed — before an inventory is loaded.
    """
    try:
        return icon_theme(value)
    except RenderError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc


def _resolve_link_template(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> LinkTemplate | None:
    """``--link-template``: a URL with ``{file}``, ``{line}`` … in it.

    Validated in a callback, like ``--icons``, so a placeholder that does not
    exist is reported as a usage error — with the known ones listed — before an
    inventory is loaded and long before a diagram full of broken links is
    written to a file.
    """
    if value is None:
        return None
    try:
        return LinkTemplate.parse(value)
    except RenderError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc


#: Which *elements* a rendering covers. Only the commands that draw the whole
#: inventory take these: ``path`` draws the route it traced, so narrowing the
#: graph underneath it would hide the very thing the reader is being shown.
_FILTER_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "--namespace",
        "namespaces",
        multiple=True,
        metavar="NS",
        shell_complete=complete_namespace,
        help="Keep only elements in this namespace or below it. Repeatable.",
    ),
    click.option(
        "--vlan",
        "vlans",
        multiple=True,
        type=click.IntRange(1, 4094),
        metavar="VID",
        help="Keep only elements participating in this VLAN. Repeatable.",
    ),
    click.option(
        "--kind",
        "kinds",
        multiple=True,
        type=click.Choice(NODE_KINDS),
        shell_complete=complete_kind,
        help="Keep only elements of this kind. Repeatable.",
    ),
    click.option(
        "--name",
        "names",
        multiple=True,
        metavar="GLOB",
        help="Keep only elements whose name matches this glob. Repeatable.",
    ),
    click.option(
        "--neighbors-of",
        metavar="NAME",
        default=None,
        shell_complete=complete_node,
        help="Keep only the neighbourhood of this element.",
    ),
    click.option(
        "--depth",
        type=click.IntRange(0),
        default=1,
        show_default=True,
        help="How many hops --neighbors-of reaches.",
    ),
)

#: What a rendering *summarises* rather than draws. Distinct from the filters
#: above: nothing here removes an element from the answer, it only folds several
#: of them into one box or one line, and the box says which ones. Only the
#: commands that draw the whole inventory take these, for the same reason the
#: filters are limited to them.
_AGGREGATE_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "--collapse",
        "collapse",
        multiple=True,
        metavar="NS",
        shell_complete=complete_namespace,
        help=(
            "Replace this namespace and everything under it with one node, labelled with "
            "what it holds. Links crossing the boundary attach to it; links inside it are "
            "counted rather than drawn. Repeatable."
        ),
    ),
    click.option(
        "--collapse-depth",
        type=click.IntRange(1),
        default=None,
        metavar="N",
        help=(
            "Collapse every namespace N levels deep, counted from the shallowest one that "
            "branches: '--collapse-depth 1' is the site-level overview of a tree laid out "
            "as sites/<site>/<tier>."
        ),
    ),
    click.option(
        "--bundle-links/--no-bundle-links",
        "bundle_links",
        default=None,
        help=(
            "Draw parallel links between the same pair of elements as one edge, with the "
            "count in the label. Members of a declared 'lag' interface are bundled either "
            "way unless --no-bundle-links is given, since the inventory already says they "
            "are one logical link."
        ),
    ),
)

#: How much detail a rendering carries. Shared by every command that produces a
#: diagram — ``render``, ``watch`` and ``path --highlight`` — so a highlighted
#: trace is styled by exactly the options a plain render is, and the three
#: cannot drift apart on the first one that gains a new default.
_DISPLAY_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "--show-ips/--no-show-ips",
        default=True,
        show_default=True,
        help="Print configured IP addresses on the nodes.",
    ),
    click.option(
        "--show-vlans/--no-show-vlans",
        default=True,
        show_default=True,
        help="Annotate nodes and links with VLAN membership.",
    ),
    click.option(
        "--group-by-namespace",
        is_flag=True,
        default=False,
        help="Draw each namespace as a visual group.",
    ),
    click.option(
        "--icons",
        callback=_resolve_icons,
        default=None,
        metavar="THEME|DIR",
        help=(
            "Draw each element as an icon instead of a plain shape. "
            f"Built in: {', '.join(theme_choices())}. A directory of images named after "
            "element kinds (router.svg, switch.png, ...) also works. Graphviz formats only."
        ),
    ),
    click.option(
        "--tooltips/--no-tooltips",
        default=True,
        show_default=True,
        help=(
            "Carry the full detail of each element — interfaces, addresses, VLANs, cabling — "
            "as hover text. Reaches a reader in svg output; png and pdf have nowhere to put it."
        ),
    ),
    click.option(
        "--link-template",
        callback=_resolve_link_template,
        default=None,
        metavar="URL",
        help=(
            "Link each element back to the YAML that declares it, e.g. "
            "'https://git.example.com/net/blob/main/{file}#L{line}'. Placeholders: "
            f"{', '.join('{' + field + '}' for field in LINK_FIELDS)}. dot and svg only."
        ),
    ),
    click.option(
        "--element-ids",
        is_flag=True,
        default=False,
        help=(
            "Give every node, edge and namespace a stable id derived from its name, so the "
            "diagram can be deep-linked and styled from outside. dot and svg only."
        ),
    ),
    click.option("--title", default=None, metavar="TEXT", help="Caption for the diagram."),
)

#: Which view of the network to draw. ``path`` does not take it: the layer a
#: trace is answered at is decided by where the path was *found*, and letting a
#: flag contradict that would draw a diagram the report disagrees with.
#:
#: Repeatable, because one output format has somewhere to put a second layer: an
#: HTML page draws each of them and puts a switcher over the set. Every other
#: format holds one view of the network, and asking for two is a usage error
#: rather than a file with two diagrams glued together.
_LAYER_OPTION: Final[Callable[[Any], Any]] = click.option(
    "--layer",
    "layers",
    multiple=True,
    type=click.Choice([layer.value for layer in Layer]),
    default=(Layer.L1.value,),
    show_default=True,
    shell_complete=complete_layer,
    help=(
        "l1 draws the physical topology; l2 annotates it with VLANs; l3 draws IP subnets "
        "and the elements addressed in them. Repeatable for -f html, which draws each layer "
        "and puts a switcher over them."
    ),
)

#: How an inventory with problems in it is treated. Shared by every command
#: that loads one before doing work on it.
_VALIDATION_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option("--strict", is_flag=True, default=False, help="Treat warnings as errors."),
    click.option(
        "--force",
        is_flag=True,
        default=False,
        help="Proceed even when validation failed. The result may not match the files.",
    ),
)

#: Everything ``render`` and ``watch`` have in common, in ``--help`` order.
_GRAPH_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    *_FILTER_OPTIONS,
    *_AGGREGATE_OPTIONS,
    *_DISPLAY_OPTIONS,
    _LAYER_OPTION,
    *_VALIDATION_OPTIONS,
)

_Command = TypeVar("_Command", bound=Callable[..., Any])


def _apply(options: Sequence[Callable[[Any], Any]], command: _Command) -> _Command:
    """Apply ``options`` to ``command``, keeping their written order in ``--help``.

    Decorators are applied bottom-up, so reversing the list reproduces the order
    the options are written in.
    """
    for option in reversed(options):
        command = option(command)
    return command


def _graph_options(command: _Command) -> _Command:
    """Apply :data:`_GRAPH_OPTIONS` to ``command``."""
    return _apply(_GRAPH_OPTIONS, command)


def _path_options(command: _Command) -> _Command:
    """Apply the options ``path --highlight`` shares with ``render``."""
    return _apply((*_DISPLAY_OPTIONS, *_VALIDATION_OPTIONS), command)


def _filter_spec(params: Mapping[str, Any]) -> FilterSpec:
    """Build the element filter from the parsed :data:`_GRAPH_OPTIONS`."""
    return FilterSpec(
        namespaces=tuple(params["namespaces"]),
        vlans=frozenset(params["vlans"]),
        kinds=tuple(params["kinds"]),
        names=tuple(params["names"]),
        neighbors_of=params["neighbors_of"],
        depth=params["depth"],
    )


def _aggregate_spec(params: Mapping[str, Any]) -> AggregateSpec:
    """Build the aggregation from the parsed :data:`_AGGREGATE_OPTIONS`.

    ``--bundle-links/--no-bundle-links`` is tri-state on purpose: unset means
    "fold only what the inventory itself calls one link", which is neither of
    the two things a plain boolean could say. Click leaves ``None`` behind for
    an unset flag pair, and that is what carries the third state.
    """
    bundling = params["bundle_links"]
    return AggregateSpec(
        collapse=tuple(params["collapse"]),
        collapse_depth=params["collapse_depth"],
        bundle=BundleMode.LAG
        if bundling is None
        else (BundleMode.ALL if bundling else BundleMode.NONE),
    )


def _layers(params: Mapping[str, Any], output_format: str) -> tuple[Layer, ...]:
    """The layers to draw, in the order they were asked for, without repeats.

    Raises:
        click.UsageError: More than one layer was asked for in a format that
            holds a single view of the network.
    """
    chosen = tuple(dict.fromkeys(Layer(value) for value in params["layers"])) or (Layer.L1,)
    if len(chosen) > 1 and not supports_layers(output_format):
        holds = ", ".join(name for name in FORMATS if supports_layers(name))
        raise click.UsageError(
            f"--layer was given {len(chosen)} times, but {output_format} output holds one "
            f"layer; render each one to its own file, or use a format that holds several "
            f"({holds})"
        )
    return chosen


def _describe_formats() -> str:
    """One clause per registered format, so ``--help`` enumerates the backends.

    Generated rather than written out: a format added to
    :data:`~netgraph.render.RENDERERS` documents itself here, and cannot drift
    out of step with what ``-f`` accepts.
    """
    return (
        "; ".join(f"{name}: {renderer.description}" for name, renderer in RENDERERS.items()) + "."
    )


def _render_options(
    params: Mapping[str, Any], *, highlight: Highlight | None = None
) -> RenderOptions:
    """Build the display options from the parsed :data:`_DISPLAY_OPTIONS`."""
    return RenderOptions(
        show_ips=params["show_ips"],
        show_vlans=params["show_vlans"],
        group_by_namespace=params["group_by_namespace"],
        title=params["title"],
        icons=params["icons"],
        tooltips=params["tooltips"],
        link_template=params["link_template"],
        element_ids=params["element_ids"],
        highlight=highlight,
    )


@cli.command("render")
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(FORMATS),
    default="dot",
    show_default=True,
    shell_complete=complete_format,
    help=f"Output format. {_describe_formats()}",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write to this file instead of stdout.",
)
@_graph_options
@click.pass_context
def render_command(
    ctx: click.Context,
    /,
    output_format: str,
    output: Path | None,
    **_options: Any,
) -> None:
    """Render the inventory as a network graph.

    Validation always runs first: a diagram drawn from an inventory with a
    dangling cable would misrepresent the network, so errors refuse the render
    unless --force is given.
    """
    app: AppContext = ctx.obj
    params = ctx.params
    strict, force = bool(params["strict"]), bool(params["force"])

    # stdout may be the diagram itself, so every diagnostic goes to stderr.
    console = app.console(err=True)
    inventory = app.load()

    findings = _run_validation(app, inventory, strict=strict)
    if _is_rejected(inventory, findings):
        _report_problems(console, inventory.errors, findings)
        if not force:
            console.error(
                "refusing to render an inventory with errors; fix them, or pass --force "
                "to render anyway"
            )
            raise click.exceptions.Exit(EXIT_INVALID)
        console.warn("rendering despite errors (--force): the diagram may not match the inventory")
    elif findings:
        _report_problems(console, (), findings)

    layers, spec = _layers(params, output_format), _filter_spec(params)
    aggregate = _aggregate_spec(params)
    graphs: list[Graph] = []
    for layer in layers:
        graph = _build_graph(
            app, inventory, layer=layer, spec=spec, aggregate=aggregate, console=console
        )
        where = f" at layer {layer}" if len(layers) > 1 else ""
        for problem in graph.dangling:
            console.warn(f"dropped from the graph{where}: {problem}")
        if graph.is_empty:
            console.warn(_empty_graph_reason(layer, spec))
        _report_advisories(console, output_format, nodes=len(graph.nodes), edges=len(graph.edges))
        graphs.append(graph)

    options = _render_options(params)
    _report_icon_support(console, output_format, options)
    _report_interaction_support(ctx, console, output_format, options)
    payload = render_layers(graphs, output_format, options)
    _write_output(console, payload, output=output, output_format=output_format)
    drawn = ", ".join(str(layer) for layer in layers)
    console.info(
        f"rendered {sum(len(graph.nodes) for graph in graphs)} node(s) and "
        f"{sum(len(graph.edges) for graph in graphs)} edge(s) as {output_format} "
        f"at layer {drawn}" + (f" to {output}" if output is not None else "")
    )


def _empty_graph_reason(layer: Layer, spec: FilterSpec) -> str:
    """Why there is nothing to draw, which is not always the filters.

    At layer 3 an inventory with no routable address produces an empty graph on
    its own, and so does an overlay view of an inventory with no tunnel;
    blaming filters that were never given would send the reader looking in the
    wrong place.
    """
    if not spec.is_empty:
        return "the filters selected no elements; the output will be an empty graph"
    if layer is Layer.L3:
        return (
            "nothing to draw at layer 3: no element carries a routable address. "
            "Loopback and link-local addresses are excluded; run 'netgraph list subnets' "
            "to see what the inventory is addressed in"
        )
    if layer is Layer.OVERLAY:
        return (
            "nothing to draw in the overlay view: the inventory declares no tunnel. "
            "Render '--layer l1' for the physical topology, or add a 'tunnel' document"
        )
    # No filter and nothing at layer 1 means the tree itself is empty, which is
    # what a freshly scaffolded 'netgraph init --minimal' looks like.
    return "the inventory declares no elements; the output will be an empty graph"


def _report_advisories(console: Console, output_format: str, *, nodes: int, edges: int) -> None:
    """Warn about anything the chosen backend has to say at this graph size.

    Mermaid's edge ceiling is the only such limit today, but the CLI does not
    know that: it asks the registry, so a backend added later warns without a
    line changing here. The output is correct in every case — what is being
    reported is that a *consumer* will refuse it.
    """
    for advisory in advisories_for(output_format, nodes=nodes, edges=edges):
        console.warn(advisory)


def _report_icon_support(console: Console, output_format: str, options: RenderOptions) -> None:
    """Say so when a theme was asked for that this format cannot draw.

    Silently ignoring ``--icons`` would leave the user staring at a diagram
    wondering which of the two they got wrong.
    """
    if options.icons is None or supports_icons(output_format):
        return
    drawn = ", ".join(name for name in FORMATS if supports_icons(name))
    console.warn(
        f"--icons is ignored for {output_format} output, which has no picture to put an "
        f"icon in; the formats that draw icons are {drawn}"
    )


def _report_interaction_support(
    ctx: click.Context, console: Console, output_format: str, options: RenderOptions
) -> None:
    """Say so when a diagram was asked to carry more than this format can.

    Only what the user actually *typed* is reported. ``--tooltips`` is on by
    default, so warning that a PNG has no tooltips on every raster render would
    train the reader to ignore the warnings that matter.
    """
    if supports_interaction(output_format):
        return
    asked = [
        option
        for option, wanted, parameter in (
            ("--tooltips", options.tooltips, "tooltips"),
            ("--link-template", options.link_template is not None, "link_template"),
            ("--element-ids", options.element_ids, "element_ids"),
        )
        if wanted and ctx.get_parameter_source(parameter) is ParameterSource.COMMANDLINE
    ]
    if not asked:
        return
    carried = ", ".join(name for name in FORMATS if supports_interaction(name))
    console.warn(
        f"{', '.join(asked)} {'is' if len(asked) == 1 else 'are'} ignored for {output_format} "
        f"output, which carries no tooltips, links or element ids; "
        f"the formats that do are {carried}"
    )


def _build_graph(
    app: AppContext,
    inventory: Inventory,
    *,
    layer: Layer,
    spec: FilterSpec,
    aggregate: AggregateSpec | None = None,
    console: Console | None = None,
) -> Graph:
    """Build, filter and summarise the graph.

    The three run in that order and only that order: filtering decides what
    exists, and aggregation folds what is left. Doing it the other way round
    would let ``--kind switch`` empty a collapsed node of everything it claims
    to stand for.

    Raises:
        click.BadParameter: ``--neighbors-of`` names no element.
    """
    graph = build_graph(inventory, layer=layer)
    if not spec.is_empty:
        app.log(f"applying filters: {spec.describe()}", level=1)
    try:
        filtered = filter_graph(graph, spec)
    except UnknownElementError as exc:
        hint = (
            f" Did you mean one of: {', '.join(exc.candidates)}?"
            if exc.candidates
            else " Run 'netgraph list devices' to see what is declared."
        )
        raise click.BadParameter(
            f"no element named {exc.name!r} in this inventory.{hint}",
            param_hint="'--neighbors-of'",
        ) from exc

    if aggregate is not None and not aggregate.is_empty:
        app.log(f"aggregating: {aggregate.describe()}", level=1)
        if console is not None and aggregate.collapses:
            _report_collapse(console, filtered, aggregate)
        filtered = aggregate_graph(filtered, aggregate)

    app.log(
        f"graph has {len(filtered.nodes)} node(s) and {len(filtered.edges)} edge(s)",
        level=1,
    )
    return filtered


def _report_collapse(console: Console, graph: Graph, spec: AggregateSpec) -> None:
    """Say so when a collapse folded nothing.

    A ``--collapse`` that matched no element is silent otherwise, and the
    resulting diagram is indistinguishable from one where the flag worked and
    the namespace was small — so a typo would cost a reader the whole point of
    the flag without ever saying so.
    """
    targets = collapse_targets(graph, spec)
    folded = {
        target
        for target in targets
        for node in graph.nodes.values()
        if node.namespace == target or node.namespace.startswith(f"{target}/")
    }
    if not folded:
        console.warn(
            "nothing was collapsed: no element sits in the namespace(s) asked for. "
            "Run 'netgraph list devices' to see the fully-qualified names"
            if spec.collapse
            else f"nothing was collapsed: no namespace is {spec.collapse_depth} level(s) "
            "below the shallowest one that branches"
        )
        return
    for named in spec.collapse:
        candidate = named.strip("/")
        if candidate and candidate not in folded:
            console.warn(
                f"--collapse {named!r} folded nothing: no element sits in it, or an "
                "enclosing namespace was collapsed instead"
            )


def _write_output(
    console: Console, payload: bytes, *, output: Path | None, output_format: str
) -> None:
    """Write the rendering to a file, or to stdout when no file was named.

    Raises:
        RenderError: The destination cannot be written, or the format is binary
            and stdout is a terminal.
    """
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
        except OSError as exc:
            raise RenderError(f"cannot write {output}: {exc.strerror or exc}") from exc
        return

    stream = click.get_binary_stream("stdout")
    if is_binary_format(output_format) and _is_a_terminal(stream):
        raise RenderError(
            f"refusing to write binary {output_format} data to the terminal; "
            f"use '--output FILE' or redirect stdout"
        )
    try:
        stream.write(payload)
        stream.flush()
    except BrokenPipeError:  # pragma: no cover - depends on the consuming process
        raise
    except OSError as exc:
        raise RenderError(f"cannot write to stdout: {exc.strerror or exc}") from exc


def _is_a_terminal(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - detached stream
        return False


# --------------------------------------------------------------------------- #
# path
# --------------------------------------------------------------------------- #

#: Formats ``--highlight`` can draw. Emphasis is a *visual* weight — a bold
#: outline, a dimmed fill — so only the backends that lay a picture out can
#: carry it; Mermaid and JSON have no such vocabulary and are left out rather
#: than silently ignoring the flag.
HIGHLIGHT_FORMATS: Final[tuple[str, ...]] = tuple(
    name for name in FORMATS if supports_highlight(name)
)


@cli.command("path")
@click.argument("src", shell_complete=complete_element)
@click.argument("dst", shell_complete=complete_element)
@click.option(
    "--vlan",
    type=click.IntRange(1, 4094),
    default=None,
    metavar="VID",
    help=(
        "Trace inside this VLAN instead of letting the trace derive one. Forces a layer-2 "
        "answer: a VLAN is a layer-2 fact, so no routed path is looked for."
    ),
)
@click.option(
    "--all",
    "all_paths",
    is_flag=True,
    default=False,
    help="Report every distinct path, not only the shortest. A redundant pair is the point.",
)
@click.option(
    "--max-hops",
    type=click.IntRange(1, 64),
    default=DEFAULT_MAX_HOPS,
    show_default=True,
    help="Abandon a route that crosses more links than this.",
)
@click.option(
    "-F",
    "--output-format",
    type=click.Choice(TRACE_FORMATS),
    default="text",
    show_default=True,
    help="text is the hop-by-hop report; json is the same trace for tooling.",
)
@click.option(
    "--highlight",
    is_flag=True,
    default=False,
    help=(
        "Also render the whole inventory with the traced path emphasised and everything else "
        "dimmed. Choose the format with -f and the destination with -o."
    ),
)
@click.option(
    "-f",
    "--format",
    "output_image",
    type=click.Choice(HIGHLIGHT_FORMATS),
    default="dot",
    show_default=True,
    shell_complete=complete_format,
    help="Format of the --highlight diagram.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the --highlight diagram to this file instead of stdout.",
)
@_path_options
@click.pass_context
def path_command(
    ctx: click.Context,
    /,
    src: str,
    dst: str,
    vlan: int | None,
    all_paths: bool,
    max_hops: int,
    output_format: str,
    highlight: bool,
    output_image: str,
    output: Path | None,
    **_options: Any,
) -> None:
    """Trace how SRC reaches DST, and what the traffic crosses on the way.

    SRC and DST are each an element name, an 'element:interface' selector, or an
    IP address configured somewhere in the inventory — an address is usually
    what a ticket or a packet capture gives you.

    \b
    netgraph path pc-north-01 srv-south-01
    netgraph path 10.1.10.51 10.2.20.11 --all
    netgraph path sw-hq:Ethernet49/1 sw-hq:Ethernet50/1 --vlan 10
    netgraph path rtr-hq rtr-branch-b --highlight -f svg -o path.svg

    The trace is layer-aware. It first walks the physical topology through
    cables, hubs, adapters and switches, honouring VLAN membership — a route
    that would have to cross an access port in another VLAN is not a route — and
    reports the VLAN it assumed. When the two ends are in no common broadcast
    domain it looks for a routed path instead, matching interface subnets across
    the elements that forward, and reports each hop's ingress and egress
    interface and address. A tunnel on the path is named with the encapsulation
    entered and left, nesting included, and a tunnel nothing in its 'over' chain
    encrypts is called out the way W127 calls it out.

    Exits 1 when there is no path, so a reachability assertion drops straight
    into CI.
    """
    app: AppContext = ctx.obj
    params = ctx.params
    _reject_diagram_options_without_highlight(ctx, highlight)

    # With --highlight and no --output the diagram owns stdout, so the report
    # moves to stderr; ``render`` splits its output the same way.
    diagram_to_stdout = highlight and output is None
    console = app.console(err=diagram_to_stdout)
    notes = app.console(err=True)

    inventory = app.load()
    findings = _run_validation(app, inventory, strict=bool(params["strict"]))
    if _is_rejected(inventory, findings):
        _report_problems(notes, inventory.errors, findings)
        if not params["force"]:
            notes.error(
                "refusing to trace an inventory with errors; a dangling cable is exactly the "
                "kind of thing that makes a path wrong. Fix them, or pass --force"
            )
            raise click.exceptions.Exit(EXIT_INVALID)
        notes.warn("tracing despite errors (--force): the path may not match the inventory")

    try:
        result = trace(inventory, src, dst, vlan=vlan, max_hops=max_hops)
    except TraceError as exc:
        raise click.BadParameter(str(exc), param_hint="'SRC' / 'DST'") from exc

    console.print(render_trace(result, output_format, all_paths=all_paths).rstrip("\n"))
    _report_cleartext_tunnels(notes, result)

    if highlight:
        _write_highlight(
            notes,
            inventory,
            result,
            params,
            all_paths=all_paths,
            output_format=output_image,
            output=output,
        )
    if not result.found:
        raise click.exceptions.Exit(EXIT_INVALID)


def _reject_diagram_options_without_highlight(ctx: click.Context, highlight: bool) -> None:
    """Fail on ``-f``/``-o`` without ``--highlight`` instead of ignoring them.

    Both name properties of a diagram this command only draws when asked to. A
    user who typed ``-o path.svg`` and got the text report on stdout and no file
    would reasonably conclude the command is broken.
    """
    if highlight:
        return
    given = [
        f"--{name.replace('_', '-')}"
        for name, flag in (("format", "output_image"), ("output", "output"))
        if ctx.get_parameter_source(flag) is ParameterSource.COMMANDLINE
    ]
    if given:
        raise click.UsageError(
            f"{given[0]} describes the diagram --highlight draws; add --highlight to draw one.",
            ctx=ctx,
        )


def _report_cleartext_tunnels(console: Console, result: TraceResult) -> None:
    """Say when the traced route crosses a tunnel that protects nothing.

    ``W127`` reports this about an *inventory*; here it is reported about a
    *route*, which is the form that matters — a cleartext VXLAN inside a data
    centre is fine, and the same tunnel on the path between two branch offices
    is not, and only a trace can tell the two apart.
    """
    for view in result.cleartext_tunnels:
        console.warn(
            f"the path crosses tunnel {view.fqn!r}, which is {view.type} and encrypts nothing, "
            f"and no tunnel in its 'over' chain does either; everything it carries crosses the "
            f"underlay in the clear (W127)"
        )


def _write_highlight(
    console: Console,
    inventory: Inventory,
    result: TraceResult,
    params: Mapping[str, Any],
    *,
    all_paths: bool,
    output_format: str,
    output: Path | None,
) -> None:
    """Draw the whole inventory with the traced route emphasised.

    The diagram is built at the layer the path was *found* at, so a switched
    answer is drawn over cables and a routed one over prefixes — the two views
    of the same trace, and neither of them a picture the report disagrees with.
    A trace that found nothing still draws: the diagram is then the topology the
    path was looked for in, dimmed, which is where the reader has to look.
    """
    layer = result.layer if result.layer is not None else Layer.L1
    graph = build_graph(inventory, layer=layer)
    options = _render_options(params, highlight=result.highlight(all_paths=all_paths))
    _report_icon_support(console, output_format, options)
    payload = render(graph, output_format, options)
    _write_output(console, payload, output=output, output_format=output_format)
    console.info(
        f"highlighted {_plural(len(result.selected(all_paths=all_paths)), 'path')} over "
        f"{_plural(len(graph.nodes), 'node')} at layer {layer}"
        + (f", written to {output}" if output is not None else "")
    )


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #


@cli.command("watch")
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(FORMATS),
    default="svg",
    show_default=True,
    shell_complete=complete_format,
    help="Output format. Defaults to svg, which is what a live preview wants.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Rewrite this file after every successful render.",
)
@_graph_options
@click.option(
    "--serve",
    is_flag=True,
    default=False,
    help="Also host the render over HTTP, on a page that reloads itself.",
)
@click.option(
    "--host",
    default=DEFAULT_HOST,
    show_default=True,
    metavar="ADDRESS",
    help=(
        "Address --serve binds to. The default keeps the preview on this machine; "
        "an inventory describes internal topology, so publishing it is an explicit act."
    ),
)
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=DEFAULT_PORT,
    show_default=True,
    help="Port --serve binds to. 0 lets the operating system choose one.",
)
@click.option(
    "--debounce",
    type=click.IntRange(0, 60_000),
    default=DEFAULT_DEBOUNCE_MS,
    show_default=True,
    metavar="MS",
    help="How long a burst of filesystem events is collected before re-rendering.",
)
@click.pass_context
def watch_command(
    ctx: click.Context,
    /,
    output_format: str,
    output: Path | None,
    serve: bool,
    host: str,
    port: int,
    debounce: int,
    **_options: Any,
) -> None:
    """Re-render the inventory whenever a file in it changes.

    Every change triggers the same load, validate and render that `netgraph
    render` performs, and prints a timestamped status line. A cycle that fails
    is reported but changes nothing: the file written by --output and the page
    served by --serve keep showing the last render that succeeded, so a
    half-typed document never blanks the diagram you are working from.

    Press Ctrl-C to stop.
    """
    app: AppContext = ctx.obj
    params = ctx.params
    console = app.console(err=True)

    _reject_serve_options_without_serve(ctx, serve)

    options = _render_options(params)
    _report_icon_support(console, output_format, options)
    _report_interaction_support(ctx, console, output_format, options)
    request = RenderRequest(
        inventory=app.inventory,
        output_format=output_format,
        layers=_layers(params, output_format),
        spec=_filter_spec(params),
        aggregate=_aggregate_spec(params),
        options=options,
        strict=bool(params["strict"]),
        force=bool(params["force"]),
    )
    live = LiveRender(output_format)

    root = app.inventory if app.inventory.is_dir() else app.inventory.parent
    changes = file_changes(
        [root],
        watch_filter=InventoryFilter(
            root=root,
            # Writing the diagram into the tree being watched would otherwise
            # make every render trigger the next one, forever.
            ignore=[output] if output is not None else [],
            only=[app.inventory] if app.inventory.is_file() else [],
        ),
        debounce_ms=debounce,
    )

    preview = _start_preview(
        app, console, live, serve=serve, host=host, port=port, title=str(app.inventory)
    )
    if preview is None and output is None:
        console.warn("neither --output nor --serve was given: each render is checked and discarded")
    console.info(
        f"watching {root} for changes, rendering as {output_format}"
        + (f" to {output}" if output is not None else "")
        + "; press Ctrl-C to stop"
    )

    try:
        run_watch(
            request,
            changes,
            output=output,
            live=live,
            on_result=lambda result: _report_cycle(
                app, console, result, live, output=output, output_format=output_format
            ),
            on_change=lambda batch: app.log(f"changed: {', '.join(batch)}", level=1),
        )
    except KeyboardInterrupt:
        # Ctrl-C is how this command is meant to end, not a failure.
        pass
    finally:
        if preview is not None:
            preview.stop()
    console.info("watch stopped")


def _reject_serve_options_without_serve(ctx: click.Context, serve: bool) -> None:
    """Fail on ``--host``/``--port`` without ``--serve`` instead of ignoring them.

    Silently accepting ``--host 0.0.0.0`` while serving nothing would teach a
    user that the flag works, and the next run — the one with ``--serve`` — is
    not the moment to discover it never did.
    """
    if serve:
        return
    given = [
        name
        for name in ("host", "port")
        if ctx.get_parameter_source(name) is click.core.ParameterSource.COMMANDLINE
    ]
    if given:
        raise click.UsageError(
            f"--{given[0]} only applies to the preview server; add --serve to start one.",
            ctx=ctx,
        )


def _start_preview(
    app: AppContext,
    console: Console,
    live: LiveRender,
    *,
    serve: bool,
    host: str,
    port: int,
    title: str,
) -> PreviewServer | None:
    """Start the HTTP preview, or return ``None`` when it was not asked for.

    Raises:
        ServeError: The address is already in use or cannot be bound.
    """
    if not serve:
        return None
    exposure = describe_exposure(host)
    if exposure is not None:
        console.warn(exposure)
    preview = PreviewServer.create(
        live,
        title=title,
        host=host,
        port=port,
        log=lambda message: app.log(f"preview: {message}", level=2),
    ).start()
    console.info(f"preview at {preview.url}")
    return preview


def _report_cycle(
    app: AppContext,
    console: Console,
    result: CycleResult,
    live: LiveRender,
    *,
    output: Path | None,
    output_format: str,
) -> None:
    """Print one status line, then whatever the cycle found."""
    snapshot = live.snapshot()
    stamp = console.dim(snapshot.stamp)
    label = console.style(f"{result.status!s:<7}", fg=_STATUS_COLOUR[result.status], bold=True)

    detail = result.message
    if result.status.is_ok and output is not None:
        detail = f"{detail} → {output}"
    elif not result.status.is_ok and snapshot.stale:
        detail = f"{detail}; keeping the render from before"

    line = f"{stamp}  {label}  {detail} {console.dim(f'({result.duration * 1000:.0f} ms)')}"
    if result.status.is_ok and app.quiet:
        return
    console.print(line)

    if result.problems:
        _report_problems(console, result.errors, result.findings)
    for dangling in result.dangling:
        console.warn(f"dropped from the graph: {dangling}")
    if result.status.is_ok:
        _report_advisories(console, output_format, nodes=result.nodes, edges=result.edges)


#: Colour per cycle status, matching the severity palette used for findings.
_STATUS_COLOUR: Final[dict[Status, str]] = {
    Status.PENDING: "cyan",
    Status.OK: "green",
    Status.INVALID: "red",
    Status.FAILED: "red",
}


# --------------------------------------------------------------------------- #
# web
# --------------------------------------------------------------------------- #


@cli.command("web")
@click.argument(
    "source",
    required=False,
    type=click.Path(exists=True, dir_okay=True, readable=True, path_type=Path),
)
@click.option(
    "--host",
    default=DEFAULT_HOST,
    show_default=True,
    metavar="ADDRESS",
    help=(
        "Address to bind. The default keeps the interface on this machine; "
        "an inventory describes internal topology, so publishing it is an explicit act."
    ),
)
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=WEB_PORT,
    show_default=True,
    help="Port to bind. 0 lets the operating system choose one.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    show_default=True,
    help="Open the interface in the default browser once it is listening.",
)
@click.option(
    "--icons",
    callback=_resolve_icons,
    default=None,
    metavar="THEME|DIR",
    help=(
        "Draw each element as an icon instead of a plain shape. "
        f"Built in: {', '.join(theme_choices())}. Chosen here rather than in the browser, "
        "because it names a directory on this machine."
    ),
)
@click.pass_obj
def web_command(
    app: AppContext,
    source: Path | None,
    host: str,
    port: int,
    open_browser: bool,
    icons: IconTheme | None,
) -> None:
    """Edit a YAML document stream in a browser and see it drawn as you type.

    The page holds the stream in a text area and the diagram beside it. Every
    edit is parsed, validated and rendered exactly as `netgraph render` would,
    and hovering a node or a link opens an info box with the detail the picture
    has no room for: every interface, its addresses and VLANs, and what it is
    cabled to.

    SOURCE seeds the editor. It may be a file or a folder, whose documents are
    concatenated into one stream; `-` reads the stream from standard input, as
    does a pipe. With no SOURCE the editor opens on the same example topology
    `netgraph init` writes.

    Note that a stream has no folders and therefore no namespaces: every element
    seeded from a tree lands in the root namespace.

    Press Ctrl-C to stop.
    """
    console = app.console(err=True)

    text = _web_source(console, source)
    exposure = describe_exposure(host, subject="the web interface")
    if exposure is not None:
        console.warn(exposure)
    if shutil.which(DOT_EXECUTABLE) is None:
        console.warn(
            f"the Graphviz {DOT_EXECUTABLE!r} executable was not found on PATH; the page "
            "will load but every render will report that it cannot draw anything"
        )

    server = WebServer.create(
        source=text,
        icons=icons,
        host=host,
        port=port,
        log=lambda message: app.log(f"web: {message}", level=2),
        on_render=lambda preview: app.log(
            f"{preview.status}: {preview.message} ({preview.duration * 1000:.0f} ms)", level=1
        ),
    ).start()
    console.info(f"editing at {server.url}; press Ctrl-C to stop")
    if open_browser:
        _open_browser(app, server.url)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        # Ctrl-C is how this command is meant to end, not a failure.
        pass
    finally:
        server.stop()
    console.info("web interface stopped")


def _web_source(console: Console, source: Path | None) -> str:
    """The document stream the editor opens with.

    A pipe wins over everything: ``netgraph render -f dot | ...`` taught users
    that netgraph reads stdin when it is not a terminal, and a stream is what
    this command edits.

    Raises:
        LoaderError: A seed folder cannot be walked.
    """
    if source is None and not _is_a_terminal(sys.stdin):
        return sys.stdin.read()
    if source is None:
        return _example_stream()
    if str(source) == "-":  # pragma: no cover - click resolves '-' to a path first
        return sys.stdin.read()
    if source.is_file():
        return source.read_text(encoding="utf-8-sig")

    files = iter_inventory_files(source)
    if any(entry.namespace for entry in files):
        console.warn(
            "a document stream has no folders: every element from a nested one is seeded "
            "into the root namespace, and two elements that shared a name in different "
            "folders now collide"
        )
    documents = [
        f"# {entry.relative.as_posix()}\n{entry.path.read_text(encoding='utf-8-sig').lstrip()}"
        for entry in files
    ]
    return "\n---\n".join(documents) if documents else _example_stream()


def _example_stream() -> str:
    """The starter topology, as one stream.

    The same documents ``netgraph init`` writes, so the first thing a reader of
    either sees is the same network — without the schema modeline, which points
    at a file the browser has no way to fetch.
    """
    scaffold = build_scaffold(schema=False)
    documents = [
        body for path, body in scaffold.files.items() if path.lower().endswith(YAML_SUFFIXES)
    ]
    return "\n---\n".join(documents)


def _open_browser(app: AppContext, url: str) -> None:
    """Ask the desktop to open ``url``, and say so rather than failing if it cannot.

    There is no browser on a server, in a container or over a bare SSH session,
    and none of those is a reason for this command to stop: the URL has already
    been printed and a tunnel is a normal way to reach it.
    """
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # pragma: no cover - platform-specific failure
        app.log(f"could not open a browser: {exc}", level=1)
        return
    if not opened:
        app.log("no browser could be opened; the address above still works", level=1)


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


@cli.command("list")
@click.argument(
    "what",
    type=click.Choice(["devices", "cables", "tunnels", "vlans", "subnets"]),
    default="devices",
    required=False,
)
@click.option(
    "-F",
    "--output-format",
    type=click.Choice(["table", "json", "yaml"]),
    default="table",
    show_default=True,
    help="table is for reading; json and yaml are for piping.",
)
@click.pass_obj
def list_command(app: AppContext, what: str, output_format: str) -> None:
    """List the devices, cables, tunnels, VLANs or subnets an inventory declares."""
    console = app.console()
    inventory = app.load()
    _warn_about_load_errors(console, inventory)

    headers, aligns, rows, records = _LISTINGS[what](inventory)
    if output_format == "table":
        console.table(headers, rows, aligns=aligns, empty=f"no {what} declared")
    else:
        console.print(_serialise(records, output_format).rstrip("\n"))


#: One listing: column headers, alignment, table rows, and the same data as
#: records for the machine-readable formats.
_Listing = tuple[
    tuple[str, ...],
    tuple[Align, ...],
    list[list[str]],
    list[dict[str, Any]],
]


def _list_devices(inventory: Inventory) -> _Listing:
    graph = build_graph(inventory)
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for node in graph.nodes.values():
        # The table has room for one address, so it shows the one that says
        # where the element sits: every host also has 127.0.0.1 and ::1.
        addresses = node.routable_addresses
        rows.append(
            [
                node.fqn,
                node.kind,
                str(len(node.ports)),
                addresses[0] if addresses else "-",
                compact_ids(node.vlans) or "-",
            ]
        )
        records.append(
            {
                "name": node.fqn,
                "shortName": node.name,
                "kind": node.kind,
                "namespace": node.namespace,
                "interfaces": len(node.ports),
                "addresses": list(addresses),
                "vlans": sorted(node.vlans),
                "source": str(inventory.source_of(node.fqn) or ""),
            }
        )
    headers = ("NAME", "KIND", "PORTS", "ADDRESS", "VLANS")
    aligns: tuple[Align, ...] = ("left", "left", "right", "left", "left")
    return headers, aligns, rows, records


def _list_cables(inventory: Inventory) -> _Listing:
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for fqn, cable in inventory.cables.items():
        left, right = cable.endpoints
        speed = format_bitrate(cable.spec.speed) if cable.spec.speed is not None else "-"
        rows.append(
            [
                fqn,
                cable.spec.medium.value,
                speed,
                str(left),
                str(right),
                _length(cable.spec.length_m),
            ]
        )
        records.append(
            {
                "name": fqn,
                "medium": cable.spec.medium.value,
                "speed": cable.spec.speed,
                "duplex": cable.spec.duplex.value,
                "endpoints": [str(left), str(right)],
                "lengthM": cable.spec.length_m,
                "label": cable.spec.label,
                "source": str(inventory.source_of(fqn) or ""),
            }
        )
    headers = ("NAME", "MEDIUM", "SPEED", "A END", "B END", "LENGTH")
    aligns: tuple[Align, ...] = ("left", "left", "right", "left", "left", "right")
    return headers, aligns, rows, records


def _list_tunnels(inventory: Inventory) -> _Listing:
    """Every tunnel, with its encapsulation stack and what protects it.

    The stack comes from :func:`~netgraph.render.graph.resolve_tunnels`, the
    same resolution ``render --layer overlay`` draws, so the listing and the
    diagram cannot disagree about what runs inside what. A tunnel whose
    endpoints do not resolve is still listed — the reader is most likely running
    this command *because* something is wrong — with its stack left at its own
    type.
    """
    views = {view.fqn: view for view in resolve_tunnels(inventory)[0]}
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for fqn, tunnel in inventory.tunnels.items():
        spec = tunnel.spec
        view = views.get(fqn)
        stack = view.stack_text if view is not None else spec.type.value
        protection = "yes" if tunnel.encrypts else ("underlay" if view and view.protected else "no")
        rows.append(
            [
                fqn,
                stack,
                str(spec.vni) if spec.vni is not None else "-",
                protection,
                str(len(spec.endpoints)),
                ", ".join(str(ref) for ref in spec.endpoints),
            ]
        )
        records.append(
            {
                "name": fqn,
                "type": spec.type.value,
                "stack": list(view.stack) if view is not None else [spec.type.value],
                "layer": spec.type.layer,
                "over": view.over if view is not None else spec.over,
                "vni": spec.vni,
                "encrypted": tunnel.encrypts,
                "protected": view.protected if view is not None else tunnel.encrypts,
                "transport": spec.type.transport.value,
                "port": spec.port,
                "mtu": spec.mtu,
                "endpoints": [str(ref) for ref in spec.endpoints],
                "source": str(inventory.source_of(fqn) or ""),
            }
        )
    headers = ("NAME", "STACK", "VNI", "ENCRYPTED", "ENDS", "ENDPOINTS")
    aligns: tuple[Align, ...] = ("left", "left", "right", "left", "right", "left")
    return headers, aligns, rows, records


def _list_vlans(inventory: Inventory) -> _Listing:
    """Every VLAN, with the elements that participate in it.

    Membership comes from the graph, so a host on an untagged access port counts
    as a member of that VLAN even though it declares no ``vlan`` block itself.
    """
    graph = build_graph(inventory)
    names: dict[int, str] = {}
    for device in inventory.devices.values():
        for definition in device.spec.vlans:
            if definition.name and definition.id not in names:
                names[definition.id] = definition.name

    members: dict[int, list[str]] = {}
    ports: dict[int, int] = {}
    for node in graph.nodes.values():
        for vlan in node.vlans:
            members.setdefault(vlan, []).append(node.fqn)
        for port in node.ports:
            for vlan in port.vlans:
                ports[vlan] = ports.get(vlan, 0) + 1

    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for vlan in sorted(members):
        elements = members[vlan]
        rows.append([str(vlan), names.get(vlan, "-"), str(len(elements)), str(ports.get(vlan, 0))])
        records.append(
            {
                "id": vlan,
                "name": names.get(vlan),
                "elements": elements,
                "interfaces": ports.get(vlan, 0),
            }
        )
    headers = ("VLAN", "NAME", "ELEMENTS", "PORTS")
    aligns: tuple[Align, ...] = ("right", "left", "right", "right")
    return headers, aligns, rows, records


def _list_subnets(inventory: Inventory) -> _Listing:
    """Every prefix an address sits in, with the elements holding one.

    The grouping is :func:`~netgraph.subnets.subnets_of`, the same one
    ``render --layer l3`` draws and the same one ``W105``/``W106`` are about, so
    the listing and the diagram cannot disagree. Loopback and link-local
    prefixes are left out there: they are scoped to a single host or a single
    link, so listing ``127.0.0.0/8`` once per machine would say nothing about
    the addressing plan this command exists to show.
    """
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for subnet in subnets_of(inventory):
        vlans = sorted(subnet.vlans)
        rows.append(
            [
                subnet.prefix,
                str(subnet.version),
                str(len(subnet.addresses)),
                str(len(subnet.elements)),
                compact_ids(vlans) or "-",
            ]
        )
        records.append(
            {
                "subnet": subnet.prefix,
                "family": subnet.family,
                "addresses": list(subnet.addresses),
                "elements": list(subnet.elements),
                "vlans": vlans,
            }
        )
    headers = ("SUBNET", "IP", "ADDRESSES", "ELEMENTS", "VLANS")
    aligns: tuple[Align, ...] = ("left", "right", "right", "right", "left")
    return headers, aligns, rows, records


_LISTINGS: Final[dict[str, Any]] = {
    "devices": _list_devices,
    "cables": _list_cables,
    "tunnels": _list_tunnels,
    "vlans": _list_vlans,
    "subnets": _list_subnets,
}


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #


@cli.command("show")
@click.argument("name", shell_complete=complete_element)
@click.option(
    "-F",
    "--output-format",
    type=click.Choice(["yaml", "json"]),
    default="yaml",
    show_default=True,
    help="Serialisation of the resolved document.",
)
@click.option(
    "--raw",
    "--no-expand",
    "raw",
    is_flag=True,
    help="Print the document as written: ranges unexpanded, 'from' unmerged.",
)
@click.pass_obj
def show_command(app: AppContext, name: str, output_format: str, raw: bool) -> None:
    """Print the fully resolved configuration of one element.

    NAME is a fully-qualified name (``sites/hq/sw1``) or a short name that is
    unique in the inventory. Defaults are materialised and values normalised, so
    the output is what netgraph actually works with rather than what was typed.

    ``--raw`` (``--no-expand``) prints the document exactly as it stands in the
    file instead: an ``interfaces[].range`` still a range, a ``spec.from`` still
    a reference. Diffing the two outputs is how a template merge is inspected.
    """
    console = app.console()
    inventory = app.load()
    _warn_about_load_errors(console, inventory)

    fqn, element = _resolve_element(inventory, name)
    source = inventory.source_of(fqn)
    app.log(f"{fqn} declared in {source}" if source else f"{fqn}", level=1)

    if raw:
        document = _as_written(inventory, fqn)
    else:
        document = element.model_dump(mode="json", by_alias=True, exclude_none=True)
    console.print(_serialise(document, output_format).rstrip("\n"))


def _as_written(inventory: Inventory, fqn: str) -> Any:
    """Re-read one element's document from disk, without any loader rewriting.

    The inventory keeps elements, not the text they came from — holding every
    parsed document alive for the sake of a command that prints one of them
    would cost every other command memory. So the one file is read again. Only
    the requested document is returned, so a syntax error introduced elsewhere
    in the same file since the load cannot be silently absorbed either: it is
    raised, exactly as ``netgraph validate`` would raise it.

    Raises:
        click.BadParameter: The element has no source on disk.
    """
    source = inventory.source_of(fqn)
    if source is None:  # pragma: no cover - every indexed element has a source
        raise click.BadParameter(
            f"{fqn!r} was not loaded from a file, so there is nothing to show raw",
            param_hint="'--raw'",
        )
    for document in read_documents(source.path, relative=PurePosixPath(source.relative)):
        if document.index == source.index:
            return document.data
    raise click.BadParameter(  # pragma: no cover - the file changed under us
        f"{source.relative} no longer holds a document {source.index}; reload the inventory",
        param_hint="'--raw'",
    )


def _resolve_element(inventory: Inventory, name: str) -> tuple[str, Element]:
    """Resolve ``name`` to exactly one element.

    Raises:
        click.BadParameter: The name matches nothing, or is ambiguous.
    """
    resolution = inventory.lookup(name)
    if resolution.element is not None and resolution.fqn is not None:
        return resolution.fqn, resolution.element

    if resolution.ambiguous:
        candidates = ", ".join(resolution.ambiguous)
        raise click.BadParameter(
            f"{name!r} is ambiguous; it matches {candidates}. Use the fully-qualified name.",
            param_hint="'NAME'",
        )
    raise click.BadParameter(
        f"no element named {name!r} in {inventory.root}. "
        f"Run 'netgraph list devices' to see what is declared.",
        param_hint="'NAME'",
    )


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #


@cli.command("rules")
@click.pass_obj
def rules_command(app: AppContext) -> None:
    """List the validation rules, their severity and their schema aliases."""
    console = app.console()
    rows = [
        [rule.id, str(rule.severity), ", ".join(rule.aliases) or "-", rule.summary]
        for rule in RULES
    ]
    console.table(("RULE", "SEVERITY", "ALIASES", "SUMMARY"), rows)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #


@cli.command("schema")
@click.option(
    "-k",
    "--kind",
    type=click.Choice(DOCUMENT_KINDS),
    default=None,
    shell_complete=complete_kind,
    help="Emit the schema for a single document kind instead of all of them.",
)
@click.option(
    "--all",
    "emit_all",
    is_flag=True,
    default=False,
    help="Emit one schema covering every kind, discriminated on 'kind'. The default.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write to this file instead of stdout.",
)
def schema_command(kind: str | None, emit_all: bool, output: Path | None) -> None:
    """Print the JSON Schema (2020-12) for netgraph documents.

    Point an editor at it and a typo'd key is underlined as you type it rather
    than when you next run 'netgraph validate'; docs/schema.md has the
    yaml-language-server modeline and the VS Code settings block.

    The default covers every kind in one schema, so a single 'yaml.schemas'
    entry matching a glob is enough for a whole inventory tree.
    """
    if emit_all and kind is not None:
        raise click.UsageError("--all and --kind are mutually exclusive.")

    document = json.dumps(build_schema(kind), indent=2, ensure_ascii=False) + "\n"
    if output is None:
        click.echo(document, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")


# --------------------------------------------------------------------------- #
# completion
# --------------------------------------------------------------------------- #


@cli.command("completion")
@click.argument("shell", type=click.Choice(SHELLS))
def completion_command(shell: str) -> None:
    """Print the shell completion script for bash, zsh or fish.

    \b
    bash:  netgraph completion bash > ~/.local/share/bash-completion/completions/netgraph
    zsh:   netgraph completion zsh  > ~/.zfunc/_netgraph   # with ~/.zfunc on $fpath
    fish:  netgraph completion fish > ~/.config/fish/completions/netgraph.fish

    Then start a new shell. Beyond the commands and flags, completion is
    inventory-aware: 'netgraph show <TAB>' and '--neighbors-of <TAB>' offer the
    elements of the tree named by -i, and '--disable <TAB>' the validation
    rules.
    """
    click.echo(completion_script(shell, cli), nl=False)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Problem:
    """A load error or a finding, flattened into one reportable shape."""

    severity: Severity
    location: str
    rule: str
    message: str

    @classmethod
    def from_load_error(cls, error: LoadError) -> _Problem:
        message = error.message
        if error.field_path:
            message = f"{format_path(error.field_path)}: {message}"
        return cls(
            severity=Severity.ERROR,
            location=error.location,
            # Schema violations carry an ``NG-*`` id when the model supplied
            # one; a syntax or I/O problem has no rule to name.
            rule=error.rule or "load",
            message=message,
        )

    @classmethod
    def from_finding(cls, finding: Finding) -> _Problem:
        return cls(
            severity=finding.severity,
            location=finding.location,
            rule=finding.rule,
            message=finding.message,
        )


def _report_problems(
    console: Console,
    errors: Iterable[LoadError],
    findings: Iterable[Finding],
    *,
    commentary: bool = False,
) -> None:
    """Print load errors and findings grouped by severity, most severe first.

    Args:
        commentary: Write the report to stderr as commentary rather than to
            stdout as data, which is what the structured output formats need:
            stdout is theirs, and ``--quiet`` then silences the human half
            without touching the document.
    """
    write = console.info if commentary else console.print
    problems = [_Problem.from_load_error(error) for error in errors]
    problems.extend(_Problem.from_finding(finding) for finding in findings)
    if not problems:
        write(console.style("no problems found", fg="green"))
        return

    for severity in sorted(Severity, key=lambda value: value.rank):
        group = [problem for problem in problems if problem.severity is severity]
        if not group:
            continue
        colour = _SEVERITY_COLOUR[severity]
        heading = f"{severity}s ({len(group)}):"
        write(console.style(heading, fg=colour, bold=True))
        for line in _problem_lines(console, group, colour):
            write(line)
        write()

    write(_summary(console, problems))


def _problem_lines(console: Console, problems: Sequence[_Problem], colour: str) -> Iterator[str]:
    """``  file.yaml#0:12  E001  message``, with the first two columns aligned."""
    location_width = max(len(problem.location) for problem in problems)
    rule_width = max(len(problem.rule) for problem in problems)
    for problem in problems:
        location = console.dim(problem.location.ljust(location_width))
        rule = console.style(problem.rule.ljust(rule_width), fg=colour)
        yield f"  {location}  {rule}  {problem.message}"


def _summary(console: Console, problems: Sequence[_Problem]) -> str:
    counts = {
        severity: sum(1 for problem in problems if problem.severity is severity)
        for severity in Severity
    }
    parts = [
        _plural(count, str(severity))
        for severity, count in sorted(counts.items(), key=lambda item: item[0].rank)
        if count
    ]
    text = ", ".join(parts)
    fatal = counts[Severity.ERROR] > 0
    return console.style(text, fg="red" if fatal else "yellow", bold=True)


def _warn_about_load_errors(console: Console, inventory: Inventory) -> None:
    """Note unreadable documents on stderr without failing a read-only command.

    ``list`` and ``show`` answer questions about what *did* load; refusing to
    answer because an unrelated file is broken would be unhelpful. Silence would
    be worse, though — the answer is incomplete and the user must know.
    """
    count = len(inventory.errors)
    if count:
        console.warn(
            f"{_plural(count, 'document')} could not be loaded and {'is' if count == 1 else 'are'} "
            f"missing from this output; run 'netgraph validate' for details"
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _serialise(payload: Any, output_format: str) -> str:
    """Dump ``payload`` as JSON or YAML, keeping key order."""
    if output_format == "json":
        return json.dumps(payload, indent=2, ensure_ascii=False)
    return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _length(metres: float | None) -> str:
    if metres is None:
        return "-"
    return f"{int(metres)}m" if float(metres).is_integer() else f"{metres}m"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point.

    Translates :class:`~netgraph.errors.NetgraphError` into a concise stderr
    message plus the exit code carried by the exception, so tracebacks are
    reserved for genuine bugs.
    """
    try:
        # ``standalone_mode=False`` keeps click from calling ``sys.exit`` itself,
        # which would bypass the error translation below. It also makes click
        # *return* the status a command asked for instead of raising it.
        result = cli.main(args=argv, standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 130
    except NetgraphError as exc:
        click.echo(f"error: {exc}", err=True)
        return exc.exit_code
    except BrokenPipeError:  # pragma: no cover - depends on the consuming process
        # Mirror the shell convention for a downstream reader closing the pipe.
        sys.stderr.close()
        return 141
    return result if isinstance(result, int) else 0


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m netgraph``
    raise SystemExit(main())
