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
``drift``
    The same captures, read the other way round: the inventory becomes an
    assertion about the live network and the command reports where reality
    disagrees. What a dialect cannot see is reported as *unobserved* rather than
    as a deletion, so a partial capture never reads as the network having been
    dismantled.
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
``export``
    The inventory as something other tools consume: an ``/etc/hosts`` fragment,
    a DNS zone, an Ansible inventory, Prometheus targets, a cabling pull-list.
    Each is deterministic and text-diffable, is scoped by the same filters a
    render takes, and reports what it could not represent as a JSON manifest on
    stderr rather than dropping it in silence.
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

import csv
import io
import itertools
import json
import shutil
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, TypeVar

import click
import click.core
import yaml
from click.core import ParameterSource

from netgraph import __version__
from netgraph.completion import (
    SHELLS,
    complete_element,
    complete_export_format,
    complete_format,
    complete_kind,
    complete_layer,
    complete_namespace,
    complete_node,
    complete_profile,
    complete_rule,
    completion_script,
)
from netgraph.config import CONFIG_FILE_NAME, Config, ValidationConfig, load_config
from netgraph.console import Align, Console
from netgraph.drift import FORMATS as DRIFT_FORMATS
from netgraph.drift import CompareSpec, DriftReport, check_drift, render_drift
from netgraph.drift import write_text as write_drift
from netgraph.errors import LoaderError, NetgraphError, RenderError, compact_ids, format_path
from netgraph.export import (
    EXPORTERS,
    ExportContext,
    ExportOptions,
    ExportResult,
    domain_name,
    export,
    is_assignable_label,
    layers_for,
)
from netgraph.export import FORMATS as EXPORT_FORMATS
from netgraph.importer import (
    DIALECTS,
    Draft,
    build_draft,
    build_files,
    read_inputs,
    write_files,
)
from netgraph.ipam import (
    DEFAULT_SIZE,
    Utilisation,
    allocations_within,
    format_capacity,
    format_utilisation,
    free_space,
    next_free,
    parse_prefix,
    parse_size,
    usable_addresses,
)
from netgraph.ipam import Report as IpamReport
from netgraph.ipam import build_report as build_ipam_report
from netgraph.loader import (
    YAML_SUFFIXES,
    Inventory,
    LoadError,
    iter_inventory_files,
    load_stream,
    load_tree,
    read_documents,
)
from netgraph.models import DOCUMENT_KINDS, Adapter, Device, Element, format_bitrate
from netgraph.render import (
    DEFAULT_RANKDIR,
    FORMATS,
    LINK_FIELDS,
    NODE_KINDS,
    RANKDIRS,
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
    draws_racks,
    filter_graph,
    icon_theme,
    is_binary_format,
    rack_formats,
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
from netgraph.report import Diagnostic, build_report, render_report
from netgraph.rules import RULES, Severity
from netgraph.scaffold import SCHEMA_FILE_NAME, build_scaffold, write_scaffold
from netgraph.schema import build_schema
from netgraph.settings import (
    RENDER_TABLE,
    Origin,
    Resolution,
    resolve_settings,
)
from netgraph.subnets import IPNetwork, subnets_of
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

if TYPE_CHECKING:
    # ``netgraph.fmt`` pulls in ruamel.yaml, which costs roughly 30 ms of import
    # time -- an eighth of what starting the CLI costs at all. Only ``fmt``
    # needs it, and ``validate`` runs in a pre-commit hook, so the import is
    # made where it is used (see ``fmt_command``) and only the names needed for
    # annotations are taken here.
    from netgraph.fmt import Mode, Summary

__all__ = ["cli", "main"]

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
    "max_content_width": 100,
}

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
    #: ``netgraph.toml``, read at most once per run. A command may ask for it
    #: twice — once to resolve its render defaults, once to grade findings — and
    #: reading the file twice could answer the two questions differently if it
    #: were edited in between.
    _config: Config | None = field(default=None, repr=False)

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
        if self._config is not None:
            return self._config
        # ``load_config`` reads a *file* argument as TOML directly, so a
        # single-file inventory must be redirected to its directory.
        root = self.inventory if self.inventory.is_dir() else self.inventory.parent
        config = load_config(root)
        if config.path is not None:
            self.log(f"using configuration {config.path}", level=1)
        self._config = config
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
# drift
# --------------------------------------------------------------------------- #

#: What ``drift --fail-on`` accepts, the gating value first.
FAIL_ON: Final[tuple[str, ...]] = ("drift", "none")


@cli.command("drift")
@click.argument("inputs", nargs=-1, metavar="[NAME=]INPUT...")
@click.option(
    "--from",
    "dialect",
    type=click.Choice(DIALECTS),
    default="auto",
    show_default=True,
    help=(
        "Input dialect, as for 'netgraph import'. 'auto' sniffs each input on its own: "
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
    "--only",
    "only",
    multiple=True,
    metavar="GLOB",
    shell_complete=complete_element,
    help=(
        "Compare only elements whose fully-qualified or short name matches this glob. Repeatable."
    ),
)
@click.option(
    "--exclude",
    "excluded",
    multiple=True,
    metavar="GLOB",
    shell_complete=complete_element,
    help="Leave elements whose name matches this glob out of the comparison. Repeatable.",
)
@click.option(
    "--exclude-interface",
    "excluded_interfaces",
    multiple=True,
    metavar="PATTERN",
    help=(
        "Leave out interfaces whose name matches this glob, as 'netgraph import --exclude' "
        "does. A declared interface it matches can never be reported as missing. Repeatable."
    ),
)
@click.option(
    "-F",
    "--output-format",
    type=click.Choice(DRIFT_FORMATS),
    default="text",
    show_default=True,
    help="text is for reading; json is for a script, junit for a CI test report.",
)
@click.option(
    "--fail-on",
    "fail_on",
    type=click.Choice(FAIL_ON),
    default="drift",
    show_default=True,
    help=(
        "Exit 1 when the network disagrees with the inventory, or never. An unobserved "
        "field is not a disagreement and never fails the run."
    ),
)
@click.pass_obj
def drift_command(
    app: AppContext,
    inputs: tuple[str, ...],
    dialect: str,
    host: str | None,
    only: tuple[str, ...],
    excluded: tuple[str, ...],
    excluded_interfaces: tuple[str, ...],
    output_format: str,
    fail_on: str,
) -> None:
    """Compare a live network against what the inventory declares.

    Reads the same captures 'netgraph import' does — no host is contacted and no
    credential is read — and reports, per element, what the network has that the
    inventory does not, what the inventory declares that the network lacks, and
    what the two spell differently.

    A field the capture cannot see is reported as unobserved rather than as a
    deletion, and never counts as drift, so a partial capture is safe to check
    against a complete inventory.

    \b
    netgraph -i net drift --from lldp captures/*.json
    ip -j addr show | netgraph -i net drift --host pc1 -
    netgraph -i net drift --fail-on drift -F junit caps/* > drift.xml
    """
    inventory = app.load()
    console = app.console()
    report_stream = console.to_stderr() if output_format != "text" else console

    if inventory.errors:
        _report_problems(console.to_stderr(), inventory.errors, ())
        console.to_stderr().error(
            "refusing to compare against an inventory that does not load; a document that was "
            "rejected is absent from the comparison, which would read as drift"
        )
        raise click.exceptions.Exit(EXIT_INVALID)

    report = check_drift(
        inventory,
        list(inputs),
        dialect=dialect,
        host=host,
        spec=CompareSpec(only=only, exclude=excluded, ignore_interfaces=excluded_interfaces),
    )
    app.log(
        f"compared {len(report.compared)} declared element(s) against "
        f"{len(report.observed)} observed device(s)",
        level=1,
    )

    if output_format == "text":
        write_drift(console, report)
    else:
        console.print(render_drift(report, output_format))
        report_stream.info(_drift_summary(report))

    if report.drifted and fail_on == "drift":
        raise click.exceptions.Exit(EXIT_INVALID)


def _drift_summary(report: DriftReport) -> str:
    """The one-line commentary printed beside a structured document."""
    if not report.drifted:
        return (
            f"no drift: {_plural(len(report.compared), 'element')} compared, "
            f"{_plural(len(report.unobserved), 'unobserved item')}"
        )
    return (
        f"{_plural(len(report.changes), 'difference')} between the inventory and the "
        f"capture, {_plural(len(report.unobserved), 'unobserved item')}"
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
# fmt
# --------------------------------------------------------------------------- #


@cli.command("fmt")
@click.argument(
    "paths",
    nargs=-1,
    # Deliberately unvalidated by click: ``exists=True`` would reject the ``-``
    # that means stdin before this command ever saw it, and a path that is
    # missing is better reported by the loader, which says what it looked for.
    type=click.Path(path_type=Path),
)
@click.option(
    "--check",
    "check",
    is_flag=True,
    default=False,
    help="Write nothing; exit 1 listing the files that are not canonical.",
)
@click.option(
    "--diff",
    "show_diff",
    is_flag=True,
    default=False,
    help="Write nothing; print a unified diff of what would change.",
)
@click.option(
    "--stdin",
    "use_stdin",
    is_flag=True,
    default=False,
    help="Format the YAML stream on stdin and write it to stdout. Same as the path '-'.",
)
@click.pass_obj
def fmt_command(
    app: AppContext,
    paths: tuple[Path, ...],
    check: bool,
    show_diff: bool,
    use_stdin: bool,
) -> None:
    """Rewrite inventory YAML in its canonical form.

    With no PATHS, formats the inventory ``-i`` points at. A PATH may be a
    folder to walk or a single YAML file; discovery is the loader's, so
    ``.netgraphignore`` and the dot- and underscore-prefix rules apply exactly
    as they do to ``validate``.

    ``docs/format.md`` defines the form. Formatting never changes what a
    document means: every file is read back with the strict loader and compared
    against what it said before, and a file that does not survive that check is
    left alone.

    Exits 1 when ``--check`` finds a file that is not canonical, or when any
    file could not be formatted.
    """
    from netgraph.fmt import Mode, format_paths

    if check and show_diff:
        raise click.UsageError("--check and --diff cannot be combined; pick one.")
    console = app.console()

    if use_stdin or (len(paths) == 1 and str(paths[0]) == "-"):
        _format_stdin(console)
        return

    mode = Mode.CHECK if check else Mode.DIFF if show_diff else Mode.WRITE
    roots = paths or (app.inventory,)
    app.log(f"formatting {len(roots)} path(s) in {mode.value} mode", level=1)
    summary = format_paths(roots, mode=mode)
    _report_formatting(console, summary, mode=mode)

    if summary.rejects(mode):
        raise click.exceptions.Exit(EXIT_INVALID)


def _format_stdin(console: Console) -> None:
    """Format the stream on stdin onto stdout.

    Nothing is summarised and nothing goes to stderr on success: the output is
    the whole point, and a caller has almost certainly redirected it.

    Raises:
        LoaderError: The stream is not well-formed YAML.
    """
    from netgraph.fmt import STDIN_NAME, FormatSyntaxError, format_source

    try:
        formatted = format_source(sys.stdin.read(), name=STDIN_NAME)
    except FormatSyntaxError as exc:
        raise LoaderError(str(exc)) from exc
    # ``print`` rather than ``echo``-per-line: the canonical form already ends
    # in exactly one newline, and click would add a second.
    click.echo(formatted, nl=False, color=console.color)


def _report_formatting(console: Console, summary: Summary, *, mode: Mode) -> None:
    """Say what happened to each file, in the register the mode calls for.

    Only what a pipe would want goes to stdout — the diff in ``--diff``, the
    file list in ``--check``. Everything else is commentary on stderr, so
    ``netgraph fmt --diff | git apply`` and ``netgraph fmt --check | xargs``
    both work.
    """
    from netgraph.fmt import Mode

    for problem in summary.discovery_errors:
        console.warn(str(problem))

    if mode is Mode.DIFF:
        for result in summary.changed:
            click.echo(result.diff, nl=False, color=console.color)
    elif mode is Mode.CHECK:
        for result in summary.changed:
            console.print(result.display)

    for result in summary.failures:
        console.error(f"{result.display}: {result.error}")

    changed = len(summary.changed)
    unchanged = len(summary.unchanged)
    verb = "reformatted" if mode is Mode.WRITE else "would be reformatted"
    parts = [f"{changed} file(s) {verb}", f"{unchanged} already formatted"]
    if summary.failures:
        parts.append(f"{len(summary.failures)} failed")
    console.info(", ".join(parts))


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
    click.option(
        "--max-addresses",
        type=click.IntRange(0),
        default=4,
        show_default=True,
        metavar="N",
        help=(
            "Longest address list spelled out under a node before it is abbreviated to "
            "'and N more'. 0 prints the count alone."
        ),
    ),
    click.option(
        "--rankdir",
        type=click.Choice(RANKDIRS, case_sensitive=False),
        default=None,
        show_default=f"{DEFAULT_RANKDIR}, top to bottom",
        help=(
            "Layout direction. A wide network reads better left to right; a deep one "
            "top to bottom. Honoured by the Graphviz backends and by mermaid."
        ),
    ),
    click.option("--title", default=None, metavar="TEXT", help="Caption for the diagram."),
)

#: How the file decides what the flags do not. Shared by every command that
#: reads render defaults out of ``netgraph.toml``; see :mod:`netgraph.settings`
#: for the precedence ladder these two select and report on.
_CONFIG_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "--profile",
        "profile",
        default=None,
        metavar="NAME",
        shell_complete=complete_profile,
        help=(
            f"Apply the [profile.NAME] block of {CONFIG_FILE_NAME} on top of its "
            f"[{RENDER_TABLE}] table. Explicit flags still win over both."
        ),
    ),
    click.option(
        "--show-config",
        is_flag=True,
        default=False,
        help=(
            "Print the settings this invocation resolves to, and where each one came "
            "from, then exit without doing any work."
        ),
    ),
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
        "and the elements addressed in them; overlay draws the tunnels; routing draws the "
        "BGP sessions and OSPF adjacencies, clustered by VRF; physical adds the patch panels "
        "l1 splices out; rack draws a front elevation per rack. Repeatable for -f html, "
        "which draws each layer and puts a switcher over them."
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
    *_CONFIG_OPTIONS,
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


def _config_options(command: _Command) -> _Command:
    """Apply :data:`_CONFIG_OPTIONS` to ``command``."""
    return _apply(_CONFIG_OPTIONS, command)


def _path_options(command: _Command) -> _Command:
    """Apply the options ``path --highlight`` shares with ``render``."""
    return _apply((*_DISPLAY_OPTIONS, *_VALIDATION_OPTIONS, *_CONFIG_OPTIONS), command)


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
    if Layer.RACK in chosen and not draws_racks(output_format):
        # Caught here rather than in the backend so the reader gets a usage
        # error naming the alternatives, before an inventory is even loaded.
        raise click.UsageError(
            f"--layer rack draws a front elevation — one row per rack unit, empty units "
            f"included — and {output_format} output has no way to express one; render it as "
            f"{', '.join(rack_formats())}"
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
        max_addresses=params["max_addresses"],
        icons=params["icons"],
        tooltips=params["tooltips"],
        link_template=params["link_template"],
        element_ids=params["element_ids"],
        rankdir=params["rankdir"],
        highlight=highlight,
    )


# --------------------------------------------------------------------------- #
# netgraph.toml: render defaults and named profiles
# --------------------------------------------------------------------------- #


def _explicit(ctx: click.Context) -> frozenset[str]:
    """The parameters the user actually supplied on this command line.

    Click stores a parsed value whether or not one was typed, so
    ``ctx.params["depth"] == 1`` cannot distinguish an absent ``--depth`` from
    an explicit ``--depth 1``. The parameter *source* can, and it is the only
    thing that can, which is why the top rung of the precedence ladder is built
    from it rather than by comparing values against defaults: a user who types
    the default value still beats the file.

    An environment variable counts as explicit for the same reason a flag does —
    the user put it there — while Click's own default and any default map do
    not.
    """
    supplied = (ParameterSource.COMMANDLINE, ParameterSource.ENVIRONMENT, ParameterSource.PROMPT)
    return frozenset(name for name in ctx.params if ctx.get_parameter_source(name) in supplied)


def _apply_settings(ctx: click.Context) -> tuple[Resolution, ...]:
    """Fold the inventory's render defaults into ``ctx.params``.

    Everything downstream — :func:`_render_options`, :func:`_filter_spec`,
    :func:`_layers` — keeps reading ``ctx.params`` and cannot tell whether a
    value was typed or configured, which is the point: there is one place where
    precedence is decided and no command can implement it differently.

    Returns:
        One :class:`~netgraph.settings.Resolution` per setting the command
        takes, for ``--show-config``.

    Raises:
        ConfigurationError: The file is unusable, or ``--profile`` names a
            block it does not declare.
    """
    app: AppContext = ctx.obj
    config = app.config()
    profile = config.profile(ctx.params.get("profile"))
    resolutions = resolve_settings(
        params=ctx.params,
        given=_explicit(ctx),
        render=config.render,
        profile=profile,
        path=config.path,
    )
    for resolution in resolutions:
        ctx.params[resolution.setting.param] = resolution.value
    configured = [item for item in resolutions if item.origin in (Origin.FILE, Origin.PROFILE)]
    if configured:
        app.log(
            f"applied {_plural(len(configured), 'setting')} from {config.path}"
            + (f" via profile {profile.name}" if profile is not None else ""),
            level=1,
        )
    return resolutions


def _settings_for(
    command: click.Command, config: Config, *, profile: str | None
) -> tuple[Resolution, ...]:
    """Resolve ``command``'s settings without running it, for ``config show``.

    No flags are in play here, so the report shows what the *file* does to a
    bare invocation: the top rung of the ladder is simply unoccupied.
    """
    # Parsed rather than read off the parameters: a repeatable option's default
    # only becomes the empty tuple the command body sees once Click has
    # processed it, and ``path`` has required arguments that an empty command
    # line does not supply — which resilient parsing is exactly for.
    context = click.Context(command, resilient_parsing=True)
    command.parse_args(context, [])
    return resolve_settings(
        params=context.params,
        given=(),
        render=config.render,
        profile=config.profile(profile),
        path=config.path,
    )


def _print_settings(
    console: Console, resolutions: Sequence[Resolution], *, config: Config, command: str
) -> None:
    """The resolved settings of one command, with a provenance column."""
    console.info(f"settings for 'netgraph {command}'")
    console.info(
        f"configuration: {config.path}"
        if config.path is not None
        else f"configuration: none ({CONFIG_FILE_NAME} not found; built-in defaults in use)"
    )
    if config.profiles:
        console.info(f"profiles declared: {', '.join(config.profile_names)}")
    console.print()
    console.table(
        ("SETTING", "VALUE", "SOURCE"),
        [[item.setting.key, item.display, item.source] for item in resolutions],
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

    # stdout may be the diagram itself, so every diagnostic goes to stderr.
    console = app.console(err=True)
    resolutions = _apply_settings(ctx)
    if params["show_config"]:
        _print_settings(app.console(), resolutions, config=app.config(), command="render")
        return
    output_format = params["output_format"]
    strict, force = bool(params["strict"]), bool(params["force"])
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
    _write_output(
        payload, output=output, binary=is_binary_format(output_format), what=output_format
    )
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
    if layer is Layer.ROUTING:
        return (
            "nothing to draw in the routing view: no element declares 'spec.routing', "
            "'spec.routes' or 'spec.vrfs'. Render '--layer l3' for the addressing the "
            "inventory does declare, or add a 'routing' block to a router"
        )
    if layer is Layer.RACK:
        return (
            "nothing to draw at layer rack: no element declares 'metadata.location' with a "
            "'rack' and a 'position'. Add one to place it on an elevation"
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
    payload: bytes, *, output: Path | None, binary: bool = False, what: str = ""
) -> None:
    """Write an artefact to a file, or to stdout when no file was named.

    Shared by ``render`` and ``export``, which differ only in whether the
    payload can be binary: a PNG printed to a terminal is a wrecked session,
    and everything :mod:`netgraph.export` emits is text by construction. The
    caller says which it has rather than the function asking a renderer
    registry, so a format that is not in that registry does not have to rely on
    the registry answering "not binary" for things it has never heard of.

    Args:
        binary: Would this payload wreck a terminal? Only then is stdout
            checked for being one.
        what: The format name, for the error message when it is.

    Raises:
        RenderError: The destination cannot be written, or the format is binary
            and stdout is a terminal.
    """
    if output is not None:
        _write_file(payload, output)
        return

    stream = click.get_binary_stream("stdout")
    if binary and _is_a_terminal(stream):
        raise RenderError(
            f"refusing to write binary {what} data to the terminal; "
            f"use '--output FILE' or redirect stdout"
        )
    try:
        stream.write(payload)
        stream.flush()
    except BrokenPipeError:  # pragma: no cover - depends on the consuming process
        raise
    except OSError as exc:
        raise RenderError(f"cannot write to stdout: {exc.strerror or exc}") from exc


def _write_file(payload: bytes, output: Path) -> None:
    """Write ``payload`` to ``output``, creating the directories above it.

    Shared by every command that takes ``--output``: a rendering, an exported
    artefact and an export manifest all fail the same way when the destination
    is unwritable, and should say so in the same words.

    Raises:
        RenderError: The destination cannot be written.
    """
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
    except OSError as exc:
        raise RenderError(f"cannot write {output}: {exc.strerror or exc}") from exc


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
    "report_format",
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
    report_format: str,
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
    resolutions = _apply_settings(ctx)
    if params["show_config"]:
        _print_settings(app.console(), resolutions, config=app.config(), command="path")
        return

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

    console.print(render_trace(result, report_format, all_paths=all_paths).rstrip("\n"))
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
    _write_output(
        payload, output=output, binary=is_binary_format(output_format), what=output_format
    )
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
    resolutions = _apply_settings(ctx)
    if params["show_config"]:
        _print_settings(console, resolutions, config=app.config(), command="watch")
        return
    output_format = params["output_format"]

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
@_config_options
@click.pass_context
def web_command(
    ctx: click.Context,
    /,
    source: Path | None,
    host: str,
    port: int,
    open_browser: bool,
    icons: IconTheme | None,
    profile: str | None,
    show_config: bool,
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

    Render defaults and --profile are read from the netgraph.toml of the
    inventory named by -i, the current directory by default. The stream being
    edited has no folder of its own to look in, so that file decides how this
    machine draws rather than what this text means.

    Press Ctrl-C to stop.
    """
    app: AppContext = ctx.obj
    console = app.console(err=True)

    resolutions = _apply_settings(ctx)
    if show_config:
        _print_settings(app.console(), resolutions, config=app.config(), command="web")
        return
    icons = ctx.params["icons"]

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
    type=click.Choice(["devices", "cables", "tunnels", "vlans", "bss", "subnets"]),
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
    """List the devices, cables, tunnels, VLANs, BSSs or subnets of an inventory."""
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


def _list_bss(inventory: Inventory) -> _Listing:
    """Every BSS the inventory declares: the wireless side of ``list vlans``.

    One row per SSID per radio, because that is the unit an operator works with
    — a dual-band access point serving three networks has six of them, and each
    has its own BSSID, its own VLAN and possibly its own security. Client radios
    are listed too, with their role, so that "who is on the guest network?" is a
    question the listing can answer.
    """
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    owners: Iterable[tuple[str, Device | Adapter]] = itertools.chain(
        inventory.devices.items(), inventory.adapters.items()
    )
    for fqn, owner in owners:
        for interface in owner.interfaces:
            wireless = interface.wireless
            if wireless is None:
                continue
            for entry in wireless.bss:
                rows.append(
                    [
                        entry.ssid + (" (hidden)" if entry.hidden else ""),
                        f"{fqn}:{interface.name}",
                        wireless.role.value,
                        wireless.channel_text or "-",
                        entry.bssid or "-",
                        str(entry.vlan) if entry.vlan is not None else "-",
                        entry.security.value if entry.security is not None else "-",
                    ]
                )
                records.append(
                    {
                        "ssid": entry.ssid,
                        "element": fqn,
                        "interface": interface.name,
                        "role": wireless.role.value,
                        "band": wireless.band.value if wireless.band is not None else None,
                        "channel": wireless.channel,
                        "widthMhz": wireless.width_mhz,
                        "txPowerDbm": wireless.tx_power_dbm,
                        "bssid": entry.bssid,
                        "vlan": entry.vlan,
                        "security": entry.security.value if entry.security is not None else None,
                        "hidden": entry.hidden,
                        "source": str(inventory.source_of(fqn) or ""),
                    }
                )
    headers = ("SSID", "RADIO", "ROLE", "CHANNEL", "BSSID", "VLAN", "SECURITY")
    aligns: tuple[Align, ...] = ("left", "left", "left", "left", "left", "right", "left")
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

    A ``VRF`` column appears only when something is in one (§16.1). Two routing
    instances may hold the same prefix, and without the column the two rows would
    be indistinguishable; adding it unconditionally would put an empty column in
    front of every inventory that has no VRF, which is nearly all of them.
    """
    subnets = subnets_of(inventory)
    partitioned = any(subnet.vrf for subnet in subnets)
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for subnet in subnets:
        vlans = sorted(subnet.vlans)
        rows.append(
            [
                *([subnet.vrf or "-"] if partitioned else []),
                subnet.prefix,
                str(subnet.version),
                str(len(subnet.addresses)),
                str(len(subnet.elements)),
                compact_ids(vlans) or "-",
            ]
        )
        record: dict[str, Any] = {
            "subnet": subnet.prefix,
            "family": subnet.family,
            "addresses": list(subnet.addresses),
            "elements": list(subnet.elements),
            "vlans": vlans,
        }
        if subnet.vrf:
            record["vrf"] = subnet.vrf
        records.append(record)
    headers = (
        *(("VRF",) if partitioned else ()),
        "SUBNET",
        "IP",
        "ADDRESSES",
        "ELEMENTS",
        "VLANS",
    )
    aligns: tuple[Align, ...] = (
        *(("left",) if partitioned else ()),
        "left",
        "right",
        "right",
        "right",
        "left",
    )
    return headers, aligns, rows, records


_LISTINGS: Final[dict[str, Any]] = {
    "devices": _list_devices,
    "cables": _list_cables,
    "tunnels": _list_tunnels,
    "vlans": _list_vlans,
    "bss": _list_bss,
    "subnets": _list_subnets,
}


# --------------------------------------------------------------------------- #
# ipam
# --------------------------------------------------------------------------- #

#: Output formats of ``netgraph ipam``. ``table`` reads; the other two pipe.
IPAM_FORMATS: Final[tuple[str, ...]] = ("table", "json", "csv")

#: Utilisation bands and the colour each is printed in. Read in order, so the
#: first band a value falls into wins. Thresholds are the ones a capacity plan
#: is usually reviewed against: past 80 % a prefix needs a decision, past 95 %
#: it needs one today.
_UTILISATION_COLOURS: Final[tuple[tuple[float, str], ...]] = ((95.0, "red"), (80.0, "yellow"))


@cli.command("ipam")
@click.option(
    "--free",
    "free_prefix",
    metavar="PREFIX",
    default=None,
    help="List the unallocated CIDR blocks inside PREFIX instead of the utilisation table.",
)
@click.option(
    "--next-free",
    "next_free_prefix",
    metavar="PREFIX",
    default=None,
    help="Print the first free block inside PREFIX, and nothing else.",
)
@click.option(
    "--size",
    "size",
    metavar="LENGTH",
    default=None,
    help="Prefix length --next-free should look for, as '24' or '/24'. "
    "[default: /24 for IPv4, /64 for IPv6]",
)
@click.option(
    "--aggregate",
    "aggregated",
    is_flag=True,
    help="Collapse sibling prefixes that fill their supernet into one row.",
)
@click.option(
    "--conflicts",
    "conflicts_only",
    is_flag=True,
    help="Report only the address-plan conflicts, without the utilisation table.",
)
@click.option(
    "--family",
    type=click.Choice(("all", "ipv4", "ipv6")),
    default="all",
    show_default=True,
    help="Restrict the utilisation table to one address family.",
)
@click.option(
    "-F",
    "--format",
    "--output-format",
    "output_format",
    type=click.Choice(IPAM_FORMATS),
    default="table",
    show_default=True,
    help="table is for reading; json and csv are for scripting.",
)
@click.pass_obj
def ipam_command(
    app: AppContext,
    free_prefix: str | None,
    next_free_prefix: str | None,
    size: str | None,
    aggregated: bool,
    conflicts_only: bool,
    family: str,
    output_format: str,
) -> None:
    """Report subnet utilisation, free address space and address-plan conflicts.

    With no options this prints how full every prefix in the inventory is,
    followed by the conflicts :mod:`netgraph.validate` finds in the address
    plan — the same findings, at the same severities, that ``netgraph validate``
    reports, filtered to the rules that are about addressing.

    ``--free`` and ``--next-free`` answer the question an engineer actually has
    when adding a device: what is left, and where does the next block start.

    Exits 1 when a conflict is reported as an error, or when ``--next-free``
    finds no room.
    """
    console = app.console()
    _reject_conflicting_ipam_options(
        free_prefix, next_free_prefix, size, aggregated, conflicts_only, family
    )
    inventory = app.load()
    _warn_about_load_errors(console, inventory)

    if next_free_prefix is not None:
        _report_next_free(app, console, inventory, next_free_prefix, size, output_format)
        return
    if free_prefix is not None:
        _report_free_space(app, console, inventory, free_prefix, output_format)
        return

    report = build_ipam_report(inventory, app.config().validation, aggregated=aggregated)
    _report_ipam(app, console, inventory, report, family, output_format, conflicts_only)


def _reject_conflicting_ipam_options(
    free_prefix: str | None,
    next_free_prefix: str | None,
    size: str | None,
    aggregated: bool,
    conflicts_only: bool,
    family: str,
) -> None:
    """Refuse option combinations that would silently ignore one of them.

    Click cannot express "these are alternatives", and a flag that is quietly
    dropped is worse than an error: the operator believes they asked for
    something they did not get.
    """
    if free_prefix is not None and next_free_prefix is not None:
        raise click.UsageError("--free and --next-free ask different questions; use one of them")
    query = free_prefix is not None or next_free_prefix is not None
    if size is not None and next_free_prefix is None:
        raise click.UsageError("--size only means something with --next-free")
    asked = "--free" if free_prefix is not None else "--next-free"
    for flag, name in ((aggregated, "--aggregate"), (conflicts_only, "--conflicts")):
        if flag and query:
            raise click.UsageError(
                f"{name} applies to the utilisation report, which {asked} replaces"
            )
    if conflicts_only and aggregated:
        raise click.UsageError("--conflicts prints no utilisation table for --aggregate to fold")
    if family != "all" and (query or conflicts_only):
        raise click.UsageError("--family applies to the utilisation table only")


def _parse_prefix_option(text: str, option: str) -> IPNetwork:
    """Read a prefix from the command line, or fail with click's own wording."""
    try:
        return parse_prefix(text)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint=option) from None


def _report_next_free(
    app: AppContext,
    console: Console,
    inventory: Inventory,
    text: str,
    size: str | None,
    output_format: str,
) -> None:
    """``--next-free``: the first free block, on stdout and nothing else.

    The prefix is printed bare so the command composes —
    ``netgraph ipam --next-free 10.0.0.0/8 --size 26`` is meant to be read by
    the next command in a pipeline as often as by a person.
    """
    prefix = _parse_prefix_option(text, "--next-free")
    length = DEFAULT_SIZE[prefix.version] if size is None else _parse_size(size, prefix.version)
    subnets = subnets_of(inventory)
    block = next_free(prefix, length, subnets)

    if output_format == "json":
        console.print(
            _serialise(
                {
                    "prefix": str(prefix),
                    "size": length,
                    "next": str(block) if block is not None else None,
                },
                "json",
            )
        )
    elif output_format == "csv":
        console.print(
            _csv(("PREFIX", "SIZE", "NEXT"), [[str(prefix), str(length), str(block or "")]])
        )
    elif block is not None:
        console.print(str(block))

    if block is None:
        console.error(
            f"no free /{length} inside {prefix}; "
            f"run 'netgraph ipam --free {prefix}' to see what is left"
        )
        raise click.exceptions.Exit(EXIT_INVALID)
    app.log(f"first free /{length} in {prefix} is {block}", level=1)


def _parse_size(size: str, version: int) -> int:
    try:
        return parse_size(size, version)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--size") from None


def _report_free_space(
    app: AppContext,
    console: Console,
    inventory: Inventory,
    text: str,
    output_format: str,
) -> None:
    """``--free``: the holes in one prefix, as the fewest CIDR blocks."""
    prefix = _parse_prefix_option(text, "--free")
    subnets = subnets_of(inventory)
    blocks = free_space(prefix, subnets)
    allocated = allocations_within(prefix, subnets)

    headers = ("BLOCK", "IP", "HOSTS")
    aligns: tuple[Align, ...] = ("left", "right", "right")
    rows = [[str(block), str(block.version), _capacity_text(block)] for block in blocks]
    records = [
        {
            "block": str(block),
            "family": f"ipv{block.version}",
            "capacity": usable_addresses(block),
        }
        for block in blocks
    ]

    if output_format == "json":
        console.print(
            _serialise(
                {
                    "prefix": str(prefix),
                    "allocated": [str(block) for block in allocated],
                    "free": records,
                },
                "json",
            )
        )
        return
    if output_format == "csv":
        console.print(_csv(headers, rows))
        return

    console.info(
        f"free space in {prefix}: {len(blocks)} block(s), "
        f"{len(allocated)} allocation(s) already carved out"
    )
    console.table(headers, rows, aligns=aligns, empty=f"{prefix} is fully allocated")


def _report_ipam(
    app: AppContext,
    console: Console,
    inventory: Inventory,
    report: IpamReport,
    family: str,
    output_format: str,
    conflicts_only: bool,
) -> None:
    """The default report: utilisation, then conflicts."""
    rows = report.rows if family == "all" else report.of_family(4 if family == "ipv4" else 6)
    diagnostics = [Diagnostic.from_finding(finding, inventory) for finding in report.findings]

    if output_format == "json":
        payload: dict[str, Any] = {"conflicts": [entry.as_record() for entry in diagnostics]}
        if not conflicts_only:
            payload = {"subnets": [row.record() for row in rows], **payload}
        console.print(_serialise(payload, "json"))
    elif output_format == "csv":
        if conflicts_only:
            console.print(_conflicts_csv(diagnostics))
        else:
            # One CSV document holds one table. The utilisation rows are the
            # half a spreadsheet or an awk script wants; the conflicts are a
            # different shape entirely, and gluing them together would produce
            # a file no parser reads correctly.
            console.print(_utilisation_csv(rows))
            if report.findings:
                console.info(
                    f"{_plural(len(report.findings), 'conflict')} not shown: CSV holds one "
                    f"table, so run 'netgraph ipam --conflicts --format csv' for them"
                )
    else:
        if not conflicts_only:
            _print_utilisation_table(console, rows, aggregated=report.aggregated)
            # The heading only earns its line when something precedes it; with
            # ``--conflicts`` the list is the whole output and needs no label.
            console.print()
            console.print(console.bold("conflicts"))
        _report_problems(console, (), report.findings)

    if any(finding.severity.is_fatal for finding in report.findings):
        raise click.exceptions.Exit(EXIT_INVALID)


def _print_utilisation_table(
    console: Console, rows: Sequence[Utilisation], *, aggregated: bool
) -> None:
    # A VRF column only when something is in one; see ``_list_subnets``.
    partitioned = any(row.vrf for row in rows)
    headers = ["PREFIX", "IP", "VLANS", "HOSTS", "USED", "FREE", "UTIL", "DEVICES"]
    aligns: list[Align] = [
        "left",
        "right",
        "left",
        "right",
        "right",
        "right",
        "right",
        "right",
    ]
    if partitioned:
        headers.insert(0, "VRF")
        aligns.insert(0, "left")
    if aggregated:
        headers.append("PARTS")
        aligns.append("right")

    table: list[list[str]] = []
    for row in rows:
        cells = [
            *([row.vrf or "-"] if partitioned else []),
            row.prefix,
            str(row.version),
            compact_ids(row.vlans) or "-",
            format_capacity(row.capacity, host_bits=row.host_bits),
            str(row.assigned),
            format_capacity(row.free, host_bits=row.host_bits),
            _utilisation_cell(console, row),
            str(row.devices),
        ]
        if aggregated:
            cells.append(str(len(row.members)) if row.is_aggregate else "-")
        table.append(cells)
    console.table(headers, table, aligns=aligns, empty="no addresses declared")


def _utilisation_cell(console: Console, row: Utilisation) -> str:
    """The utilisation percentage, coloured once it is worth acting on."""
    text = format_utilisation(row.assigned, row.capacity)
    percent = row.assigned * 100 / row.capacity if row.capacity else 0.0
    for threshold, colour in _UTILISATION_COLOURS:
        if percent >= threshold:
            return console.style(text, fg=colour)
    return text


def _capacity_text(block: IPNetwork) -> str:
    return format_capacity(usable_addresses(block), host_bits=block.max_prefixlen - block.prefixlen)


def _utilisation_csv(rows: Sequence[Utilisation]) -> str:
    return _csv(
        ("prefix", "family", "vlans", "capacity", "assigned", "free", "utilisation", "devices"),
        [
            [
                row.prefix,
                row.family,
                " ".join(str(vlan) for vlan in row.vlans),
                str(row.capacity),
                str(row.assigned),
                str(row.free),
                f"{row.assigned / row.capacity:.6f}" if row.capacity else "",
                str(row.devices),
            ]
            for row in rows
        ],
    )


def _conflicts_csv(diagnostics: Sequence[Diagnostic]) -> str:
    return _csv(
        ("rule", "alias", "severity", "element", "file", "message"),
        [
            [
                entry.rule,
                entry.alias or "",
                str(entry.severity),
                entry.element or "",
                entry.file or "",
                entry.message,
            ]
            for entry in diagnostics
        ],
    )


def _csv(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Render a table as RFC 4180 CSV, without a trailing newline.

    ``\\n`` line endings rather than csv's default ``\\r\\n``: every other
    format this CLI writes uses them, and a mixed-ending file surprises the
    next tool in the pipeline more than a Unix-ending CSV does.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().rstrip("\n")


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #

#: Which zones ``export dns-zone`` writes. ``all`` puts them in one document
#: separated by banners, which is for reading and diffing; a nameserver loads
#: one zone per file, so publishing means ``forward`` and ``reverse`` in turn.
ZONE_SELECTIONS: Final[tuple[str, ...]] = ("all", "forward", "reverse")

#: How ``export cable-list`` lays the pull list out. Same rows, same columns,
#: same order — only the framing differs, so neither is the lossy one.
TABLE_FORMATS: Final[tuple[str, ...]] = ("csv", "markdown")

#: Which formats each format-specific option belongs to, and how the option is
#: spelled. Everything not listed here — the filters, ``--strict``, ``--force``
#: — applies to every format.
#:
#: The point of the table is :func:`_reject_irrelevant_export_options`: a flag
#: that is silently ignored is worse than a usage error, because the operator
#: believes they asked for something they did not get. Driving that check from
#: one table rather than from a chain of conditionals is what stops a new
#: option from being added without one.
_EXPORT_OPTION_SCOPE: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "origin": ("--origin", ("dns-zone",)),
    "ttl": ("--ttl", ("dns-zone",)),
    "soa_mname": ("--soa-mname", ("dns-zone",)),
    "soa_rname": ("--soa-rname", ("dns-zone",)),
    "soa_serial": ("--serial", ("dns-zone",)),
    "soa_refresh": ("--refresh", ("dns-zone",)),
    "soa_retry": ("--retry", ("dns-zone",)),
    "soa_expire": ("--expire", ("dns-zone",)),
    "soa_minimum": ("--minimum", ("dns-zone",)),
    "nameservers": ("--ns", ("dns-zone",)),
    "zones": ("--zones", ("dns-zone",)),
    "port": ("--port", ("prometheus-sd",)),
    "labels": ("--label", ("prometheus-sd",)),
    "table_format": ("--table-format", ("cable-list",)),
}


def _resolve_domain(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """Validate a domain name option and normalise it to its absolute form.

    Done at parse time so ``--origin 'example .com'`` fails before an inventory
    is loaded, with the flag named — rather than producing a zone file every
    nameserver rejects and blaming the tool that wrote it.
    """
    if value is None:
        return None
    try:
        return domain_name(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from None


def _resolve_domains(
    ctx: click.Context, param: click.Parameter, value: Sequence[str]
) -> tuple[str, ...]:
    """The repeatable form of :func:`_resolve_domain`, for ``--ns``."""
    return tuple(dict.fromkeys(_resolve_domain(ctx, param, item) or "" for item in value))


def _resolve_labels(
    ctx: click.Context, param: click.Parameter, value: Sequence[str]
) -> dict[str, str]:
    """Parse ``--label KEY=VALUE`` into a mapping, refusing what it must.

    Three things are refused, and the last two matter most. A name that is not a
    Prometheus label name at all. A reserved ``__``-prefixed one, which would be
    discarded after relabelling, leaving a target file that looks configured and
    is not. And any label the emitter computes per element — ``instance`` and the
    ``netgraph_`` namespace — because a *static* value for one of those would
    give every target in the estate the same identity.
    """
    labels: dict[str, str] = {}
    for item in value:
        key, separator, text = item.partition("=")
        if not separator:
            raise click.BadParameter(
                f"{item!r} is not 'KEY=VALUE'; write --label site=hq", ctx=ctx, param=param
            )
        if not is_assignable_label(key):
            raise click.BadParameter(
                f"{key!r} cannot be set here: a label is letters, digits and '_', not "
                f"starting with a digit; the reserved '__' prefix, 'instance' and the "
                f"'netgraph_' namespace are computed per element and would be overwritten "
                f"for every target at once",
                ctx=ctx,
                param=param,
            )
        labels[key] = text
    return dict(sorted(labels.items()))


#: The SOA and zone parameters of ``export dns-zone``. The defaults are the
#: conventional ones (RFC 1912 §2.2) rather than anything netgraph invents;
#: what is *not* conventional is the serial, which is fixed rather than derived
#: from the clock so that two exports of an unchanged inventory are identical.
_DNS_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "--origin",
        callback=_resolve_domain,
        default=None,
        metavar="NAME",
        help="Zone origin, e.g. 'example.com'. Required by dns-zone.",
    ),
    click.option(
        "--ttl",
        type=click.IntRange(0),
        default=3600,
        show_default=True,
        metavar="SECONDS",
        help="$TTL of every zone written.",
    ),
    click.option(
        "--soa-mname",
        callback=_resolve_domain,
        default=None,
        metavar="NAME",
        show_default="ns.<origin>",
        help="Primary nameserver for the SOA record.",
    ),
    click.option(
        "--soa-rname",
        callback=_resolve_domain,
        default=None,
        metavar="NAME",
        show_default="hostmaster.<origin>",
        help="Responsible mailbox for the SOA record, in DNS form.",
    ),
    click.option(
        "--serial",
        "soa_serial",
        type=click.IntRange(0, 2**32 - 1),
        default=1,
        show_default=True,
        metavar="N",
        help=(
            "SOA serial. Fixed rather than derived from the clock, so that re-exporting "
            "an unchanged inventory produces an unchanged file; bump it where you publish."
        ),
    ),
    click.option(
        "--refresh",
        "soa_refresh",
        type=click.IntRange(0),
        default=86400,
        show_default=True,
        metavar="SECONDS",
        help="SOA refresh: how often a secondary re-checks the zone.",
    ),
    click.option(
        "--retry",
        "soa_retry",
        type=click.IntRange(0),
        default=7200,
        show_default=True,
        metavar="SECONDS",
        help="SOA retry: how long a secondary waits after a failed refresh.",
    ),
    click.option(
        "--expire",
        "soa_expire",
        type=click.IntRange(0),
        default=3600000,
        show_default=True,
        metavar="SECONDS",
        help="SOA expire: when a secondary stops answering with data it could not refresh.",
    ),
    click.option(
        "--minimum",
        "soa_minimum",
        type=click.IntRange(0),
        default=3600,
        show_default=True,
        metavar="SECONDS",
        help="SOA minimum: the negative-caching TTL of RFC 2308.",
    ),
    click.option(
        "--ns",
        "nameservers",
        multiple=True,
        callback=_resolve_domains,
        metavar="NAME",
        show_default="the --soa-mname",
        help="NS record at the zone apex. Repeatable.",
    ),
    click.option(
        "--zones",
        type=click.Choice(ZONE_SELECTIONS),
        default="all",
        show_default=True,
        help=(
            "Which zones to write. 'all' concatenates them into one document for reading; "
            "a nameserver wants 'forward' and 'reverse' in separate files."
        ),
    ),
)

#: Everything the other two parameterised formats take.
_TARGET_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "--port",
        type=click.IntRange(1, 65535),
        default=None,
        metavar="PORT",
        show_default="none, a bare address",
        help="Port appended to every prometheus-sd target. IPv6 is bracketed automatically.",
    ),
    click.option(
        "--label",
        "labels",
        multiple=True,
        callback=_resolve_labels,
        metavar="KEY=VALUE",
        help="Static label merged into every prometheus-sd target. Repeatable.",
    ),
    click.option(
        "--table-format",
        type=click.Choice(TABLE_FORMATS),
        default="csv",
        show_default=True,
        help="How cable-list is laid out. The rows and columns are the same either way.",
    ),
)


def _export_flags(command: _Command) -> _Command:
    """Apply every option ``export`` takes, in ``--help`` order."""
    return _apply(
        (*_FILTER_OPTIONS, *_DNS_OPTIONS, *_TARGET_OPTIONS, *_VALIDATION_OPTIONS), command
    )


def _describe_exports() -> str:
    """One clause per registered exporter, generated from the registry.

    The same reasoning as :func:`_describe_formats`: a format added to
    :data:`~netgraph.export.EXPORTERS` documents itself here and cannot drift
    out of step with what ``FORMAT`` accepts.
    """
    return (
        "; ".join(f"{name}: {exporter.description}" for name, exporter in EXPORTERS.items()) + "."
    )


@cli.command("export", epilog=f"Formats -- {_describe_exports()}")
@click.argument(
    "export_format",
    metavar="FORMAT",
    type=click.Choice(EXPORT_FORMATS),
    shell_complete=complete_export_format,
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the artefact to this file instead of stdout.",
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help=(
        "Write the JSON record of what was skipped to this file. "
        "It goes to stderr when no file is named."
    ),
)
@_export_flags
@click.pass_context
def export_command(
    ctx: click.Context,
    /,
    export_format: str,
    output: Path | None,
    manifest_path: Path | None,
    **_options: Any,
) -> None:
    """Turn the inventory into an operational artefact.

    FORMAT is one of hosts, dns-zone, ansible-inventory, prometheus-sd or
    cable-list. Every one of them is deterministic and text-diffable, is scoped
    by the same filters a render takes, and is lossy in its own way — so what it
    could not represent is reported as a JSON manifest on stderr rather than
    dropped in silence.

    Validation runs first, exactly as it does for a render: an artefact
    generated from an inventory with a dangling cable would misrepresent the
    network, so errors refuse the export unless --force is given.
    """
    app: AppContext = ctx.obj
    params = ctx.params
    # stdout carries the artefact, so every diagnostic and the manifest go to
    # stderr unless a file was named for them.
    console = app.console(err=True)

    _reject_irrelevant_export_options(ctx, export_format)
    options = _export_options(params, export_format)

    inventory = app.load()
    findings = _run_validation(app, inventory, strict=bool(params["strict"]))
    if _is_rejected(inventory, findings):
        _report_problems(console, inventory.errors, findings, commentary=True)
        if not params["force"]:
            console.error(
                "refusing to export from an inventory with errors; fix them, or pass "
                "--force to export anyway"
            )
            raise click.exceptions.Exit(EXIT_INVALID)
        console.warn("exporting despite errors (--force): the artefact may not match the network")
    elif findings:
        _report_problems(console, (), findings, commentary=True)

    spec = _filter_spec(params)
    graphs = {
        layer: _build_graph(app, inventory, layer=layer, spec=spec, console=console)
        for layer in layers_for(export_format)
    }
    result = export(
        export_format,
        lambda recorder: ExportContext(
            inventory=inventory, graphs=graphs, options=options, recorder=recorder
        ),
    )

    _write_output(result.encode(), output=output)
    _report_manifest(console, result, manifest_path=manifest_path)
    console.info(
        f"exported {export_format}: {result.manifest.summary()}"
        + (f", written to {output}" if output is not None else "")
    )


def _reject_irrelevant_export_options(ctx: click.Context, export_format: str) -> None:
    """Refuse a format-specific flag given to a format that has no use for it.

    Raises:
        click.UsageError: An option outside this format's scope was typed. Only
            what the user actually typed is checked — every one of these has a
            default, and refusing the defaults would refuse every invocation.
    """
    typed = _explicit(ctx)
    for parameter, (option, formats) in _EXPORT_OPTION_SCOPE.items():
        if parameter in typed and export_format not in formats:
            applies = " or ".join(formats)
            raise click.UsageError(f"{option} applies to '{applies}', not to '{export_format}'")
    if export_format == "dns-zone" and not ctx.params.get("origin"):
        raise click.UsageError(
            "dns-zone needs --origin: a zone file has no meaning without the domain its "
            "records hang under, e.g. --origin example.com"
        )


def _export_options(params: Mapping[str, Any], export_format: str) -> ExportOptions:
    """Build the emitter options from the parsed command line."""
    return ExportOptions(
        origin=params["origin"] or "",
        ttl=params["ttl"],
        soa_mname=params["soa_mname"] or "",
        soa_rname=params["soa_rname"] or "",
        soa_serial=params["soa_serial"],
        soa_refresh=params["soa_refresh"],
        soa_retry=params["soa_retry"],
        soa_expire=params["soa_expire"],
        soa_minimum=params["soa_minimum"],
        nameservers=tuple(params["nameservers"]),
        zones=params["zones"],
        port=params["port"],
        labels=dict(params["labels"]),
        table_format=params["table_format"],
    )


def _report_manifest(console: Console, result: ExportResult, *, manifest_path: Path | None) -> None:
    """Emit the record of what the artefact could not hold.

    To a file when one was named, and to stderr otherwise — never to stdout,
    which belongs to the artefact. A clean export still produces a manifest: a
    consumer parsing it must not have to distinguish "nothing was skipped" from
    "the tool forgot to say".

    The stderr copy is commentary and is therefore silenced by ``--quiet``, as
    every other note this CLI writes is. ``--manifest FILE`` is not: a pipeline
    that wants the record *and* wants the run quiet names a file for it, which
    is written whatever the verbosity.
    """
    document = result.manifest.to_json()
    if manifest_path is not None:
        _write_file(document.encode("utf-8"), manifest_path)
        console.info(f"manifest written to {manifest_path}")
        return
    console.info(document.rstrip("\n"))


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


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

#: The commands whose settings ``config show`` can resolve. Each one applies the
#: ``[render]`` table; which settings it *takes* is decided by the options it
#: declares, so ``web`` shows the two it has rather than a table of twenty
#: settings it would ignore.
CONFIGURABLE: Final[tuple[str, ...]] = ("render", "watch", "path", "web")


@cli.group("config")
def config_command() -> None:
    """Inspect the per-inventory netgraph.toml."""


@config_command.command("show")
@click.argument("command", type=click.Choice(CONFIGURABLE), default="render", required=False)
@click.option(
    "--profile",
    default=None,
    metavar="NAME",
    shell_complete=complete_profile,
    help="Resolve as if --profile NAME had been given.",
)
@click.pass_obj
def config_show_command(app: AppContext, command: str, profile: str | None) -> None:
    """Print the settings COMMAND resolves to, and where each one comes from.

    No flags are in play, so what is shown is what the file does to a bare
    'netgraph COMMAND': every value is the profile's, the [render] table's, or
    netgraph's own. Add --show-config to the command itself to see a particular
    invocation resolved, flags included.
    """
    console = app.console()
    config = app.config()
    target = cli.commands[command]
    _print_validation(console, config)
    console.print()
    _print_settings(
        console,
        _settings_for(target, config, profile=profile),
        config=config,
        command=command,
    )


def _print_validation(console: Console, config: Config) -> None:
    """The ``[validate]`` half of the file, which has no per-command shape."""
    validation = config.validation
    console.info("validation")
    console.table(
        ("SETTING", "VALUE", "SOURCE"),
        [
            [
                "strict",
                "true" if validation.strict else "false",
                _validate_source(config, "strict"),
            ],
            [
                "ignore",
                ", ".join(sorted(validation.ignore)) or "(none)",
                _validate_source(config, "ignore"),
            ],
            [
                "severity",
                ", ".join(f"{rule}={grade}" for rule, grade in sorted(validation.severity.items()))
                or "(none)",
                _validate_source(config, "severity"),
            ],
        ],
    )


def _validate_source(config: Config, key: str) -> str:
    """Whether the file said anything about this validation setting."""
    if config.path is None:
        return "default"
    default = ValidationConfig()
    current = getattr(config.validation, {"severity": "severity"}.get(key, key))
    return "file [validate]" if current != getattr(default, key) else "default"


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
