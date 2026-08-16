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
import fnmatch
import io
import json
import os
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any, Final, TypeVar, cast

import click
import click.core
import yaml
from click.core import ParameterSource

from netgraph.completion import (
    SHELLS,
    complete_element,
    complete_export_format,
    complete_fix,
    complete_format,
    complete_kind,
    complete_layer,
    complete_namespace,
    complete_node,
    complete_profile,
    complete_rule,
    complete_test_suite,
    completion_script,
)
from netgraph.config import (
    CACHE_TABLE,
    CONFIG_FILE_NAME,
    CacheConfig,
    Config,
    ValidationConfig,
    load_config,
)
from netgraph.console import Align, Console
from netgraph.converge import (
    CONVERGE_DIALECTS,
    ConvergePlan,
    DisruptiveChangeError,
    render_converge,
)
from netgraph.converge import REPORT_FORMATS as CONVERGE_FORMATS
from netgraph.converge import converge as converge_network
from netgraph.converge import write_scripts as write_converge_scripts
from netgraph.diagnostics import FORMATS as DIAGNOSTIC_FORMATS
from netgraph.diagnostics import Diagnostic, dump_json
from netgraph.diagnostics import build_report as build_diagnostics
from netgraph.diagnostics import render_report as render_diagnostics
from netgraph.diff import draw
from netgraph.drawio import (
    Diagram,
    DrawioFormatError,
    ReconcileOptions,
    Reconciliation,
    parse_mxfile,
    reconcile,
)
from netgraph.drawio import Level as DrawioLevel
from netgraph.drift import FORMATS as DRIFT_FORMATS
from netgraph.drift import CompareSpec, DriftReport, check_drift, render_drift
from netgraph.drift import write_text as write_drift
from netgraph.edit import (
    DEFAULT_SUFFIX,
    AddInterface,
    AddressError,
    CascadeRequired,
    Connect,
    CopyPlan,
    CreateElement,
    DeleteElement,
    Disconnect,
    EditError,
    EditSession,
    MoveElement,
    Operation,
    RemoveInterface,
    RenameElement,
    SetField,
    UnsetField,
    ValidationRefused,
    copy_plan,
    operations_from_json,
)
from netgraph.errors import (
    ConfigurationError,
    LoaderError,
    NetgraphError,
    RenderError,
    count_text,
    format_path,
)
from netgraph.export import (
    CONFIG_FORMATS,
    EXPORTERS,
    ConfigSet,
    ExportContext,
    ExportOptions,
    ExportResult,
    domain_name,
    export,
    is_assignable_label,
    layers_for,
)
from netgraph.export import FORMATS as EXPORT_FORMATS
from netgraph.export.config.write import stale_files, write_config
from netgraph.fixes import FIXES, FixReport, repair, spec_for
from netgraph.fsio import display_path, write_text
from netgraph.history import Commit, Frame, HistoryError, Timeline
from netgraph.impact import DEFAULT_LIMIT, ImpactError, render_impact
from netgraph.impact import LAYERS as IMPACT_LAYERS
from netgraph.impact import REPORT_FORMATS as IMPACT_REPORT_FORMATS
from netgraph.impact import simulate as run_impact
from netgraph.importer import (
    DIALECTS,
    Draft,
    ImportSourceError,
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
from netgraph.layout.geometry import Routing
from netgraph.layout.resolve import resolve_geometry
from netgraph.layout.seed import (
    DEFAULT_LAYOUT_NAME,
    LAYOUT_ENGINES,
    LayoutReport,
    clear_operations,
    inspect_layout,
    live_keys,
    prune_operations,
    seed_geometry,
    views_for,
    write_operations,
)
from netgraph.listing import LISTINGS
from netgraph.listing import SUBJECTS as LISTING_SUBJECTS
from netgraph.listing import utilisation as utilisation_listing
from netgraph.loader import (
    DISABLE_ENV_VAR,
    YAML_SUFFIXES,
    CacheInfo,
    DocumentCache,
    Inventory,
    LoadError,
    clear_cache,
    disabled_by_environment,
    inspect_cache,
    load_stream,
    load_tree,
    open_cache,
    read_documents,
    short_name,
    subset,
)
from netgraph.models import DOCUMENT_KINDS, KINDS, Element, Medium
from netgraph.models.layout import LAYOUT_VIEWS, ROUTING_STYLES
from netgraph.plan import (
    PLAN_FORMATS,
    Plan,
    PlanExecutionError,
    PlanFormatError,
    PlanSourceError,
    StateRef,
    adopt,
    git_ref,
    plan_from_dict,
    render_plan,
    state_digest,
    summary_line,
    translate,
    write_plan,
)
from netgraph.plan import (
    diff as diff_states,
)
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
    Theme,
    UnknownElementError,
    advisories_for,
    aggregate_graph,
    build_graph,
    collapse_targets,
    diff_formats,
    draws_racks,
    filter_graph,
    icon_theme,
    is_binary_format,
    load_theme,
    rack_formats,
    render,
    render_layers,
    resolve_theme,
    style_theme_choices,
    supports_diff,
    supports_highlight,
    supports_icons,
    supports_interaction,
    supports_layers,
    theme_choices,
)
from netgraph.render.dot import (
    DOT_ENV_VAR,
    DOT_EXECUTABLE,
    IMAGE_FORMATS,
    find_dot,
    graphviz_install_hint,
    routing_advisories,
)
from netgraph.report import EPOCH_ENV_VAR, Bundle, git_revision, resolve_timestamp
from netgraph.report import FORMATS as REPORT_FORMATS
from netgraph.report import JSON_FILE as REPORT_JSON_FILE
from netgraph.report import Options as ReportOptions
from netgraph.report import generate as generate_report
from netgraph.review import Diagram as ReviewDiagram
from netgraph.review import Review, Verdict, build_review, render_comment, summarise
from netgraph.rules import RULES, Severity, resolve_rule_id, rule_for
from netgraph.scaffold import SCHEMA_FILE_NAME, build_scaffold, write_scaffold
from netgraph.schema import build_schema
from netgraph.settings import (
    RENDER_TABLE,
    Origin,
    Resolution,
    resolve_settings,
)
from netgraph.subnets import IPNetwork, subnets_of
from netgraph.testing import FORMATS as TEST_FORMATS
from netgraph.testing import render_test_report, run_tests, suite_location
from netgraph.trace import DEFAULT_MAX_HOPS, TraceError, TraceResult, render_trace, trace
from netgraph.trace import REPORT_FORMATS as TRACE_FORMATS
from netgraph.validate import Finding
from netgraph.validate import validate as run_validation
from netgraph.version import as_dict as version_as_dict
from netgraph.version import collect as collect_version
from netgraph.version import format_text as format_version
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
    is_loopback,
    run_watch,
)
from netgraph.web import DEFAULT_PORT as WEB_PORT
from netgraph.web import WebServer
from netgraph.web.session import EditingSession, TreeWatcher

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

#: Exit status when a plan is not empty: 'netgraph converge plan' exits 0 when
#: the network already matches the inventory and 2 when changes are pending.
#: Distinct from :data:`EXIT_INVALID` on purpose — a pipeline has to be able to
#: tell "there is work to do" from "the inputs were wrong".
EXIT_CONVERGE_PENDING: Final = 2

#: Report formats ``impact`` accepts only with ``--redundancy``: both describe
#: problems in files, and an analysis of a network is not one.
DIAGNOSTIC_ONLY_FORMATS: Final[tuple[str, ...]] = ("sarif", "github")

#: Every value ``impact --output-format`` accepts.
IMPACT_FORMATS: Final[tuple[str, ...]] = (*IMPACT_REPORT_FORMATS, *DIAGNOSTIC_ONLY_FORMATS)

#: How many candidate names an ambiguous ``--fail`` lists before giving up.
_MAX_CANDIDATES: Final = 10

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
    #: ``--no-cache``: parse every file, and remember nothing for the next run.
    no_cache: bool = False
    #: ``netgraph.toml``, read at most once per run. A command may ask for it
    #: twice — once to resolve its render defaults, once to grade findings — and
    #: reading the file twice could answer the two questions differently if it
    #: were edited in between.
    _config: Config | None = field(default=None, repr=False)
    #: The parse cache, opened at most once per run and shared by every load —
    #: which is what makes a *second* load in the same process (``watch``) hit
    #: memory rather than the disk. ``_cache_open`` distinguishes "not yet asked
    #: for" from "asked for, and there is none".
    _cache: DocumentCache | None = field(default=None, repr=False)
    _cache_open: bool = field(default=False, repr=False)

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
        # Provenance is the YAML node tree, which is exactly what a cached entry
        # does not hold, so the two are mutually exclusive by construction rather
        # than by a check inside the loader.
        cache = None if keep_provenance else self.cache()
        inventory = load_tree(self.inventory, keep_provenance=keep_provenance, cache=cache)
        self.log(
            f"loaded {len(inventory.elements)} element(s): "
            f"{len(inventory.devices)} device(s), {len(inventory.cables)} cable(s), "
            f"{len(inventory.adapters)} adapter(s), {len(inventory.tunnels)} tunnel(s)",
            level=1,
        )
        if cache is not None:
            self.log(cache.stats.summary(), level=1)
            if cache.stats.problem is not None:
                self.log(f"cache: {cache.stats.problem}", level=1)
        return inventory

    def cache(self) -> DocumentCache | None:
        """The parse cache for this inventory, or ``None`` when it is off.

        Off means one of three things, in the order they are checked:
        ``--no-cache``, the :data:`~netgraph.loader.DISABLE_ENV_VAR` environment
        variable, or ``[cache] enabled = false`` in ``netgraph.toml``.

        A ``netgraph.toml`` that cannot be read at all is *not* one of them. Some
        commands here never look at the file; making them start failing over a
        broken ``[render]`` table because the cache went to ask about a directory
        would be a new failure mode for an optimisation. The commands that do
        read it still report the error properly, and this one runs uncached.
        """
        if self._cache_open:
            return self._cache
        self._cache_open = True
        settings = self.cache_settings()
        if not settings.enabled:
            return None
        self._cache = open_cache(
            self.inventory,
            directory=settings.directory,
            max_bytes=settings.max_bytes,
        )
        return self._cache

    def cache_settings(self) -> CacheConfig:
        """The ``[cache]`` table with the command line and environment applied."""
        try:
            settings = self.config().cache
        except ConfigurationError:
            settings = CacheConfig()
        return settings.with_overrides(
            no_cache=self.no_cache or disabled_by_environment(),
        )

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


def _show_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """``-V``/``--version``: the report, then exit before anything is loaded.

    Eager and ``expose_value=False``, the way click's own ``version_option`` is,
    so ``netgraph --version`` answers from a directory that holds no inventory.
    """
    if not value or ctx.resilient_parsing:
        return
    click.echo(format_version(collect_version()), nl=False)
    ctx.exit()


@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.option(
    "-V",
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_show_version,
    help=(
        "Show the netgraph, Python and Graphviz versions in use, and exit. "
        "'netgraph version --json' is the same report, machine-readably."
    ),
)
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
@click.option(
    "--no-cache",
    is_flag=True,
    help=(
        "Parse every file, and remember nothing. The cache is keyed by file "
        f"contents and is safe to leave on; set {DISABLE_ENV_VAR}=1 to switch it "
        "off for a whole environment. See 'netgraph cache info'."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    inventory: Path,
    quiet: bool,
    verbose: int,
    color: bool | None,
    no_cache: bool,
) -> None:
    """Declare network elements in YAML and render them as network graphs."""
    ctx.obj = AppContext(
        inventory=inventory,
        quiet=quiet,
        verbosity=verbose,
        color=color,
        no_cache=no_cache,
    )
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
        console.info(f"  {_path_inside(file, path)}")
    for line in _next_steps(path, with_schema=with_schema, minimal=minimal):
        console.info(line)


def _path_inside(file: Path, root: Path) -> str:
    """``file`` as written inside ``root``, falling back to the path as given.

    Relative to the tree that was just created rather than to the working
    directory, which is what :func:`~netgraph.fsio.display_path` answers; the
    listing under "created 6 files in net/" reads as the file names it made.
    """
    try:
        return file.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - every written file is under the root
        return str(file)


def _next_steps(path: Path, *, with_schema: bool, minimal: bool = False) -> Iterator[str]:
    """What to run now, as a copy-pasteable block.

    ``cd`` is printed only when it is needed: the netgraph commands below it are
    run from the inventory root, which is already the shell's directory when
    ``init`` was given no argument. ``netgraph test`` is offered only for the
    example tree: ``--minimal`` declares no element, so it ships no assertions
    and the command would have nothing to grade.
    """
    yield ""
    yield "next steps:"
    if path.resolve() != Path.cwd():
        yield f"  cd {path}"
    yield "  netgraph validate"
    if not minimal:
        yield "  netgraph test"
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

#: The sub-command ``netgraph import`` runs when the first argument is not the
#: name of one. It is what the command has always been, and keeping it reachable
#: without typing it is not nostalgia: ``netgraph import caps/*.json`` is in
#: everybody's shell history and in the documentation of two released versions.
LEGACY_IMPORT: Final = "captures"


class _ImportGroup(click.Group):
    """``netgraph import`` — a group that still answers to its old signature.

    The command started life taking capture files positionally, and now has to
    grow a second source that reads something else entirely: an edited draw.io
    diagram is not a capture, does not scaffold a tree, and reconciles rather
    than writes. That is a sub-command, not a ``--from`` dialect.

    So the group resolves a first argument it does not recognise as a
    sub-command by handing the whole line to :data:`LEGACY_IMPORT`. ``netgraph
    import drawio FILE`` reaches the new command, ``netgraph import caps/*.json``
    reaches the old one, and neither has to know about the other. The one thing
    the fallback must not swallow is a request for help about the *group*, which
    is why the help options are checked before anything else.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] not in self.get_help_option_names(ctx):
            args = [LEGACY_IMPORT, *args]
        return super().parse_args(ctx, args)


@cli.group("import", cls=_ImportGroup)
def import_group() -> None:
    """Bring something from outside the tree into the inventory.

    \b
    netgraph import captures/*.json          from what live devices printed
    netgraph import drawio diagram.drawio    from a diagram somebody edited

    The first form is the original one and needs no sub-command: anything that
    is not the name of one is read as a capture file, so 'netgraph import
    caps/*.json' still means what it always did.
    """


@import_group.command(LEGACY_IMPORT)
@click.argument("inputs", nargs=-1, metavar="[NAME=]INPUT...")
@click.option(
    "--from",
    "dialect",
    type=click.Choice(DIALECTS),
    default="auto",
    show_default=True,
    help=(
        "Input dialect. 'auto' sniffs each input on its own, so one run may mix all ten: "
        "lldp is 'lldpctl -f json', iproute is 'ip -j link show' or 'ip -j addr show', "
        "csv is 'device,port,device,port' cabling rows, and netplan, networkd, ifupdown, "
        "frr, nftables, wireguard and interfaces are a device's running configuration in "
        "the same dialects 'netgraph export' writes."
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
def import_captures_command(
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
            report.info(f"  {_path_inside(file, output)}")
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
# import drawio
# --------------------------------------------------------------------------- #


@import_group.command("drawio")
@click.argument(
    "diagram_file",
    metavar="FILE",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--view",
    type=click.Choice([layer.value for layer in Layer]),
    default=None,
    shell_complete=complete_layer,
    show_default="the view the file says it was exported from",
    help=(
        "Which view the diagram draws. Read from the file for anything netgraph exported; "
        "needed only for a diagram netgraph did not write."
    ),
)
@click.option(
    "--geometry/--no-geometry",
    default=True,
    show_default=True,
    help="Carry cells that were dragged back as stored geometry.",
)
@click.option(
    "--renames/--no-renames",
    default=True,
    show_default=True,
    help="Carry a retyped label back as a rename, rewriting every reference to it.",
)
@click.option(
    "--deletions/--no-deletions",
    default=True,
    show_default=True,
    help=(
        "Carry a deleted cell back as a deleted element. Never applied to a diagram that was "
        "exported from a filtered view, whichever way this is set."
    ),
)
@click.option(
    "--connections/--no-connections",
    default=True,
    show_default=True,
    help="Carry an edge drawn in draw.io back as a cable on the first free port at each end.",
)
@click.option(
    "--name",
    "layout_name",
    default=DEFAULT_LAYOUT_NAME,
    show_default=True,
    metavar="NAME",
    help=(
        "metadata.name of the layout document new geometry goes into. Geometry for something "
        "an existing layout already places is written back into that one instead."
    ),
)
@click.option(
    "--namespace",
    "layout_namespace",
    default="",
    metavar="PATH",
    help="Folder to declare a new layout document in. The inventory root by default.",
)
@click.option(
    "--file",
    "layout_file",
    default=None,
    metavar="PATH",
    help="File to write a new layout document to. Chosen by the layout conventions when absent.",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    default=False,
    help="Do not ask before writing. For automation; a person should read the changeset first.",
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write nothing; print the changeset and the unified diff it would produce.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Write even if the result would introduce new validation errors.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Report as JSON.")
@click.pass_obj
def import_drawio_command(
    app: AppContext,
    diagram_file: Path,
    view: str | None,
    geometry: bool,
    renames: bool,
    deletions: bool,
    connections: bool,
    layout_name: str,
    layout_namespace: str,
    layout_file: str | None,
    auto_approve: bool,
    dry_run: bool,
    force: bool,
    as_json: bool,
) -> None:
    """Bring an edited draw.io diagram back into the inventory.

    The file is reconciled by the identity netgraph stamped into each cell when
    it exported it, not by what the cells are called or where they are: a cell
    that moved is a geometry write, one whose label changed is a rename, one
    that is gone is a deletion, and an edge somebody drew is a new cable. The
    result is a 'netgraph plan' changeset, shown and confirmed before any file
    is touched.

    A diagram netgraph did not export carries no identity, so nothing can be
    reconciled against it. It is read anyway and reported cell by cell, which
    is the honest answer: netgraph will not invent hardware from a rectangle.

    \b
    netgraph export drawio -o site.drawio      # hand this out
    netgraph import drawio site.drawio -n      # see what came back
    netgraph import drawio site.drawio         # apply it
    """
    console = app.console()
    root = app.inventory if app.inventory.is_dir() else app.inventory.parent

    try:
        diagram = parse_mxfile(
            diagram_file.read_text(encoding="utf-8", errors="replace"), source=str(diagram_file)
        )
    except OSError as exc:
        console.error(f"cannot read {diagram_file}: {exc.strerror or exc}")
        raise click.exceptions.Exit(EXIT_INVALID) from exc
    except DrawioFormatError as exc:
        console.error(str(exc))
        raise click.exceptions.Exit(exc.exit_code) from exc

    layer = _diagram_layer(diagram, view)
    inventory = app.load()
    if inventory.errors:
        _report_problems(console, inventory.errors, ())
        console.error("refusing to reconcile a diagram against an inventory that does not load")
        raise click.exceptions.Exit(EXIT_INVALID)

    result = reconcile(
        diagram,
        inventory,
        build_graph(inventory, layer=layer),
        ReconcileOptions(
            layout=layout_name,
            namespace=layout_namespace,
            file=layout_file,
            deletions=deletions,
            renames=renames,
            geometry=geometry,
            connections=connections,
        ),
    )
    _report_drawio_notes(console, result, as_json=as_json)
    if result.failed:
        raise click.exceptions.Exit(EXIT_INVALID)

    console.info(f"{diagram_file} ({layer} view): {result.summary()}")
    if result.is_empty:
        console.info("nothing to change")
        return

    session = EditSession(root=root, config=_edit_config(app), cache=app.cache())
    before = session.baseline
    try:
        session.apply_all(result.operations)
    except EditError as exc:
        _report_edit_error(console, exc)
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    plan = diff_states(
        before,
        session.inventory,
        source=StateRef(kind="tree", description=str(root), digest=state_digest(before)),
        target=StateRef(kind="drawio", description=str(diagram_file)),
    )
    if not as_json:
        write_plan(console, plan, verbose=app.verbosity > 0)
    if not dry_run and not auto_approve and not _confirm(console, plan):
        console.info("aborted; nothing has been written")
        raise click.exceptions.Exit(EXIT_INVALID)

    _run_edit_session(
        app,
        session,
        dry_run=dry_run,
        as_json=as_json,
        force=force,
        note=f"{len(result.operations)} operation(s) from {diagram_file}",
    )


def _diagram_layer(diagram: Diagram, view: str | None) -> Layer:
    """Which view the diagram is of.

    The file's own answer wins over the flag, because a diagram exported from
    the routing view reconciled as though it were the cabling view would read
    every cable as missing. ``--view`` is for the other case — a diagram
    netgraph never exported, which says nothing about what it draws.

    Raises:
        click.UsageError: The flag contradicts the file.
    """
    if diagram.view and view is not None and view != diagram.view:
        raise click.UsageError(
            f"the diagram says it was exported from the {diagram.view} view, so it cannot be "
            f"imported as the {view} one; drop --view"
        )
    if diagram.view:
        return Layer(diagram.view)
    return Layer(view) if view is not None else Layer.L1


def _report_drawio_notes(console: Console, result: Reconciliation, *, as_json: bool) -> None:
    """Everything the diagram said that did not become an operation.

    Printed *before* the changeset, so "why is this cell not in the plan?" is
    answered on the same screen as the plan. Warnings and errors go through the
    console's own channels so ``--quiet`` and a redirect treat them the way they
    treat every other diagnostic.
    """
    if as_json:
        console.print(json.dumps([note.to_dict() for note in result.notes], indent=2))
        return
    for note in result.notes:
        if note.level is DrawioLevel.ERROR:
            console.error(str(note))
        elif note.level is DrawioLevel.WARNING:
            console.warn(str(note))
        else:
            console.info(str(note))


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
        "csv is 'device,port,device,port' cabling rows, and netplan, networkd, ifupdown, "
        "frr, nftables, wireguard and interfaces are the running configuration in the same "
        "dialects 'netgraph export' writes."
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
    type=click.Choice(DIAGNOSTIC_FORMATS),
    default="text",
    show_default=True,
    help="text is for reading; json, sarif and github are for CI.",
)
@click.option(
    "--fix",
    is_flag=True,
    default=False,
    help=(
        "Repair every problem that has one unambiguous mechanical fix, then report what is "
        "left. A fix that would introduce a new finding is undone and reported instead."
    ),
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    default=False,
    help="With --fix: print the unified diff the repairs would apply, and write nothing.",
)
@click.option(
    "--choose",
    "chosen",
    multiple=True,
    metavar="RULE=FIX",
    shell_complete=complete_fix,
    help=(
        "Pick which repair to use for a rule that offers several, e.g. --choose W114=list. "
        "Repeatable. 'netgraph rules --fixable' lists the keys."
    ),
)
@click.pass_obj
def validate_command(
    app: AppContext,
    strict: bool,
    disabled: tuple[str, ...],
    output_format: str,
    fix: bool,
    dry_run: bool,
    chosen: tuple[str, ...],
) -> None:
    """Check the inventory for schema and semantic problems.

    Exits 1 when anything is reported as an error, 0 otherwise.

    The structured formats put their document on stdout and move the human
    summary to stderr, so ``netgraph validate -F sarif > report.sarif`` writes a
    file a code-scanning upload accepts while a person watching the run still
    sees what happened. ``--quiet`` drops that summary; it never drops the
    document.

    \b
        netgraph validate --fix              repair what can be repaired
        netgraph validate --fix --dry-run    ... and show the diff instead
        netgraph validate --fix --choose W114=list   pick between two repairs

    A fix is applied only if re-validating shows the finding gone and no rule
    reporting more than it did before, so --fix cannot make an inventory worse.
    docs/validation-rules.md lists which rules are fixable and what each fix does.
    """
    if dry_run and not fix:
        raise click.UsageError("--dry-run says how to --fix; it does nothing on its own")
    if chosen and not fix:
        raise click.UsageError("--choose says how to --fix; it does nothing on its own")
    if fix:
        _fix_inventory(
            app,
            strict=strict,
            disabled=disabled,
            output_format=output_format,
            dry_run=dry_run,
            choices=_fix_choices(chosen),
        )
        return

    # Only the structured formats report a line and a column, and paying for
    # them is a measurable amount of retained memory -- so the text path, which
    # locates a finding at its document, does not.
    inventory = app.load(keep_provenance=output_format != "text")
    findings = _run_validation(app, inventory, strict=strict, disabled=disabled)

    if output_format == "text":
        _report_problems(app.console(), inventory.errors, findings)
    else:
        report = build_diagnostics(inventory, findings)
        # The summary is commentary once the document is the output, so it goes
        # to stderr through ``info`` -- which is what ``--quiet`` silences.
        _report_problems(app.console(), inventory.errors, findings, commentary=True)
        document = render_diagnostics(report, output_format)
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


def _fix_choices(chosen: Sequence[str]) -> dict[str, str]:
    """Parse ``--choose W114=list`` into ``{"W114": "list"}``.

    Both halves are checked here rather than at use, so a typo is a usage error
    before anything is loaded instead of a repair that silently did not happen.

    Raises:
        click.UsageError: The token is malformed, names no rule, names a rule
            with no fix, or names a repair that rule does not offer.
    """
    choices: dict[str, str] = {}
    for token in chosen:
        name, separator, key = token.partition("=")
        if not separator or not name.strip() or not key.strip():
            raise click.UsageError(f"--choose takes RULE=FIX, not {token!r}")
        try:
            rule_id = resolve_rule_id(name.strip())
        except KeyError:
            raise click.UsageError(
                f"--choose {token}: no rule is called {name.strip()!r}"
            ) from None
        spec = spec_for(rule_id)
        if spec is None:
            raise click.UsageError(f"--choose {token}: {rule_id} has no mechanical fix")
        offered = [choice.key for choice in spec.choices]
        if key.strip() not in offered:
            listed = ", ".join(offered) if offered else "nothing to choose between"
            raise click.UsageError(
                f"--choose {token}: {rule_id} offers {listed}, not {key.strip()!r}"
            )
        choices[rule_id] = key.strip()
    return choices


def _fix_inventory(
    app: AppContext,
    *,
    strict: bool,
    disabled: Sequence[str],
    output_format: str,
    dry_run: bool,
    choices: Mapping[str, str],
) -> None:
    """Repair what can be repaired, then report the inventory that is left.

    The report is produced by loading and validating again rather than by
    reusing what the repair loop last saw, so ``--fix -F sarif`` says exactly
    what a plain ``-F sarif`` would say about the tree now on disk — lines,
    columns and all. A dry run has nothing on disk to re-read, so it reports
    what the repairs *would* leave, located at the document rather than the line.

    Raises:
        click.exceptions.Exit: The tree still has an error, or an edit failed.
    """
    console = app.console()
    commentary = output_format != "text"
    if not app.inventory.is_dir():
        # An edit session's unit is a *tree*: it resolves addresses across every
        # document under its root and may write any of them. Repairing findings
        # that came from one file while writing its neighbours is not something
        # a user can have meant, so the folder has to be named explicitly.
        raise click.UsageError(
            f"--fix works on an inventory folder; -i names the single file "
            f"{app.inventory.name!r}. Point -i at the folder that holds it."
        )
    root = app.inventory
    session = EditSession(root=root, config=_edit_config(app), cache=app.cache())
    settings = app.config().validation.with_overrides(
        strict=True if strict else None, ignore=disabled
    )
    try:
        report = repair(session, settings=settings, choices=choices)
        written = () if dry_run or not report.changed else session.commit()
    except EditError as exc:
        _report_edit_error(app.console(err=True), exc)
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    _report_fixes(console, report, dry_run=dry_run, written=written, commentary=commentary)
    if dry_run:
        # The diff is the artefact of a dry run, so it goes to stdout -- unless
        # stdout is a diagnostics document, in which case it is commentary like
        # everything else that is not the document.
        if report.diff:
            (console.info if commentary else console.print)(report.diff.rstrip("\n"))
        inventory, findings = session.inventory, list(report.remaining)
    else:
        inventory = app.load(keep_provenance=output_format != "text")
        findings = _run_validation(app, inventory, strict=strict, disabled=disabled)

    if output_format == "text":
        _report_problems(console, inventory.errors, findings)
    else:
        _report_problems(console, inventory.errors, findings, commentary=True)
        document = render_diagnostics(build_diagnostics(inventory, findings), output_format)
        if document:
            console.print(document)

    if _is_rejected(inventory, findings):
        raise click.exceptions.Exit(EXIT_INVALID)


def _report_fixes(
    console: Console,
    report: FixReport,
    *,
    dry_run: bool,
    written: Sequence[str],
    commentary: bool,
) -> None:
    """What the repair pass did, and what it deliberately did not do."""
    write = console.info if commentary else console.print
    verb = "would fix" if dry_run else "fixed"
    if report.applied:
        write(console.style(f"{verb} {count_text(len(report.applied), 'problem')}:", bold=True))
        for entry in report.applied:
            write(f"  {entry.finding.rule}  {entry.fix.title}")
    else:
        write("nothing here has a mechanical fix")

    # The choices a rule offers are the same for every finding of that rule, and
    # a tree with seventeen dangling cables would otherwise print them
    # seventeen times. The refusal itself is per finding, because it names one.
    detailed: set[str] = set()
    for refusal in report.skipped:
        rule = refusal.finding.rule
        head = f"{rule} at {refusal.finding.location} not fixed: {refusal.reason}"
        # Beside the findings rather than on stderr beneath them: in text output
        # the findings *are* the data, and a refusal is a statement about one of
        # them. It moves to stderr with everything else once stdout is a document.
        write(console.style(head, fg=_SEVERITY_COLOUR[Severity.WARNING]))
        for introduced in refusal.introduced:
            write(f"    it would add: {introduced}")
        if rule in detailed:
            continue
        detailed.add(rule)
        for fix in refusal.fixes if len(refusal.fixes) > 1 else ():
            write(f"    --choose {rule}={fix.key}  {fix.title}")
    if report.skipped:
        write()
    if written:
        write(f"wrote {count_text(len(written), 'file')}: {', '.join(written)}")


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
# edit
# --------------------------------------------------------------------------- #


#: The three flags every ``edit`` subcommand shares. Declared once because they
#: mean the same thing everywhere — what to do instead of writing, what to
#: print, and what to override — and declaring them per command would be twelve
#: chances for one of them to drift.
_EDIT_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "-n",
        "--dry-run",
        is_flag=True,
        help="Write nothing; print the unified diff the edit would apply.",
    ),
    click.option(
        "--json",
        "as_json",
        is_flag=True,
        help=(
            "Print the applied operations and their inverses as JSON, so a caller can keep "
            "an undo stack."
        ),
    ),
    click.option(
        "--force",
        is_flag=True,
        help=(
            "Write even when the edit would introduce a new error. The check for files that "
            "changed on disk is never skipped."
        ),
    ),
)


def _edit_flags(command: Any) -> Any:
    """Apply :data:`_EDIT_OPTIONS`, keeping their written order in ``--help``."""
    for option in reversed(_EDIT_OPTIONS):
        command = option(command)
    return command


@cli.group("edit", invoke_without_command=True)
@click.pass_context
def edit_command(ctx: click.Context) -> None:
    """Change the inventory: one typed, reversible, comment-preserving operation.

    Every subcommand is one operation from ``netgraph.edit``. It is applied to an
    in-memory copy of the tree, the tree is loaded and validated as it would be
    once written, and the files are only written if the edit introduces no new
    error -- so the inventory on disk stays loadable.

    Untouched lines are not rewritten: comments, blank lines, key order and
    quoting survive byte for byte, and ``--dry-run`` shows exactly the hunk that
    would change.

    With no subcommand, operations are read as JSON from stdin -- see
    'netgraph edit apply'.

    For the same operations from a browser, with a file tree, a live diagram and
    an undo stack, run 'netgraph web ./inventory --write'.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(edit_apply_command)


@edit_command.command("apply")
@click.option(
    "-f",
    "--file",
    "source",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Read the operations from this file instead of from stdin.",
)
@_edit_flags
@click.pass_obj
def edit_apply_command(
    app: AppContext, source: Path | None, dry_run: bool, as_json: bool, force: bool
) -> None:
    """Apply operations given as JSON, one object or a list of them.

    This is the programmatic face of the command, and the one the web editor and
    'netgraph plan' are described in terms of. Each object carries an "op" naming
    what it does and the keys that operation takes:

    \b
        [{"op": "set", "address": "core-sw", "path": "spec.model", "value": "C9300"},
         {"op": "connect", "a": "core-sw:Gi1/0/3", "b": "acc-sw:Gi1/0/1"}]

    The whole list is applied in order and judged as one change: an operation
    that is only valid once a later one has run is fine.
    """
    text = source.read_text(encoding="utf-8") if source is not None else sys.stdin.read()
    _run_edit(
        app,
        operations_from_json(text),
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("set")
@click.argument("address", shell_complete=complete_element)
@click.argument("path")
@click.argument("value")
@click.option(
    "--string",
    "as_string",
    is_flag=True,
    help="Take VALUE literally instead of reading it as YAML, so 1500 stays a string.",
)
@_edit_flags
@click.pass_obj
def edit_set_command(
    app: AppContext,
    address: str,
    path: str,
    value: str,
    as_string: bool,
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Set a field on an element: netgraph edit set core-sw spec.model 'C9300'.

    PATH is a field path -- ``spec.model``, ``spec.interfaces[2].mtu``,
    ``metadata.labels.site`` -- and the mappings on the way to it are created if
    they are not there. VALUE is read as a YAML scalar, so ``1500`` is a number,
    ``true`` is a boolean and ``[10, 20]`` is a list; ``--string`` turns that off.
    """
    _run_edit(
        app,
        [SetField(address=address, path=path, value=value if as_string else _scalar(value))],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("unset")
@click.argument("address", shell_complete=complete_element)
@click.argument("path")
@_edit_flags
@click.pass_obj
def edit_unset_command(
    app: AppContext, address: str, path: str, dry_run: bool, as_json: bool, force: bool
) -> None:
    """Remove a field from an element."""
    _run_edit(
        app,
        [UnsetField(address=address, path=path)],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("create")
@click.argument("kind", type=click.Choice(KINDS))
@click.argument("name")
@click.option(
    "--namespace",
    default="",
    help="Folder to declare it in, relative to the inventory root. The root by default.",
)
@click.option(
    "--spec",
    "spec_json",
    default="{}",
    show_default=True,
    help="The element's spec, as JSON.",
)
@click.option(
    "--metadata",
    "metadata_json",
    default="{}",
    show_default=True,
    help="Description, labels and annotations, as JSON.",
)
@click.option(
    "--file",
    "target",
    help=(
        "File to write it to, relative to the inventory root. Chosen by the layout "
        "conventions when absent."
    ),
)
@_edit_flags
@click.pass_obj
def edit_create_command(
    app: AppContext,
    kind: str,
    name: str,
    namespace: str,
    spec_json: str,
    metadata_json: str,
    target: str | None,
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Declare a new element.

    The document is checked against the schema of its KIND before anything is
    written, and it lands in the file that already holds elements of that kind in
    that namespace, or in a new one named by the conventions in
    docs/inventory-layout.md.
    """
    _run_edit(
        app,
        [
            CreateElement(
                kind=kind,
                name=name,
                namespace=namespace,
                spec=_json_object(spec_json, "--spec"),
                metadata=_json_object(metadata_json, "--metadata"),
                file=target,
            )
        ],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


#: The flags ``copy`` and ``duplicate`` share. ``duplicate`` is ``copy`` that
#: stays where it is, so it takes every one of these except ``--to``.
_COPY_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "--name",
        "new_name",
        default=None,
        help=(
            "metadata.name of the copy. Derived from the original's when absent; "
            "only meaningful when copying one element."
        ),
    ),
    click.option(
        "--suffix",
        default=DEFAULT_SUFFIX,
        show_default=True,
        help="What a derived name gets before its counter: sw1 -> sw1-copy -> sw1-copy-2.",
    ),
    click.option(
        "--keep-unique",
        is_flag=True,
        help=(
            "Keep the MAC addresses, fixed IP addresses, serials and outlets a copy "
            "normally drops. The result usually fails validation; use it when the copy "
            "is a starting point you are about to edit."
        ),
    ),
    click.option(
        "--view",
        default=None,
        type=click.Choice(LAYOUT_VIEWS),
        help=(
            "Place the copies in this view's stored geometry, offset from the originals. "
            "Nothing is written to a layout document when absent."
        ),
    ),
)


def _copy_flags(command: Any) -> Any:
    """Apply :data:`_COPY_OPTIONS`, keeping their written order in ``--help``."""
    for option in reversed(_COPY_OPTIONS):
        command = option(command)
    return command


@edit_command.command("copy")
@click.argument("address", shell_complete=complete_element)
@click.option(
    "--to",
    "namespace",
    default=None,
    help=(
        "Namespace to write the copies into; the folder each original is in by default. "
        "The empty string is the inventory root."
    ),
)
@_copy_flags
@_edit_flags
@click.pass_obj
def edit_copy_command(
    app: AppContext,
    address: str,
    namespace: str | None,
    new_name: str | None,
    suffix: str,
    keep_unique: bool,
    view: str | None,
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Copy an element, or a whole namespace, into a new document.

    ADDRESS is an element or a namespace. A namespace copies its subtree: every
    element below it lands under --to with the same shape, and the cables between
    them are cloned and rewired to the clones. A cable with only one end in the
    copied set is left behind and named, because a cable joining a clone to an
    original is a claim about the network nobody made.

    The copy is the original document, comments and all, with three changes: a
    free name, the references that point inside the copied set redirected to the
    copies, and the fields two elements cannot both have -- MAC addresses, fixed
    IP addresses, serials, PDU outlets -- dropped. See docs/editing.md for the
    whole table.
    """
    _run_copy(
        app,
        [address],
        namespace=namespace,
        name=new_name,
        suffix=suffix,
        keep_unique=keep_unique,
        view=view,
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("duplicate")
@click.argument("address", shell_complete=complete_element)
@_copy_flags
@_edit_flags
@click.pass_obj
def edit_duplicate_command(
    app: AppContext,
    address: str,
    new_name: str | None,
    suffix: str,
    keep_unique: bool,
    view: str | None,
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Copy an element into the namespace it is already in.

    'netgraph edit copy' with no --to, under the name a diagram editor gives it:
    it is the same operation, and Ctrl-D in 'netgraph web' does exactly this.
    """
    _run_copy(
        app,
        [address],
        namespace=None,
        name=new_name,
        suffix=suffix,
        keep_unique=keep_unique,
        view=view,
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


def _run_copy(
    app: AppContext,
    addresses: Sequence[str],
    *,
    namespace: str | None,
    name: str | None,
    suffix: str,
    keep_unique: bool,
    view: str | None,
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Plan a copy against the loaded tree, then apply it like any other edit.

    A step more than :func:`_run_edit` does, and it is the step that makes a copy
    a copy: which elements are in the set, what each one is called and which
    cables come with them are decided by
    :func:`~netgraph.edit.clipboard.copy_plan` against the *inventory*, and only
    then does the resulting list of operations go through the same two gates
    every other edit goes through.
    """
    console = app.console()
    root = app.inventory if app.inventory.is_dir() else app.inventory.parent
    session = EditSession(root=root, config=_edit_config(app), cache=app.cache())
    try:
        plan: CopyPlan = copy_plan(
            session.inventory,
            addresses,
            namespace=namespace,
            name=name,
            suffix=suffix,
            keep_unique=keep_unique,
            view=view,
        )
        session.apply_all(plan.operations)
    except EditError as exc:
        _report_edit_error(console, exc)
        raise click.exceptions.Exit(EXIT_INVALID) from exc
    for entry in plan.dropped:
        console.warn(f"not copied -- {entry}")
    _run_edit_session(
        app,
        session,
        dry_run=dry_run,
        as_json=as_json,
        force=force,
        note=_copy_note(plan),
    )


#: How many name pairs the copy summary spells out before it counts the rest.
#: A site copy is hundreds of them, and a terminal line nobody can read is not a
#: summary; the whole mapping is in ``--json``.
_COPY_LISTED: Final = 10


def _copy_note(plan: CopyPlan) -> str | None:
    """One line naming what the copy produced: the new names, or how many."""
    if not plan.mapping:
        return None
    pairs = [f"{source} -> {target}" for source, target in plan.mapping.items()]
    listed = ", ".join(pairs[:_COPY_LISTED])
    if len(pairs) > _COPY_LISTED:
        listed += f", and {len(pairs) - _COPY_LISTED} more"
    return f"copied {len(pairs)} element(s): {listed}"


@edit_command.command("delete")
@click.argument("address", shell_complete=complete_element)
@click.option(
    "--cascade",
    is_flag=True,
    help="Also delete the cables and tunnels that terminate on it, and the notes "
    "and areas that cannot be drawn without it, and clear the optional references "
    "to it.",
)
@_edit_flags
@click.pass_obj
def edit_delete_command(
    app: AppContext, address: str, cascade: bool, dry_run: bool, as_json: bool, force: bool
) -> None:
    """Remove an element, and the file if it was the last document in it.

    Refuses, and names them, when other documents depend on it. The diagram
    geometry that placed it is dropped either way: coordinates for something
    that is gone are not a dependency, and leaving them behind is what
    ``netgraph layout --prune`` used to have to clean up afterwards.
    """
    _run_edit(
        app,
        [DeleteElement(address=address, cascade=cascade)],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("rename")
@click.argument("address", shell_complete=complete_element)
@click.argument("new_name")
@_edit_flags
@click.pass_obj
def edit_rename_command(
    app: AppContext, address: str, new_name: str, dry_run: bool, as_json: bool, force: bool
) -> None:
    """Rename an element, and every reference to it across the tree.

    A reference keeps the spelling its author chose: a short name stays short
    where a short name still resolves, and a qualified one stays qualified.
    """
    _run_edit(
        app,
        [RenameElement(address=address, new_name=new_name)],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("move")
@click.argument("address", shell_complete=complete_element)
@click.argument("file")
@_edit_flags
@click.pass_obj
def edit_move_command(
    app: AppContext, address: str, file: str, dry_run: bool, as_json: bool, force: bool
) -> None:
    """Move an element's document to another file, verbatim.

    FILE is relative to the inventory root. Moving to another folder changes the
    element's namespace, and the references to it are rewritten with it.
    """
    _run_edit(
        app,
        [MoveElement(address=address, file=file)],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("connect")
@click.argument("a")
@click.argument("b")
@click.option(
    "--medium",
    type=click.Choice([medium.value for medium in Medium]),
    default=Medium.COPPER.value,
    show_default=True,
    help="What the link is made of.",
)
@click.option("--speed", help="Negotiated link rate, e.g. 1Gbps.")
@click.option("--label", help="The identifier printed on the cable.")
@click.option("--name", help="metadata.name of the cable; derived from the endpoints when absent.")
@click.option(
    "--namespace",
    default=None,
    help="Folder to declare it in. The nearest folder containing both ends by default.",
)
@click.option("--file", "target", help="File to write it to, relative to the inventory root.")
@_edit_flags
@click.pass_obj
def edit_connect_command(
    app: AppContext,
    a: str,
    b: str,
    medium: str,
    speed: str | None,
    label: str | None,
    name: str | None,
    namespace: str | None,
    target: str | None,
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Cable two interfaces together: netgraph edit connect sw1:Gi1/0/1 pc:eno1.

    Both ends are ``device:interface``, and both interfaces have to exist -- a
    cable to a port nothing declares is the mistake NG-C002 exists to catch.
    """
    spec: dict[str, Any] = {"medium": medium}
    if speed:
        spec["speed"] = speed
    if label:
        spec["label"] = label
    _run_edit(
        app,
        [Connect(a=a, b=b, spec=spec, name=name, namespace=namespace, file=target)],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("disconnect")
@click.argument("address", shell_complete=complete_element)
@click.option(
    "--cascade",
    is_flag=True,
    help="Also delete the notes and areas that cannot be drawn without the cable.",
)
@_edit_flags
@click.pass_obj
def edit_disconnect_command(
    app: AppContext, address: str, cascade: bool, dry_run: bool, as_json: bool, force: bool
) -> None:
    """Remove a cable. The devices it joined are untouched.

    Its waypoints and its label position go with it — a route for a link that
    is gone places nothing. A note anchored to it and placed nowhere else is a
    document §21 refuses without the anchor, so that one needs ``--cascade``.
    """
    _run_edit(
        app,
        [Disconnect(address=address, cascade=cascade)],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("add-interface")
@click.argument("address", shell_complete=complete_element)
@click.argument("name")
@click.option(
    "--type",
    "interface_type",
    default="ethernet",
    show_default=True,
    help="Interface type, as spec.interfaces[].type spells it.",
)
@click.option("--description", help="What the port is for.")
@click.option(
    "--field",
    "fields",
    multiple=True,
    metavar="PATH=VALUE",
    help="Any other key of the interface, e.g. --field mtu=9000. Repeatable.",
)
@_edit_flags
@click.pass_obj
def edit_add_interface_command(
    app: AppContext,
    address: str,
    name: str,
    interface_type: str,
    description: str | None,
    fields: tuple[str, ...],
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Add an interface to an element."""
    interface: dict[str, Any] = {"name": name, "type": interface_type}
    if description:
        interface["description"] = description
    for entry in fields:
        key, separator, value = entry.partition("=")
        if not separator:
            raise click.BadParameter(f"{entry!r} is not PATH=VALUE", param_hint="--field")
        interface[key] = _scalar(value)
    _run_edit(
        app,
        [AddInterface(address=address, interface=interface)],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


@edit_command.command("remove-interface")
@click.argument("address", shell_complete=complete_element)
@click.argument("name")
@click.option(
    "--cascade",
    is_flag=True,
    help="Also remove the cables and tunnels that terminate on the interface.",
)
@_edit_flags
@click.pass_obj
def edit_remove_interface_command(
    app: AppContext,
    address: str,
    name: str,
    cascade: bool,
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Remove an interface from an element.

    Only an interface the document itself declares: one that came from a
    ``range`` or from a template has to be removed where it was written.
    """
    _run_edit(
        app,
        [RemoveInterface(address=address, name=name, cascade=cascade)],
        dry_run=dry_run,
        as_json=as_json,
        force=force,
    )


def _scalar(text: str) -> Any:
    """A command-line value, read the way YAML would read it.

    ``1500`` is a number, ``true`` is a boolean, ``[10, 20]`` is a list and
    everything else is the string it looks like -- which is the same set of
    rules that decides what the value would have meant had it been typed into
    the document by hand. ``--string`` is there for the times that is wrong.
    """
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def _json_object(text: str, hint: str) -> dict[str, Any]:
    """Parse a JSON object given on the command line."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"not JSON: {exc}", param_hint=hint) from exc
    if not isinstance(value, dict):
        raise click.BadParameter(
            f"expected a JSON object, got {type(value).__name__}", param_hint=hint
        )
    return value


def _run_edit(
    app: AppContext,
    operations: Sequence[Operation],
    *,
    dry_run: bool,
    as_json: bool,
    force: bool,
) -> None:
    """Apply operations to the inventory and report what happened.

    One path for every subcommand, so ``--dry-run``, ``--json`` and the
    diagnostics read the same whichever operation produced them.
    """
    console = app.console()
    root = app.inventory if app.inventory.is_dir() else app.inventory.parent
    session = EditSession(root=root, config=_edit_config(app), cache=app.cache())
    try:
        session.apply_all(operations)
    except EditError as exc:
        _report_edit_error(console, exc)
        raise click.exceptions.Exit(EXIT_INVALID) from exc
    _run_edit_session(app, session, dry_run=dry_run, as_json=as_json, force=force)


def _run_edit_session(
    app: AppContext,
    session: EditSession,
    *,
    dry_run: bool,
    as_json: bool,
    force: bool,
    note: str | None = None,
) -> None:
    """Check, write and report a session whose operations have all been applied.

    Split out from :func:`_run_edit` so that ``apply`` — which builds its
    operations one changeset entry at a time, against the tree the previous
    entry left — reaches the disk through exactly the same two gates and prints
    exactly the same report.
    """
    console = app.console()
    try:
        if not session.changes:
            console.info("nothing to change")
            if as_json:
                console.print(json.dumps(session.summary().to_dict(), indent=2))
            return
        # Captured before the commit: writing makes the pending set empty, and
        # the report is about what was written.
        changes = dict(session.changes)
        diff = session.diff()
        if dry_run:
            problems = session.check()
            written: tuple[str, ...] = ()
        else:
            written = session.commit(force=force)
            problems = ()
    except EditError as exc:
        _report_edit_error(console, exc)
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    summary = session.summary(written=written, changes=changes)
    if as_json:
        console.print(json.dumps(summary.to_dict(), indent=2))
    elif dry_run:
        console.print(diff.rstrip("\n"))
    for problem in problems:
        console.warn(str(problem))
    for applied in summary.applied:
        console.info(applied.summary)
    if note is not None:
        console.info(note)
    verb = "would change" if dry_run else "changed"
    console.info(f"{verb} {len(summary.changes)} file(s): {', '.join(sorted(summary.changes))}")


def _edit_config(app: AppContext) -> Config | None:
    """``netgraph.toml``, when there is one worth reading.

    A configuration error must not stop an edit: the validation gate grades with
    the defaults instead, which is stricter than the file would be and therefore
    the safe way to be wrong.
    """
    try:
        return app.config()
    except ConfigurationError:
        return None


def _report_edit_error(console: Console, error: EditError) -> None:
    """One refusal, with whatever the caller needs to do about it."""
    console.error(str(error))
    if isinstance(error, CascadeRequired):
        for dependent in error.dependents:
            console.info(f"  {dependent}")
    elif isinstance(error, ValidationRefused):
        for problem in error.problems:
            console.info(f"  {problem}")
    elif isinstance(error, AddressError):
        for candidate in error.candidates:
            console.info(f"  {candidate}")


# --------------------------------------------------------------------------- #
# plan / apply
# --------------------------------------------------------------------------- #


@cli.command("plan")
@click.argument("inputs", nargs=-1, metavar="[NAME=]INPUT...")
@click.option(
    "--from",
    "source",
    metavar="REF|DIR",
    default=None,
    help=(
        "Take the current state from a git ref or another folder instead of from the "
        "inventory. A directory that exists is a folder; anything else is a git ref, "
        "exported read-only — the working tree is never touched."
    ),
)
@click.option(
    "--to",
    "target",
    metavar="REF|DIR",
    default=None,
    help="Take the desired state from a git ref or another folder. Defaults to the inventory.",
)
@click.option(
    "--from-live",
    "from_live",
    is_flag=True,
    default=False,
    help=(
        "Take the desired state from a live capture: the inventory as it would read if it "
        "agreed with what the network reports. Reads the same inputs 'netgraph import' and "
        "'netgraph drift' do."
    ),
)
@click.option(
    "--dialect",
    type=click.Choice(DIALECTS),
    default="auto",
    show_default=True,
    help="Input dialect for --from-live, as 'netgraph drift --from' takes it.",
)
@click.option(
    "--host",
    metavar="NAME",
    default=None,
    help="Device every --from-live input was captured on, when the input does not name it.",
)
@click.option(
    "--only",
    multiple=True,
    metavar="GLOB",
    shell_complete=complete_element,
    help="Adopt only elements whose name matches this glob (--from-live only). Repeatable.",
)
@click.option(
    "--exclude",
    "excluded",
    multiple=True,
    metavar="GLOB",
    shell_complete=complete_element,
    help="Leave elements matching this glob out of the adoption (--from-live only). Repeatable.",
)
@click.option(
    "--exclude-interface",
    "excluded_interfaces",
    multiple=True,
    metavar="GLOB",
    help="Leave interfaces matching this glob out of the adoption. Repeatable.",
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    metavar="ADDRESS",
    help=(
        "Keep only changes to elements this glob selects, matched against the address "
        "(device.core/sw-1), the qualified name or the short name. Repeatable."
    ),
)
@click.option(
    "--no-renames",
    "no_renames",
    is_flag=True,
    default=False,
    help="Report every rename as a delete and a create rather than detecting it.",
)
@click.option(
    "-F",
    "--output-format",
    type=click.Choice(PLAN_FORMATS),
    default="text",
    show_default=True,
    help="text is for reading; json is for CI and for a script.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Shorthand for '-F json'.")
@click.option(
    "-out",
    "--out",
    "out_path",
    metavar="FILE",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Write the plan to FILE so 'netgraph apply FILE' executes exactly what was reviewed. "
        "The file records a hash of the current state and apply refuses if the tree has moved on."
    ),
)
@click.option(
    "--fail-on",
    "fail_on",
    type=click.Choice(("never", "changes")),
    default="never",
    show_default=True,
    help="Exit 1 when the plan is not empty, so CI can gate on 'nothing to do'.",
)
@click.pass_obj
def plan_command(
    app: AppContext,
    inputs: tuple[str, ...],
    source: str | None,
    target: str | None,
    from_live: bool,
    dialect: str,
    host: str | None,
    only: tuple[str, ...],
    excluded: tuple[str, ...],
    excluded_interfaces: tuple[str, ...],
    targets: tuple[str, ...],
    no_renames: bool,
    output_format: str,
    as_json: bool,
    out_path: Path | None,
    fail_on: str,
) -> None:
    """Diff two inventory states and print an ordered changeset.

    The two sides come from wherever they can: a git ref against the working
    tree, two folders, or a live capture with --from-live. The result is a
    terraform-style plan — what is added, changed, renamed and destroyed, field
    by field — that 'netgraph apply' executes back onto the *files*.

    Nothing is written unless -out is given, and nothing is ever sent to a
    device: applying to the live network is out of scope.

    \b
    netgraph -i net plan --from HEAD
    netgraph -i net plan --to ../proposed --json
    netgraph -i net plan --from-live captures/*.json -out drift.plan
    netgraph -i net plan --fail-on changes --from origin/main
    """
    console = app.console()
    stream = console.to_stderr() if output_format != "text" or as_json else console
    root = app.inventory if app.inventory.is_dir() else app.inventory.parent

    if from_live and target is not None:
        raise click.UsageError("--from-live and --to both name the desired state; pick one")
    if not from_live and not inputs and source is None and target is None:
        raise click.UsageError(
            "nothing to compare against: give --from, --to, or --from-live with a capture"
        )
    if inputs and not from_live:
        raise click.UsageError("capture inputs are only read with --from-live")

    try:
        plan = _build_plan(
            app,
            root=root,
            source=source,
            target=target,
            from_live=from_live,
            inputs=inputs,
            dialect=dialect,
            host=host,
            spec=CompareSpec(only=only, exclude=excluded, ignore_interfaces=excluded_interfaces),
            renames=not no_renames,
            console=stream,
        )
    except (PlanSourceError, ImportSourceError, LoaderError) as exc:
        stream.error(str(exc))
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    plan = plan.select(targets)
    if targets and plan.empty:
        stream.warn(f"--target matched no change; {len(targets)} pattern(s) given")

    # Written before anything is printed: a reader that closes the pipe early
    # (``netgraph plan | head``) must not be able to lose the file that the
    # subsequent ``netgraph apply`` is about to be given.
    if out_path is not None:
        write_text(out_path, render_plan(plan, "json") + "\n")

    if output_format == "json" or as_json:
        console.print(render_plan(plan, "json"))
        stream.info(f"Plan: {summary_line(plan)}.")
    else:
        write_plan(console, plan, verbose=app.verbosity > 0)

    if out_path is not None:
        stream.info(f"plan written to {out_path}; apply it with 'netgraph apply {out_path}'")

    if fail_on == "changes" and not plan.empty:
        raise click.exceptions.Exit(EXIT_INVALID)


def _build_plan(
    app: AppContext,
    *,
    root: Path,
    source: str | None,
    target: str | None,
    from_live: bool,
    inputs: Sequence[str],
    dialect: str,
    host: str | None,
    spec: CompareSpec,
    renames: bool,
    console: Console,
) -> Plan:
    """Load both sides and diff them.

    The working tree is one side of every comparison unless both --from and --to
    name something else, which is what makes the common case — "what did I
    change?" — a single flag.
    """
    with ExitStack() as stack:
        current = _load_state(stack, app, root, source, fallback="the inventory")
        if from_live:
            return _live_plan(
                current,
                inputs=inputs,
                dialect=dialect,
                host=host,
                spec=spec,
                renames=renames,
                console=console,
                verbose=app.verbosity > 0,
            )
        desired = _load_state(stack, app, root, target, fallback="the inventory")
        _refuse_broken(console, current.inventory, "the current state")
        _refuse_broken(console, desired.inventory, "the desired state")
        return diff_states(
            current.inventory,
            desired.inventory,
            source=current.ref,
            target=desired.ref,
            renames=renames,
        )


@dataclass(frozen=True, slots=True)
class _State:
    """One side of a plan, loaded."""

    inventory: Inventory
    ref: StateRef


def _load_state(
    stack: ExitStack, app: AppContext, root: Path, spec: str | None, *, fallback: str
) -> _State:
    """Load the working tree, a folder, or a git ref, whichever ``spec`` names."""
    if spec is None:
        inventory = app.load()
        return _State(
            inventory=inventory,
            ref=StateRef(kind="tree", description=fallback, digest=state_digest(inventory)),
        )
    folder = Path(spec)
    if folder.is_dir():
        inventory = load_tree(folder)
        # ``display_path`` and not ``str``: this description is the first line
        # the plan prints and a field of the plan file, so a folder named
        # ``examples/home-lab`` has to read back that way on Windows too --
        # otherwise the same command against the same tree produces a different
        # document depending on who ran it.
        return _State(
            inventory=inventory,
            ref=StateRef(
                kind="folder", description=display_path(folder), digest=state_digest(inventory)
            ),
        )
    exported = stack.enter_context(git_ref(root, spec))
    inventory = load_tree(exported)
    # The root the export was loaded from is a temporary directory nobody will
    # ever open again, so the plan reports the ref instead.
    return _State(
        inventory=inventory,
        ref=StateRef(kind="git", description=spec, digest=state_digest(inventory)),
    )


def _live_plan(
    current: _State,
    *,
    inputs: Sequence[str],
    dialect: str,
    host: str | None,
    spec: CompareSpec,
    renames: bool,
    console: Console,
    verbose: bool,
) -> Plan:
    """The plan that adopts a capture into the declared inventory."""
    _refuse_broken(console, current.inventory, "the inventory")
    draft = build_draft(read_inputs(list(inputs), host=host, stdin=sys.stdin), dialect=dialect)
    adoption = adopt(current.inventory, draft, spec=spec)
    # What the capture was not entitled to change is the reason the plan is
    # shorter than a drift report; it is commentary, so it is behind -v with a
    # count in its place -- the same shape 'netgraph drift' reports it in.
    if adoption.unobserved:
        if verbose:
            for note in adoption.unobserved:
                console.info(f"  unobserved: {note}")
        else:
            console.info(
                f"{count_text(len(adoption.unobserved), 'declared item')} the capture could "
                f"not vouch for were left alone; -v lists them"
            )
    for problem in adoption.rejected:
        console.warn(f"could not adopt: {problem}")
    return diff_states(
        current.inventory,
        adoption.inventory,
        source=current.ref,
        target=StateRef(
            kind="live",
            description="the live network ("
            + (", ".join(sorted(set(draft.dialects.values()))) or "no input")
            + ")",
        ),
        renames=renames,
    )


def _refuse_broken(console: Console, inventory: Inventory, side: str) -> None:
    """A state that does not load cannot be planned against.

    A document that was rejected is absent from the inventory, so diffing
    against it would read as a deletion — the same reasoning ``drift`` applies,
    and with more at stake here because the answer is meant to be executed.
    """
    if not inventory.errors:
        return
    _report_problems(console, inventory.errors, ())
    raise PlanSourceError(
        f"refusing to plan: {side} does not load ({count_text(len(inventory.errors), 'error')})"
    )


@cli.command("apply")
@click.argument(
    "plan_file",
    metavar="PLAN",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    metavar="ADDRESS",
    help=(
        "Apply only the changes this glob selects, matched against the address "
        "(device.core/sw-1), the qualified name or the short name. Repeatable."
    ),
)
@click.option(
    "--auto-approve",
    is_flag=True,
    default=False,
    help="Do not ask before writing. For automation; a person should read the plan first.",
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write nothing; print the unified diff the plan would produce.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Write even if the result would introduce new validation errors. The state check "
        "is never skipped: a plan is only ever applied to the tree it was made from."
    ),
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Report as JSON.")
@click.pass_obj
def apply_command(
    app: AppContext,
    plan_file: Path,
    targets: tuple[str, ...],
    auto_approve: bool,
    dry_run: bool,
    force: bool,
    as_json: bool,
) -> None:
    """Execute a plan against the inventory *files*.

    Each entry becomes a 'netgraph edit' operation, so comments, key order and
    formatting survive, and the same validation gate applies: an edit that would
    introduce a new error is refused unless --force.

    Applying to the live network is deliberately out of scope. This command
    writes YAML and nothing else — it opens no session to a device, and there is
    no flag that makes it. The loop it closes runs the other way: adopt what the
    network reports into the declared inventory.

    \b
    netgraph -i net plan --from-live caps/*.json -out drift.plan
    netgraph -i net apply drift.plan
    netgraph -i net apply drift.plan --target 'device.core/*' --auto-approve
    """
    console = app.console()
    root = app.inventory if app.inventory.is_dir() else app.inventory.parent

    try:
        plan = plan_from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError) as exc:
        console.error(f"cannot read {plan_file}: {exc}")
        raise click.exceptions.Exit(EXIT_INVALID) from exc
    except (json.JSONDecodeError, PlanFormatError) as exc:
        console.error(f"{plan_file} is not a netgraph plan: {exc}")
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    session = EditSession(root=root, config=_edit_config(app), cache=app.cache())
    if not _state_matches(console, plan, session, plan_file=plan_file):
        raise click.exceptions.Exit(EXIT_INVALID)

    selected = plan.select(targets)
    if selected.empty:
        console.info("nothing to apply" if not targets else "--target selected no change")
        if as_json:
            console.print(json.dumps(session.summary().to_dict(), indent=2))
        return

    console.info(f"Plan: {summary_line(selected)}.")
    for change in selected:
        console.info(f"  {change.sigil} {change.headline}  [{change.kind}]")

    if not dry_run and not auto_approve and not _confirm(console, selected):
        console.info("aborted; nothing has been written")
        raise click.exceptions.Exit(EXIT_INVALID)

    try:
        applied = [operations for _, operations in translate(selected, session)]
    except (PlanExecutionError, EditError) as exc:
        if isinstance(exc, EditError):
            _report_edit_error(console, exc)
        else:
            console.error(str(exc))
        console.info("nothing has been written")
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    _run_edit_session(
        app,
        session,
        dry_run=dry_run,
        as_json=as_json,
        force=force,
        note=f"{sum(len(group) for group in applied)} operation(s) from {plan_file}",
    )


def _state_matches(console: Console, plan: Plan, session: EditSession, *, plan_file: Path) -> bool:
    """Is the tree still the one the plan was made from?"""
    expected = plan.source.digest
    if expected is None:
        console.error(
            f"{plan_file} records no source state, so it cannot be applied safely; "
            f"re-run 'netgraph plan' with -out"
        )
        return False
    if session.baseline.errors:
        _report_problems(console, session.baseline.errors, ())
        console.error("refusing to apply to an inventory that does not load")
        return False
    actual = state_digest(session.baseline)
    if actual == expected:
        return True
    console.error(
        f"{plan_file} was made against a different state of {session.root}; the tree has "
        f"changed since. Re-run 'netgraph plan' and review the new plan."
    )
    console.info(f"  plan expects {expected}")
    console.info(f"  tree is      {actual}")
    return False


def _confirm(console: Console, plan: Plan) -> bool:
    """Ask before writing. A closed stdin is a no, not a yes."""
    console.info("")
    try:
        return click.confirm(
            f"Apply these {len(plan)} change(s) to the inventory files?", default=False
        )
    except click.Abort:
        return False


# --------------------------------------------------------------------------- #
# converge
# --------------------------------------------------------------------------- #


@cli.group("converge")
def converge_group() -> None:
    """Turn drift into an ordered, reviewable remediation plan.

    'netgraph drift' says how the live network differs from the inventory and
    'netgraph export config' says what a device would run if it agreed. This
    joins them: per device, the minimal set of changes that would close every
    difference, in dependency order, each carrying the drift finding that asked
    for it and a risk classification.

    netgraph never applies any of it. There is no transport here and no flag
    that adds one: the command writes a plan and a set of scripts, and a person
    runs them. See docs/commands/converge.md.
    """


@converge_group.command("plan")
@click.argument("inputs", nargs=-1, metavar="[NAME=]INPUT...")
@click.option(
    "--from",
    "capture_dialect",
    type=click.Choice(DIALECTS),
    default="auto",
    show_default=True,
    help="Input dialect, exactly as 'netgraph drift --from' takes it.",
)
@click.option(
    "--host",
    metavar="NAME",
    default=None,
    help="Device every input was captured on, when the input does not name it.",
)
@click.option(
    "--dialect",
    type=click.Choice(CONVERGE_DIALECTS),
    default="interfaces",
    show_default=True,
    help=(
        "Which configuration dialect the commands are written in. 'interfaces' is "
        "netgraph's own imperative grammar and covers every device kind; the other five "
        "are declarative, so their remediation is the generated file plus a reload."
    ),
)
@click.option(
    "--only",
    multiple=True,
    metavar="GLOB",
    shell_complete=complete_element,
    help="Converge only elements whose name matches this glob. Repeatable.",
)
@click.option(
    "--exclude",
    "excluded",
    multiple=True,
    metavar="GLOB",
    shell_complete=complete_element,
    help="Leave elements whose name matches this glob out of the plan. Repeatable.",
)
@click.option(
    "--exclude-interface",
    "excluded_interfaces",
    multiple=True,
    metavar="GLOB",
    help="Leave interfaces whose name matches this glob out, as 'drift' takes it. Repeatable.",
)
@click.option(
    "--allow-disruptive",
    is_flag=True,
    default=False,
    help=(
        "Emit a plan even when a change touches the path a device is managed on. Without "
        "this the whole plan is refused and nothing is written."
    ),
)
@click.option(
    "--rollback",
    is_flag=True,
    default=False,
    help=(
        "Emit the inverse of every change: the commands that put each device back the way "
        "the capture found it. Affects the scripts written by --out."
    ),
)
@click.option(
    "-F",
    "--format",
    "output_format",
    type=click.Choice(CONVERGE_FORMATS),
    default="text",
    show_default=True,
    help="text is for reading, json for a script or a transport, markdown for a ticket.",
)
@click.option(
    "-o",
    "--out",
    "out_dir",
    metavar="DIR",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Write one .txt script per device under DIR, laid out like the inventory tree.",
)
@click.pass_obj
def converge_plan_command(
    app: AppContext,
    inputs: tuple[str, ...],
    capture_dialect: str,
    host: str | None,
    dialect: str,
    only: tuple[str, ...],
    excluded: tuple[str, ...],
    excluded_interfaces: tuple[str, ...],
    allow_disruptive: bool,
    rollback: bool,
    output_format: str,
    out_dir: Path | None,
) -> None:
    """Plan the changes that would bring the live network back to the inventory.

    Reads the same captures 'netgraph drift' does — no host is contacted and no
    credential is read — and writes, per device, the ordered commands that close
    every difference, with the drift finding behind each one.

    Exits 0 when the network already matches the inventory and 2 when changes
    are pending, mirroring 'netgraph plan'.

    \b
    netgraph -i net converge plan caps/*.json
    netgraph -i net converge plan --dialect netplan -o scripts/ caps/*.json
    netgraph -i net converge plan --rollback -o rollback/ caps/*.json
    """
    inventory = app.load()
    console = app.console()
    commentary = console.to_stderr() if output_format != "text" else console

    if inventory.errors:
        _report_problems(console.to_stderr(), inventory.errors, ())
        console.to_stderr().error(
            "refusing to plan against an inventory that does not load; a document that was "
            "rejected is absent from the comparison, which would read as drift to converge"
        )
        raise click.exceptions.Exit(EXIT_INVALID)

    try:
        plan = converge_network(
            inventory,
            list(inputs),
            dialect=dialect,
            capture_dialect=capture_dialect,
            host=host,
            spec=CompareSpec(only=only, exclude=excluded, ignore_interfaces=excluded_interfaces),
            allow_disruptive=allow_disruptive,
        )
    except DisruptiveChangeError as exc:
        console.to_stderr().error(str(exc))
        raise click.exceptions.Exit(exc.exit_code) from exc

    if output_format == "text":
        console.print(render_converge(plan, output_format).rstrip("\n"))
    else:
        console.print(render_converge(plan, output_format).rstrip("\n"))
        commentary.info(_converge_summary(plan))

    if out_dir is not None:
        written = write_converge_scripts(plan, out_dir, rollback=rollback)
        commentary.info(
            f"{len(written)} script(s) written under {out_dir}"
            if written
            else f"no script written under {out_dir}: every change needs a person, not a command"
        )
    elif rollback:
        commentary.warn(
            "--rollback affects the scripts '--out DIR' writes; without it the plan already "
            "carries every inverse command, and 'netgraph converge plan -F json' shows them"
        )

    if not plan.converged:
        raise click.exceptions.Exit(EXIT_CONVERGE_PENDING)


def _converge_summary(plan: ConvergePlan) -> str:
    """The one-line commentary printed beside a structured document."""
    if plan.converged:
        return "converged: the network already matches the inventory"
    disruptive = len(plan.disruptive)
    risk = f", {disruptive} disruptive" if disruptive else ""
    return (
        f"{_plural(len(plan.changes), 'change')} pending across "
        f"{_plural(len(plan.devices), 'element')}{risk}"
    )


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


def _resolve_theme(ctx: click.Context, param: click.Parameter, value: str | None) -> Theme | None:
    """``--theme``: a built-in theme name, ``none``, or a path to a theme file.

    Resolved in a callback for the same reason ``--icons`` is: a theme that does
    not exist, or one whose colours do not parse, is reported as a usage error
    with the option named, before an inventory is loaded.
    """
    try:
        return load_theme(value)
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
        "--annotations/--no-annotations",
        default=True,
        show_default=True,
        help=(
            "Draw the notes, areas and legends the inventory declares for this view. "
            "Turn them off for a diagram that should carry the topology and nothing "
            "written about it — a printed page for an audit — and leave them on for "
            "one that is being read rather than checked, where the callout is the "
            "reason the screenshot is worth attaching to the ticket."
        ),
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
        "--theme",
        callback=_resolve_theme,
        default=None,
        metavar="NAME|PATH",
        help=(
            "Apply a stylesheet: selectors by kind, name, namespace, role or label, each "
            f"mapping onto a style block. Built in: {', '.join(style_theme_choices())}. A path "
            "to a 'kind: theme' YAML file also works. An element's own spec.style still wins."
        ),
    ),
    click.option(
        "--style/--no-style",
        "styling",
        default=True,
        show_default=True,
        help=(
            "Honour the styles the inventory and the theme declare. --no-style draws the "
            "plain diagram from the built-in palette alone, which is the way to read a "
            "topology whose stylesheet is in the way. Icons are unaffected: use --icons none."
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
    click.option(
        "--routing",
        type=click.Choice(ROUTING_STYLES, case_sensitive=False),
        default=None,
        show_default="whatever the inventory's layout documents say, else spline",
        help=(
            "How links are drawn between the bends they are pinned through: 'spline' is "
            "the curve Graphviz draws, 'orthogonal' right angles, 'straight' segment to "
            "segment. A default: a link that pins a style of its own keeps it. Honoured "
            "by the Graphviz backends, the JSON export and the editor. 'netgraph layout "
            "--write' records it in the view it arranges, so the choice is the "
            "inventory's rather than the command line's from then on."
        ),
    ),
    click.option(
        "--avoid/--no-avoid",
        default=True,
        show_default="avoid",
        help=(
            "Route orthogonal links around the boxes they are not attached to instead of "
            "straight across them. Only applies to an arranged diagram drawn with "
            "'--routing orthogonal': a spline has nothing to route around, and an "
            "unarranged one is routed by Graphviz, which already avoids nodes. A bend you "
            "placed yourself is never moved — routing fills the segments between them. "
            "'--no-avoid' is the local Z-and-L every orthogonal diagram was drawn with "
            "before this existed."
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
        "l1 splices out; rack draws a front elevation per rack; power draws the PDUs and the "
        "feeds into everything they power; identity draws the users and groups; netns opens "
        "each machine up into the network stacks inside it, joined by their veth pairs; "
        "security draws the firewall zones and what the policy lets cross between them. "
        "Repeatable for -f html, which draws each layer and puts a switcher over them."
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


def _validation_options(command: _Command) -> _Command:
    """Apply :data:`_VALIDATION_OPTIONS` alone.

    For a command that reads the inventory and draws nothing: it still has to
    decide what to do about an inventory with errors, and it has no diagram to
    describe.
    """
    return _apply(_VALIDATION_OPTIONS, command)


def _report_flags(command: _Command) -> _Command:
    """Apply the options ``report`` shares: the filters, then the validation flags."""
    return _apply((*_FILTER_OPTIONS, *_VALIDATION_OPTIONS), command)


def _layout_flags(command: _Command) -> _Command:
    """Apply the options ``layout`` takes beyond its own.

    The display options are here because they are *inputs to the layout*: a
    label decides how big a node is, and node sizes decide where a layout puts
    them. The configuration options are here so ``netgraph.toml``'s ``[render]``
    reaches the seed as well as the render — seeding with one set of options and
    drawing with another is how an arrangement stops matching its diagram.

    The filter options are deliberately *not* here. An arrangement covers a
    view, and one seeded from three of a hundred devices would leave the other
    ninety-seven unplaced and the diagram permanently half-arranged.
    """
    return _apply((*_DISPLAY_OPTIONS, *_CONFIG_OPTIONS, *_EDIT_OPTIONS), command)


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
        annotations=params["annotations"],
        title=params["title"],
        max_addresses=params["max_addresses"],
        icons=params["icons"],
        theme=params.get("theme"),
        styling=params.get("styling", True),
        tooltips=params["tooltips"],
        link_template=params["link_template"],
        element_ids=params["element_ids"],
        rankdir=params["rankdir"],
        routing=None if params.get("routing") is None else Routing(params["routing"].lower()),
        avoid=params.get("avoid", True),
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
    if "theme" in ctx.params:
        # The ``[theme]`` table is not a *setting* — it is not one value with
        # a flag above it — so it is composed here, after the ladder has
        # picked which named theme is in force. Appending means it wins a tie
        # against that theme without having to restate what it agrees with.
        ctx.params["theme"] = resolve_theme(ctx.params["theme"], inline=config.theme)
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
    for graph in graphs:
        _report_routing(console, graph, options, output_format)
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


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


@cli.command("diff")
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
@click.option(
    "--against",
    metavar="REF|DIR",
    default=None,
    help=(
        "Draw the inventory against this git ref or folder: '--against HEAD' is "
        "'what have I changed since the last commit'. The same side as --from, spelled the "
        "way a diff reads."
    ),
)
@click.option(
    "--from",
    "source",
    metavar="REF|DIR",
    default=None,
    help=(
        "Take the state on the left of the diff from a git ref or another folder. A "
        "directory that exists is a folder; anything else is a git ref, exported read-only."
    ),
)
@click.option(
    "--to",
    "target",
    metavar="REF|DIR",
    default=None,
    help="Take the state on the right of the diff from a git ref or folder. Defaults to the "
    "inventory.",
)
@click.option(
    "--plan",
    "plan_file",
    metavar="FILE",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Draw the inventory against the state a saved plan would leave it in, without "
        "writing anything. The plan is checked against the tree exactly as 'netgraph apply' "
        "checks it."
    ),
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    metavar="ADDRESS",
    help=(
        "Mark only changes to elements this glob selects; the rest of the diagram is drawn "
        "untouched. Repeatable."
    ),
)
@click.option(
    "--no-renames",
    "no_renames",
    is_flag=True,
    default=False,
    help="Draw every rename as a deletion beside a creation rather than as one moved element.",
)
@_graph_options
@click.pass_context
def diff_command(
    ctx: click.Context,
    /,
    output_format: str,
    output: Path | None,
    against: str | None,
    source: str | None,
    target: str | None,
    plan_file: Path | None,
    targets: tuple[str, ...],
    no_renames: bool,
    **_options: Any,
) -> None:
    """Draw the difference between two inventory states as one diagram.

    Added elements and links are green, removed ones red and dashed but still
    in place, changed ones amber with a badge naming the fields that moved, and
    everything untouched is faded. It is 'netgraph plan' as a picture: the same
    changeset, drawn by the same renderers, so there is one notion of what
    changed and one of what the network looks like.

    A removed node keeps the position the layout document gave it, so a deletion
    does not reshuffle the diagram and hide itself in the churn.

    \b
    netgraph -i net diff --against HEAD -f svg -o change.svg
    netgraph -i net diff --from ../live --to . -f html -o review.html
    netgraph -i net diff --plan drift.plan -f json | jq .changeset
    """
    app: AppContext = ctx.obj
    params = ctx.params

    # stdout may be the diagram itself, so every diagnostic goes to stderr.
    console = app.console(err=True)
    resolutions = _apply_settings(ctx)
    if params["show_config"]:
        _print_settings(app.console(), resolutions, config=app.config(), command="diff")
        return
    output_format = params["output_format"]
    if not supports_diff(output_format):
        raise click.UsageError(
            f"{output_format} output has no way to say what changed — it can colour nothing "
            f"and hold no changeset beside the graph; render the diff as "
            f"{', '.join(diff_formats())}"
        )

    if against is not None:
        if source is not None:
            raise click.UsageError("--against and --from name the same side of the diff; pick one")
        source = against
    if plan_file is not None and (source is not None or target is not None):
        raise click.UsageError(
            "--plan already says what the desired state is; --from and --to name another"
        )
    if plan_file is None and source is None and target is None:
        raise click.UsageError(
            "nothing to compare against: give --against, --from, --to, or --plan"
        )

    root = app.inventory if app.inventory.is_dir() else app.inventory.parent
    force = bool(params["force"])
    with ExitStack() as stack:
        try:
            plan, before, after = _diff_states(
                stack,
                app,
                root=root,
                source=source,
                target=target,
                plan_file=plan_file,
                renames=not no_renames,
                console=console,
                force=force,
            )
        except (PlanSourceError, LoaderError) as exc:
            console.error(str(exc))
            raise click.exceptions.Exit(EXIT_INVALID) from exc

        if targets:
            plan = plan.select(targets)
            if plan.empty:
                console.warn(f"--target matched no change; {len(targets)} pattern(s) given")

        layer = _single_layer(params, output_format)
        spec, aggregate = _filter_spec(params), _aggregate_spec(params)
        drawing = draw(
            plan,
            _build_graph(app, before, layer=layer, spec=spec, aggregate=aggregate),
            _build_graph(app, after, layer=layer, spec=spec, aggregate=aggregate, console=console),
        )

    if targets:
        # ``--target`` narrows the *marks*, not the graph. Presence in the two
        # drawings is what says "added" and "removed", and it knows nothing
        # about the patterns — so the overlay is narrowed to the addresses the
        # selected changeset covers, and everything else is drawn untouched.
        drawing = replace(drawing, overlay=drawing.overlay.narrowed(_addresses_in(plan)))

    options = replace(_render_options(params), diff=drawing.overlay)
    _report_icon_support(console, output_format, options)
    _report_interaction_support(ctx, console, output_format, options)
    _report_advisories(
        console, output_format, nodes=len(drawing.graph.nodes), edges=len(drawing.graph.edges)
    )
    _report_routing(console, drawing.graph, options, output_format)
    payload = render(drawing.graph, output_format, options)
    _write_output(
        payload, output=output, binary=is_binary_format(output_format), what=output_format
    )

    if plan.empty:
        console.info("nothing changed; the whole diagram is drawn untouched")
    elif drawing.is_empty:
        console.warn(
            f"{count_text(len(plan), 'change')} to draw, but none of them is visible at layer "
            f"{layer}; try another --layer, or read 'netgraph plan'"
        )
    else:
        console.info(f"diff at layer {layer}: {drawing.overlay.summary()}")


def _addresses_in(plan: Plan) -> set[str]:
    """Every fully-qualified name a changeset touches, both sides of a rename."""
    names = {change.address.fqn for change in plan}
    names |= {change.new_address.fqn for change in plan if change.new_address is not None}
    return names


def _single_layer(params: Mapping[str, Any], output_format: str) -> Layer:
    """The one layer a diff draws.

    A diff is a comparison of one view. Two views would need two overlays, and
    one output holding both would have to say which of them each mark belonged
    to — so this refuses rather than merging marks that were computed against
    different drawings.

    Raises:
        click.UsageError: More than one ``--layer`` was given.
    """
    # Counted before ``_layers`` runs, so that the answer does not depend on the
    # format: ``-f html`` holds several layers and would otherwise be told it
    # could have them.
    chosen = tuple(dict.fromkeys(params["layers"]))
    if len(chosen) > 1:
        raise click.UsageError(
            f"--layer was given {len(chosen)} times; a diff compares one view of the network, "
            f"so draw each layer as its own diff"
        )
    return _layers(params, output_format)[0]


def _diff_states(
    stack: ExitStack,
    app: AppContext,
    *,
    root: Path,
    source: str | None,
    target: str | None,
    plan_file: Path | None,
    renames: bool,
    console: Console,
    force: bool,
) -> tuple[Plan, Inventory, Inventory]:
    """The changeset and the two inventories behind it.

    Raises:
        PlanSourceError: A side cannot be read, or does not load and --force was
            not given.
        click.exceptions.Exit: ``--plan`` names something that is not a plan, or
            one the tree has moved on from.
    """
    if plan_file is not None:
        return _planned_states(app, root=root, plan_file=plan_file, console=console)
    current = _load_state(stack, app, root, source, fallback="the inventory")
    desired = _load_state(stack, app, root, target, fallback="the inventory")
    for side, what in ((current, "the state on the left"), (desired, "the state on the right")):
        if force:
            # ``render --force`` draws an inventory with errors on the grounds
            # that a broken picture beats no picture; a diff of two broken
            # states is the same bargain, and says so once per side.
            if side.inventory.errors:
                console.warn(f"{what} does not load; drawing it anyway (--force)")
        else:
            _refuse_broken(console, side.inventory, what)
    return (
        diff_states(
            current.inventory,
            desired.inventory,
            source=current.ref,
            target=desired.ref,
            renames=renames,
        ),
        current.inventory,
        desired.inventory,
    )


def _planned_states(
    app: AppContext, *, root: Path, plan_file: Path, console: Console
) -> tuple[Plan, Inventory, Inventory]:
    """The tree, and the tree as ``plan_file`` would leave it — without writing.

    The plan is executed into an :class:`~netgraph.edit.EditSession` that is
    never committed, so what is drawn on the right is the *same* text
    ``netgraph apply`` would write, produced by the same operations. Drawing a
    reconstruction instead would be a second answer to a question the mutation
    layer already answers.
    """
    try:
        plan = plan_from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError) as exc:
        console.error(f"cannot read {plan_file}: {exc}")
        raise click.exceptions.Exit(EXIT_INVALID) from exc
    except (json.JSONDecodeError, PlanFormatError) as exc:
        console.error(f"{plan_file} is not a netgraph plan: {exc}")
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    session = EditSession(root=root, config=_edit_config(app), cache=app.cache())
    if not _state_matches(console, plan, session, plan_file=plan_file):
        raise click.exceptions.Exit(EXIT_INVALID)
    before = session.baseline
    try:
        for _ in translate(plan, session):
            pass
    except (PlanExecutionError, EditError) as exc:
        if isinstance(exc, EditError):
            _report_edit_error(console, exc)
        else:
            console.error(str(exc))
        console.info("nothing has been written")
        raise click.exceptions.Exit(EXIT_INVALID) from exc
    return plan, before, session.inventory


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
            "'spec.routes', 'spec.vrfs', 'spec.route_tables' or 'spec.routing_policy'. "
            "Render '--layer l3' for the addressing the inventory does declare, or add a "
            "'routing' block to a router"
        )
    if layer is Layer.RACK:
        return (
            "nothing to draw at layer rack: no element declares 'metadata.location' with a "
            "'rack' and a 'position'. Add one to place it on an elevation"
        )
    if layer is Layer.POWER:
        return (
            "nothing to draw in the power view: the inventory declares no 'pdu' and no "
            "'spec.power'. Add a pdu document and name its outlets in a device's "
            "'power.inputs', or run 'netgraph list power' to see what is recorded"
        )
    if layer is Layer.NETNS:
        return (
            "nothing to draw in the netns view: no device declares 'spec.netns' or a veth "
            "pair. This view opens a machine up; a machine with one network stack is drawn "
            "at every other layer already"
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


#: The formats a route reaches through Graphviz, and so the ones a route that
#: cannot be honoured is worth warning about. Mermaid has one edge shape and
#: JSON publishes the arrangement verbatim, so neither can lose anything.
_ROUTED_FORMATS: Final[frozenset[str]] = frozenset({"dot", "html", *IMAGE_FORMATS})


def _report_routing(
    console: Console, graph: Graph, options: RenderOptions, output_format: str
) -> None:
    """Say what this rendering could not honour about the routes it was given.

    A pinned bend in a half-arranged diagram, or an orthogonal layout carrying
    edge labels, are both cases Graphviz accepts and then quietly does something
    else with. Each one names the command that fixes it — see
    :func:`~netgraph.render.dot.routing_advisories`.
    """
    if output_format not in _ROUTED_FORMATS:
        return
    for advisory in routing_advisories(graph, options):
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
        raise _unknown_element(exc) from exc

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


def _unknown_element(exc: UnknownElementError) -> click.BadParameter:
    """The usage error for a ``--neighbors-of`` that names nothing.

    One wording for every command that filters a graph, so a typo is explained
    the same way whether it was typed at ``render`` or at ``report``.
    """
    hint = (
        f" Did you mean one of: {', '.join(exc.candidates)}?"
        if exc.candidates
        else " Run 'netgraph list devices' to see what is declared."
    )
    return click.BadParameter(
        f"no element named {exc.name!r} in this inventory.{hint}",
        param_hint="'--neighbors-of'",
    )


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
# netgraph review
# --------------------------------------------------------------------------- #


@cli.command("review")
@click.option(
    "--from",
    "source",
    metavar="REF|DIR",
    required=True,
    help=(
        "The state before the change: a git ref (origin/main) or a folder. This is the "
        "baseline every finding is measured against, so a problem it already had is not "
        "reported as one this change introduced."
    ),
)
@click.option(
    "--to",
    "target",
    metavar="REF|DIR",
    default=None,
    help="The state after the change. Defaults to the inventory as it is on disk.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the comment body here instead of to stdout.",
)
@click.option(
    "--plan-out",
    metavar="FILE",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Also write the changeset as JSON, the document 'netgraph plan --json' writes.",
)
@click.option(
    "--summary-out",
    metavar="FILE",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help=(
        "Also write the verdict and the counts as a small JSON document, so a workflow "
        "can gate on them without reading the comment's prose."
    ),
)
@click.option(
    "--sarif-out",
    metavar="FILE",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help=(
        "Also write the head state's diagnostics as SARIF, for a code-scanning upload. It "
        "is the same validation the comment reports, so the two cannot disagree."
    ),
)
@click.option(
    "--diagram",
    "diagram_paths",
    multiple=True,
    metavar="[LABEL=]PATH",
    help=(
        "A rendering of the visual diff to link, as 'netgraph diff -o' wrote it. The label "
        "defaults to the suffix, upper-cased. Repeatable."
    ),
)
@click.option(
    "--diagram-url",
    "diagram_urls",
    multiple=True,
    metavar="[LABEL=]URL",
    help=(
        "Where a rendering is published, if it is somewhere a browser can fetch it. Those "
        "are embedded in the comment; a --diagram with no URL is only linked. Repeatable."
    ),
)
@click.option(
    "--artifact-url",
    metavar="URL",
    default=None,
    help="Where the whole bundle can be downloaded -- in CI, the run that produced it.",
)
@click.option(
    "--artifact-name",
    metavar="NAME",
    default=None,
    help="What that bundle is called, for the link text.",
)
@click.option(
    "--repository-url",
    metavar="URL",
    default=None,
    help=(
        "Base URL of the repository, https://github.com/owner/repo. With --head-sha it "
        "turns every finding into a permalink to the line it is anchored at."
    ),
)
@click.option(
    "--head-sha",
    metavar="SHA",
    default=None,
    help="Commit the head state is, so the links are permalinks rather than branch links.",
)
@click.option(
    "--title",
    default="netgraph",
    show_default=True,
    metavar="TEXT",
    help=(
        "Heading of the comment, and the key of its sticky marker. Two inventories reviewed "
        "in one repository need two titles, or they will overwrite each other's comment."
    ),
)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Promote every warning to an error, on both sides of the comparison.",
)
@click.option(
    "--disable",
    "disabled",
    multiple=True,
    metavar="RULE",
    shell_complete=complete_rule,
    help="Silence a rule by id, on both sides. Repeatable.",
)
@click.option(
    "--no-renames",
    is_flag=True,
    default=False,
    help="Report every rename as a deletion beside a creation rather than as one move.",
)
@click.option(
    "--max-changes",
    type=click.IntRange(1),
    default=40,
    show_default=True,
    metavar="N",
    help="How many elements the changeset table names before it says how many are left.",
)
@click.option(
    "--max-findings",
    type=click.IntRange(1),
    default=25,
    show_default=True,
    metavar="N",
    help="The same, for the table of new findings.",
)
@click.option(
    "--fail-on",
    type=click.Choice(("new-errors", "new-findings", "changes", "never")),
    default="new-errors",
    show_default=True,
    help=(
        "What exits 1. 'new-errors' is the one a pull-request check should use: an error "
        "the base already had is not this change's to fix, and gating on it would mean no "
        "repository with a legacy finding could ever adopt the check."
    ),
)
@click.pass_obj
def review_command(
    app: AppContext,
    source: str,
    target: str | None,
    output: Path | None,
    plan_out: Path | None,
    summary_out: Path | None,
    sarif_out: Path | None,
    diagram_paths: tuple[str, ...],
    diagram_urls: tuple[str, ...],
    artifact_url: str | None,
    artifact_name: str | None,
    repository_url: str | None,
    head_sha: str | None,
    title: str,
    strict: bool,
    disabled: tuple[str, ...],
    no_renames: bool,
    max_changes: int,
    max_findings: int,
    fail_on: str,
) -> None:
    """Write up what a change does to the network, as one review comment.

    The Markdown body on stdout is what the pull-request bot posts: a verdict, a
    table of what is added, changed and removed grouped by kind, the validation
    findings this change *introduced* -- measured against --from, so a legacy
    warning is not reported as new -- and the diff drawn.

    Only new problems fail it. Run it locally before you push, or from the
    composite action in .github/actions/netgraph-review; see docs/ci.md.

    \b
    netgraph -i net review --from origin/main
    netgraph -i net review --from HEAD --diagram diff.svg -o comment.md
    netgraph -i net review --from origin/main --sarif-out head.sarif --fail-on never
    """
    console = app.console()
    stream = console.to_stderr() if output is None else console
    root = app.inventory if app.inventory.is_dir() else app.inventory.parent

    diagrams = _review_diagrams(diagram_paths, diagram_urls)
    settings = app.config().validation.with_overrides(
        strict=True if strict else None, ignore=disabled
    )
    # Provenance is what turns a finding into a line number, and a review whose
    # findings pointed at whole documents would be markedly less useful than the
    # SARIF it is written beside.
    inventory = app.load(keep_provenance=True) if target is None else None

    try:
        result = build_review(
            root=root,
            base=source,
            head=target,
            head_inventory=inventory,
            config=settings,
            renames=not no_renames,
            diagrams=diagrams,
            artifact_url=artifact_url,
            artifact_name=artifact_name,
            repository_url=repository_url,
            head_sha=head_sha,
            title=title,
            strict=strict,
            max_changes=max_changes,
            max_findings=max_findings,
        )
    except (PlanSourceError, LoaderError) as exc:
        stream.error(str(exc))
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    # Written before anything is printed, and before the exit below: the file a
    # workflow uploads must exist whatever the verdict was, or the one run that
    # had something to report is the one that reports nothing.
    if plan_out is not None and result.plan is not None:
        write_text(plan_out, render_plan(result.plan, "json") + "\n")
    if sarif_out is not None:
        write_text(sarif_out, render_diagnostics(result.head, "sarif") + "\n")
    if summary_out is not None:
        write_text(summary_out, dump_json(summarise(result.review)) + "\n")

    body = render_comment(result.review)
    if output is not None:
        write_text(output, body)
        stream.info(f"review written to {display_path(output)}")
    else:
        console.print(body.rstrip("\n"))

    stream.info(_review_summary(result.review))
    if _review_fails(result.review, fail_on):
        raise click.exceptions.Exit(EXIT_INVALID)


def _review_diagrams(paths: Sequence[str], urls: Sequence[str]) -> tuple[ReviewDiagram, ...]:
    """Merge ``--diagram`` and ``--diagram-url`` into one list, keyed by label.

    Both spellings take an optional ``LABEL=`` so that the same drawing given as
    a path and as a URL is one entry: without a key they would be two, and the
    comment would link a PNG it had already embedded.

    Raises:
        click.UsageError: A value is empty, which is always a quoting mistake.
    """
    order: list[str] = []
    found: dict[str, tuple[str | None, str | None]] = {}

    def record(value: str, *, is_url: bool, flag: str) -> None:
        label, separator, rest = value.partition("=")
        # A Windows path is ``C:\...`` and a URL is ``https://...``; neither is
        # a label, so a one-character key and a key holding a scheme are read as
        # part of the value rather than as a name for it.
        if not separator or len(label) < 2 or "/" in label or "\\" in label:
            label, rest = "", value
        if not rest:
            raise click.UsageError(f"{flag} was given an empty value")
        name = label or _diagram_label(rest)
        if name not in found:
            order.append(name)
            found[name] = (None, None)
        path, url = found[name]
        found[name] = (rest if not is_url else path, rest if is_url else url)

    for value in paths:
        record(value, is_url=False, flag="--diagram")
    for value in urls:
        record(value, is_url=True, flag="--diagram-url")
    return tuple(
        ReviewDiagram(label=name, path=found[name][0], url=found[name][1]) for name in order
    )


def _diagram_label(value: str) -> str:
    """``diff.svg`` -> ``SVG``; anything with no suffix keeps its own name."""
    suffix = PurePosixPath(value.replace("\\", "/")).suffix.lstrip(".")
    return suffix.upper() if suffix else "diagram"


def _review_summary(review: Review) -> str:
    """The line printed to stderr beside the body, for whoever is watching."""
    delta = review.delta
    if review.plan is None:
        return f"review: the head state does not load; {count_text(len(delta.new), 'new finding')}"
    changed = count_text(len(review.plan), "change") if review.changed else "no change"
    if not delta.new:
        return f"review: {changed}, nothing newly wrong"
    return f"review: {changed}, {count_text(len(delta.new), 'new finding')}"


def _review_fails(review: Review, fail_on: str) -> bool:
    """Does ``--fail-on`` say this review exits 1?"""
    if fail_on == "never":
        return False
    if review.verdict is Verdict.BROKEN:
        return True
    if fail_on == "changes":
        return review.changed
    if fail_on == "new-findings":
        return bool(review.delta.new)
    return bool(review.delta.new_errors)


# --------------------------------------------------------------------------- #
# netgraph log
# --------------------------------------------------------------------------- #


@cli.command("log")
@click.option(
    "--from",
    "source",
    metavar="REV",
    default=None,
    help=(
        "Oldest revision to list, exclusive — as 'git log a..b' means it. The revision itself "
        "is the state the oldest listed commit is drawn against, so it is not listed."
    ),
)
@click.option(
    "--to",
    "target",
    metavar="REV",
    default="HEAD",
    show_default=True,
    help="Newest revision to list, inclusive.",
)
@click.option(
    "-n",
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="List at most this many commits, newest first. 'netgraph log -n 1' is the last change.",
)
@click.option(
    "--max-revisions",
    "max_revisions",
    type=click.IntRange(min=1),
    default=None,
    show_default="[history] max-revisions in netgraph.toml, or 100",
    help=(
        "Refuse a range holding more revisions than this rather than reading them all. "
        "A limit narrows the range; this bounds what may be asked for."
    ),
)
@click.option(
    "--summary/--no-summary",
    "with_summary",
    default=True,
    show_default=True,
    help=(
        "Say what each commit did to the network, which means loading the inventory on both "
        "sides of it. --no-summary lists the commits alone and reads nothing."
    ),
)
@click.option(
    "--no-renames",
    "no_renames",
    is_flag=True,
    default=False,
    help="Count every rename as a removal and an addition rather than detecting it.",
)
@click.option(
    "-F",
    "--output-format",
    type=click.Choice(("text", "json")),
    default="text",
    show_default=True,
    help="text is for reading; json is for a script.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Shorthand for '-F json'.")
@click.pass_obj
def log_command(
    app: AppContext,
    source: str | None,
    target: str,
    limit: int,
    max_revisions: int | None,
    with_summary: bool,
    no_renames: bool,
    output_format: str,
    as_json: bool,
) -> None:
    """List the commits that changed the inventory, and what each one changed.

    'git log' for the network rather than for the files: only commits that
    touched the inventory directory are listed, and each carries a one-line
    summary of its changeset — devices added, links removed, addresses moved —
    computed by the same code 'netgraph plan' and 'netgraph diff' use.

    Every revision is read out of the object database. The working tree, the
    index and the checked-out branch are never touched.

    \b
    netgraph -i net log
    netgraph -i net log --from v1.0 --to v2.0 --json
    netgraph -i net log -n 1 --no-summary
    """
    console = app.console()
    stream = console.to_stderr() if output_format == "json" or as_json else console
    root = app.inventory if app.inventory.is_dir() else app.inventory.parent
    bound = max_revisions if max_revisions is not None else app.config().history.max_revisions

    try:
        timeline = Timeline.open(root, max_revisions=bound)
        commits = timeline.commits(since=source, until=target, limit=limit)
    except HistoryError as exc:
        stream.error(str(exc))
        raise click.exceptions.Exit(EXIT_INVALID) from exc

    # Walked in the order they were listed, so the cache does its job: each
    # commit's parent state is the next commit's own state, and a linear history
    # is read once rather than twice.
    frames = list(timeline.frames(commits, renames=not no_renames)) if with_summary else []

    if output_format == "json" or as_json:
        listed: list[dict[str, Any]] = (
            [frame.to_dict() for frame in frames]
            if with_summary
            else [commit.to_dict() for commit in commits]
        )
        console.print(
            dump_json(
                {
                    "root": str(root),
                    "range": {"from": source, "to": target},
                    "maxRevisions": bound,
                    "commits": listed,
                }
            )
        )
    else:
        _write_log(console, commits, frames)
    stream.info(
        f"{count_text(len(commits), 'revision')} of {root}"
        + (f", newest first, of at most {bound}" if len(commits) >= bound else "")
    )


def _write_log(console: Console, commits: Sequence[Commit], frames: Sequence[Frame]) -> None:
    """Print one block per commit: the header line, then what it did.

    Two lines rather than a table, because the subject is the widest column and
    the one worth reading in full — a table would either truncate it or push the
    summary off the right of an 80-column terminal. ``frames`` is empty under
    ``--no-summary``, and then only the header line is printed.
    """
    if not commits:
        console.print(console.dim("no commit has touched this inventory"))
        return
    width = max(len(commit.author) for commit in commits)
    summaries = {frame.commit.hash: frame for frame in frames}
    for commit in commits:
        console.print(
            f"{console.style(commit.abbrev, fg='yellow')}  {commit.date}  "
            f"{commit.author.ljust(width)}  {console.bold(commit.subject)}"
        )
        frame = summaries.get(commit.hash)
        if frame is None:
            continue
        indent = " " * (len(commit.abbrev) + 2)
        if frame.ok:
            console.print(f"{indent}{console.dim(frame.summary)}")
        else:
            console.print(f"{indent}{console.style('! ' + frame.summary, fg='red')}")


# --------------------------------------------------------------------------- #
# netgraph layout
# --------------------------------------------------------------------------- #


@cli.command("layout")
@click.option(
    "--layer",
    "layers",
    multiple=True,
    type=click.Choice([layer.value for layer in Layer]),
    default=(Layer.L1.value,),
    show_default=True,
    shell_complete=complete_layer,
    help="Which view to arrange. Repeatable; each view is arranged separately.",
)
@click.option(
    "--engine",
    type=click.Choice(LAYOUT_ENGINES),
    default="dot",
    show_default=True,
    help=(
        "Graphviz engine to lay the diagram out with when seeding. dot is the hierarchical "
        "layout netgraph draws with; circo suits a ring, fdp and neato a flat mesh."
    ),
)
@click.option(
    "--write",
    is_flag=True,
    default=False,
    help="Run the layout once and store the result, making the arrangement editable.",
)
@click.option(
    "--clear",
    is_flag=True,
    default=False,
    help="Drop the stored arrangement, so the view is laid out from scratch again.",
)
@click.option(
    "--replace",
    "replace_all",
    is_flag=True,
    default=False,
    help=(
        "With --write, lay every node out afresh instead of keeping what is already "
        "arranged and placing only the rest."
    ),
)
@click.option(
    "--prune",
    is_flag=True,
    default=False,
    help="Drop geometry for elements the inventory no longer declares.",
)
@click.option(
    "--waypoints",
    is_flag=True,
    default=False,
    help=(
        "Also store the bends each link is routed through, and the sizes of the nodes "
        "they leave from. Off by default: the render recomputes an equivalent route from "
        "the node positions, and a handful of points per link is a lot of noise. Turn it "
        "on to get a starting point that can then be dragged."
    ),
)
@click.option(
    "--name",
    "layout_name",
    default=DEFAULT_LAYOUT_NAME,
    show_default=True,
    metavar="NAME",
    help="metadata.name of the layout document to write into or create.",
)
@click.option(
    "--namespace",
    "layout_namespace",
    default="",
    metavar="PATH",
    help="Folder to declare the layout document in. The inventory root by default.",
)
@click.option(
    "--file",
    "target",
    default=None,
    metavar="PATH",
    help=(
        "File to write a new layout document to, relative to the inventory root. Chosen by "
        "the layout conventions when absent."
    ),
)
@_layout_flags
@click.pass_context
def layout_command(
    ctx: click.Context,
    /,
    layers: tuple[str, ...],
    engine: str,
    write: bool,
    clear: bool,
    replace_all: bool,
    prune: bool,
    waypoints: bool,
    routing: str | None,
    layout_name: str,
    layout_namespace: str,
    target: str | None,
    dry_run: bool,
    as_json: bool,
    force: bool,
    **_options: Any,
) -> None:
    """Seed and maintain the stored arrangement of a diagram.

    A diagram that Graphviz lays out afresh on every render cannot be arranged:
    drag a switch and the next keystroke puts it back. This command turns the
    automatic layout into *data* -- a 'kind: layout' document holding a position
    per node, scoped by view -- after which the arrangement is the source of
    truth and a render reproduces it exactly.

    \b
        netgraph layout                     what is arranged, and what is stale
        netgraph layout --write             place what is not placed yet
        netgraph layout --write --replace   lay every node out afresh
        netgraph layout --write --engine circo   ... with a different engine
        netgraph layout --prune             drop geometry for deleted elements
        netgraph layout --clear             go back to laying it out from scratch

    Writes go through the same path as 'netgraph edit', so comments and
    formatting in a hand-arranged file survive, --dry-run shows the exact hunk,
    and the tree is validated before anything is written.

    The display options matter when seeding: a label decides how big a node is,
    and how big the nodes are decides where the layout puts them. Seed with the
    options you render with -- or put them in netgraph.toml, which this command
    reads too.
    """
    app: AppContext = ctx.obj
    params = ctx.params
    console = app.console(err=True)

    chosen = [flag for flag, given in (("--write", write), ("--clear", clear)) if given]
    if len(chosen) > 1:
        raise click.UsageError(f"{' and '.join(chosen)} ask for opposite things; pick one")
    if clear and prune:
        raise click.UsageError("--clear already removes everything --prune would")
    if replace_all and not write:
        raise click.UsageError("--replace says how to --write; it does nothing on its own")

    _apply_settings(ctx)
    options = _render_options(params)
    views = views_for(layers)

    inventory = app.load()
    _report_load_errors(console, inventory, force=force)

    if clear:
        _run_layout_edit(
            app, clear_operations(views, inventory=inventory), dry_run=dry_run, force=force
        )
        return

    drawings = [(view, build_graph(inventory, layer=Layer(view))) for view in views]
    if write:
        operations = [
            operation
            for view, graph in drawings
            for operation in write_operations(
                seed_geometry(
                    graph,
                    options,
                    engine=engine,
                    replace_all=replace_all,
                    with_waypoints=waypoints,
                ),
                layout=layout_name,
                namespace=layout_namespace,
                file=target,
                with_waypoints=waypoints,
                routing=routing,
            )
        ]
        _run_layout_edit(app, operations, dry_run=dry_run, force=force)
        return

    if prune:
        live = {view: live_keys(graph, options) for view, graph in drawings}
        _run_layout_edit(app, prune_operations(inventory, live=live), dry_run=dry_run, force=force)
        return

    report = inspect_layout(
        inventory,
        # Against the *unnarrowed* arrangement: ``build_graph`` drops geometry
        # for nodes the drawing does not have, which is exactly the geometry
        # this report exists to point at.
        [
            (view, live_keys(graph, options), resolve_geometry(inventory, view))
            for view, graph in drawings
        ],
    )
    if as_json:
        app.console().print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_layout_report(app.console(), report)


def _report_load_errors(console: Console, inventory: Inventory, *, force: bool) -> None:
    """Refuse to arrange a tree that does not load, unless told to anyway.

    An arrangement of a broken inventory is an arrangement of whatever survived
    the errors, and writing one would quietly bake a half-loaded diagram into
    the tree.

    Raises:
        click.exceptions.Exit: The inventory has load errors and ``force`` is off.
    """
    if not inventory.errors:
        return
    _report_problems(console, inventory.errors, (), commentary=True)
    if not force:
        console.error(
            "refusing to arrange an inventory that does not load; fix the errors, or pass "
            "--force to arrange what did load"
        )
        raise click.exceptions.Exit(EXIT_INVALID)
    console.warn("arranging despite errors (--force): the geometry may not match the network")


def _run_layout_edit(
    app: AppContext, operations: Sequence[Operation], *, dry_run: bool, force: bool
) -> None:
    """Apply geometry operations, or say there were none to apply."""
    if not operations:
        app.console().info("nothing to change")
        return
    _run_edit(app, operations, dry_run=dry_run, as_json=False, force=force)


#: How many stale geometry keys a warning names before it says "and N more".
_STALE_SHOWN: Final = 8


def _print_layout_report(console: Console, report: LayoutReport) -> None:
    """What is arranged, per view, and what the arrangement has left behind."""
    if not report.documents:
        console.info("no layout document in this inventory; 'netgraph layout --write' seeds one")
    else:
        console.info(f"layout documents: {', '.join(report.documents)}")
    console.table(
        ("VIEW", "MODE", "NODES", "EDGES", "GROUPS", "STALE"),
        [
            [
                view.view,
                str(view.mode),
                f"{view.placed}/{view.nodes}",
                f"{view.routed}/{view.edges}",
                f"{view.boxed}/{view.groups}",
                str(len(view.stale)) if view.stale else "-",
            ]
            for view in report.views
        ],
    )
    for view, section, key, layout in report.conflicts:
        console.warn(
            f"{layout} also places {key!r} in the {view} view's {section}; "
            f"the first document to declare it wins"
        )
    if report.stale:
        shown = ", ".join(report.stale[:_STALE_SHOWN])
        rest = len(report.stale) - _STALE_SHOWN
        console.warn(
            f"{count_text(len(report.stale), 'stale entry', 'stale entries')} name nothing "
            f"this inventory draws: {shown}{f' and {rest} more' if rest > 0 else ''}. "
            "Run 'netgraph layout --prune' to drop them."
        )


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


@cli.command("impact")
@click.option(
    "--fail",
    "failed",
    multiple=True,
    metavar="ELEMENT",
    shell_complete=complete_element,
    help=(
        "Remove this element and report what breaks. Takes a name, a "
        "fully-qualified name, or a kind-qualified one such as 'device/sw1' or "
        "'cable/rack1-a3'. Repeatable: several failures are simulated together."
    ),
)
@click.option(
    "--from",
    "anchors",
    multiple=True,
    metavar="ELEMENT",
    shell_complete=complete_element,
    help=("Measure reachability from here instead of from the designated gateways. Repeatable."),
)
@click.option(
    "--path",
    "paths",
    multiple=True,
    metavar="SRC=DST",
    help=(
        "Re-run this trace on both sides of the failure and report whether it survived. "
        "Repeatable. Each end takes the spellings 'netgraph path' takes."
    ),
)
@click.option(
    "--spof",
    is_flag=True,
    default=False,
    help=(
        "Enumerate the single points of failure instead of simulating one, ranked by how "
        "many endpoints each isolates. The default when no --fail is given."
    ),
)
@click.option(
    "--redundancy",
    is_flag=True,
    default=False,
    help=(
        "Check the redundancy expectations elements declare in their "
        "'netgraph/redundancy' annotation, and report E047, E048 and W141 through the "
        "ordinary rule machinery."
    ),
)
@click.option(
    "--layer",
    "layers_wanted",
    multiple=True,
    type=click.Choice(IMPACT_LAYERS),
    help="Which views to analyse. Repeatable; all three by default.",
)
@click.option(
    "--limit",
    type=click.IntRange(0),
    default=DEFAULT_LIMIT,
    show_default=True,
    metavar="N",
    help="Report at most this many single points of failure. 0 reports every one of them.",
)
@click.option(
    "--min-isolated",
    "minimum",
    type=click.IntRange(1),
    default=1,
    show_default=True,
    metavar="N",
    help="Ignore a single point of failure that isolates fewer endpoints than this.",
)
@click.option(
    "-F",
    "--output-format",
    "report_format",
    type=click.Choice(IMPACT_FORMATS),
    default="text",
    show_default=True,
    help=(
        "text is the report; json is the whole analysis for tooling. sarif and github "
        "carry the --redundancy findings alone, for a code-scanning upload."
    ),
)
@click.option(
    "--fail-on",
    type=click.Choice(("impact", "none")),
    default="impact",
    show_default=True,
    help=(
        "Exit 1 when something was isolated, a checked path broke or a declared expectation "
        "was not met, or never. Enumerating single points of failure never fails the run."
    ),
)
@_validation_options
@click.pass_obj
def impact_command(
    app: AppContext,
    /,
    failed: tuple[str, ...],
    anchors: tuple[str, ...],
    paths: tuple[str, ...],
    spof: bool,
    redundancy: bool,
    layers_wanted: tuple[str, ...],
    limit: int,
    minimum: int,
    report_format: str,
    fail_on: str,
    strict: bool,
    force: bool,
) -> None:
    """Simulate failures: what breaks, what is a single point of failure.

    \b
    netgraph impact --spof
    netgraph impact --fail device/sw1 --fail cable/rack1-a3
    netgraph impact --fail pdu/pdu-r1-a --path pc-alice=srv-web
    netgraph impact --redundancy -F github

    With --fail, the named elements are removed from the inventory, every layer
    is derived again without them, and the report says per layer which elements
    stop being reachable from the designated gateways, which namespaces are
    partitioned, and which of the paths named with --path no longer exist. A PDU
    that fails takes everything it solely feeds with it, transitively, so a
    switch that sources PoE for six access points is reported as losing all
    seven.

    With --spof, nothing is removed: every articulation point and every bridge of
    every layer is enumerated in one pass, ranked by how many endpoints it
    isolates, and a device whose only power feed is one PDU is reported beside
    them — redundant cabling does not survive a single power source.

    With --redundancy, the expectations elements declare in their
    'netgraph/redundancy' annotation are graded by E047, E048 and W141, which is
    what lets CI fail a change that quietly removed somebody's second path.

    Exits 1 when a simulated failure isolated something, a checked path broke, or
    a declared expectation was not met. Enumerating single points of failure does
    not fail the run: every network of any size has them.
    """
    console = app.console()
    notes = console.to_stderr() if report_format != "text" else console
    wanted_spof = spof or not (failed or redundancy)
    if report_format in DIAGNOSTIC_ONLY_FORMATS and not redundancy:
        raise click.UsageError(
            f"-F {report_format} carries findings about files, which only --redundancy "
            f"produces; use -F json for the analysis."
        )

    inventory = app.load()
    findings = _run_validation(app, inventory, strict=strict)
    if _is_rejected(inventory, findings):
        _report_problems(notes, inventory.errors, findings)
        if not force:
            notes.error(
                "refusing to simulate failures on an inventory with errors; a dangling cable "
                "is exactly the kind of thing that makes a blast radius wrong. Fix them, or "
                "pass --force"
            )
            raise click.exceptions.Exit(EXIT_INVALID)
        notes.warn("analysing despite errors (--force): the answer may not match the inventory")

    try:
        report = run_impact(
            inventory,
            fail=failed,
            anchors=anchors,
            paths=paths,
            wanted_layers=layers_wanted or IMPACT_LAYERS,
            spof=wanted_spof,
            redundancy=redundancy,
            limit=limit,
            minimum=minimum,
        )
    except ImpactError as exc:
        hint = "'--fail'" if failed else "'--from' / '--path'"
        if exc.candidates:
            notes.info("candidates: " + ", ".join(exc.candidates[:_MAX_CANDIDATES]))
        raise click.BadParameter(str(exc), param_hint=hint) from exc

    if report_format in DIAGNOSTIC_ONLY_FORMATS:
        document = render_diagnostics(build_diagnostics(inventory, report.findings), report_format)
        if document:
            console.print(document)
    else:
        console.print(render_impact(report, report_format).rstrip("\n"))

    if fail_on == "impact" and report.impacted:
        raise click.exceptions.Exit(EXIT_INVALID)


# --------------------------------------------------------------------------- #
# test
# --------------------------------------------------------------------------- #


@cli.command("test")
@click.argument("suites", nargs=-1, metavar="[SUITE]...", shell_complete=complete_test_suite)
@click.option(
    "-F",
    "--output-format",
    "report_format",
    type=click.Choice(TEST_FORMATS),
    default="text",
    show_default=True,
    help=(
        "text is the progress report; json is the whole run for a script; junit is the XML "
        "GitHub, GitLab and Jenkins all render as a test report."
    ),
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the report to this file instead of stdout. Usual for -F junit.",
)
@click.option(
    "--max-hops",
    type=click.IntRange(1, 64),
    default=DEFAULT_MAX_HOPS,
    show_default=True,
    help="Abandon a traced route that crosses more links than this, unless the assertion "
    "sets its own 'max_hops'.",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    default=False,
    help="List the suites and their assertions without grading any of them.",
)
@_validation_options
@click.pass_obj
def test_command(
    app: AppContext,
    /,
    suites: tuple[str, ...],
    report_format: str,
    output: Path | None,
    max_hops: int,
    list_only: bool,
    strict: bool,
    force: bool,
) -> None:
    """Grade the assertions the inventory declares in its 'kind: testsuite' documents.

    \b
    netgraph test
    netgraph test connectivity segmentation
    netgraph test -F junit -o test-results.xml

    An assertion says what somebody is relying on — that the tills reach the
    payment gateway, that the guest VLAN reaches nothing else, that no
    management address is used twice — and every one of them is checked against
    the same graphs 'netgraph render' draws and the same search 'netgraph path'
    runs. Nothing is probed and no device is contacted.

    A failure names the assertion, the elements it is about, what the graph
    actually contained, and the file and line the assertion is written on, so an
    editor and a CI annotation can both link straight to it.

    SUITE narrows the run to the suites whose name matches; a glob that matches
    nothing fails the run rather than quietly grading zero assertions.

    Exits 1 when an assertion failed. A skipped assertion does not fail the run.
    """
    console = app.console()
    notes = console.to_stderr() if report_format != "text" or output is not None else console

    inventory = app.load(keep_provenance=True)
    findings = _run_validation(app, inventory, strict=strict)
    if _is_rejected(inventory, findings):
        _report_problems(notes, inventory.errors, findings, commentary=True)
        if not force:
            notes.error(
                "refusing to test an inventory with errors: a dangling cable is exactly the "
                "kind of thing that makes an assertion pass for the wrong reason. Fix them, "
                "or pass --force"
            )
            raise click.exceptions.Exit(EXIT_INVALID)
        notes.warn("testing despite errors (--force): a verdict may not match the inventory")

    if list_only:
        _list_test_suites(console, inventory, suites)
        return

    report = run_tests(inventory, names=suites, max_hops=max_hops)
    document = render_test_report(report, report_format, verbose=app.verbosity > 0)
    if output is None:
        console.print(document.rstrip("\n"))
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        write_text(output, document if document.endswith("\n") else document + "\n")
        notes.info(f"wrote {report.total} assertion(s) to {output}")

    if not report.ok:
        raise click.exceptions.Exit(EXIT_INVALID)


def _list_test_suites(console: Console, inventory: Inventory, wanted: Sequence[str]) -> None:
    """Print what would be graded, without grading it.

    The point is to be able to answer "what does this inventory actually check?"
    without waiting for a thousand-device tree to be traced — and to see the
    file and line of every assertion, which is what somebody editing one needs.
    """
    matched = False
    for fqn, suite in inventory.test_suites.items():
        if wanted and not any(
            fnmatch.fnmatchcase(fqn, pattern) or fnmatch.fnmatchcase(short_name(fqn), pattern)
            for pattern in wanted
        ):
            continue
        matched = True
        described = f"  ({suite.spec.description})" if suite.spec.description else ""
        console.print(f"{fqn}  {_plural(len(suite.assertions), 'assertion')}{described}")
        for index, assertion in enumerate(suite.assertions):
            where = suite_location(inventory, fqn, ("spec", "assertions", index))
            console.print(f"  {assertion.title}  [{assertion.type}]  {where}")
    if not matched:
        console.print("no test suite matches" if wanted else "no 'kind: testsuite' document here")


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
    # Spelled out rather than left to Click, because the value is per-platform
    # (see netgraph.watch.loop) and ``show_default=True`` would make --help, and
    # the generated flag table in docs/commands/watch.md, say something different
    # on each one -- which reads as documentation drift rather than as a fact
    # about the filesystem-event backends.
    show_default="300 on Linux, 700 on macOS and Windows",
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
        # One store for the whole run, so the second cycle onwards re-parses
        # only what the editor actually saved.
        cache=app.cache(),
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
@click.option(
    "--theme",
    callback=_resolve_theme,
    default=None,
    metavar="NAME|PATH",
    help=(
        "Apply a stylesheet to the diagram (§22). The style inspector shows the resolved "
        f"appearance and which layer each value came from. Built in: "
        f"{', '.join(style_theme_choices())}. Chosen here rather than in the browser, "
        "because it names a file on this machine."
    ),
)
@click.option(
    "--write/--read-only",
    "write",
    default=False,
    show_default="--read-only",
    help=(
        "Let the browser change the inventory. Only for a SOURCE folder, only on a "
        "loopback bind, and never by default: an editor that can write is a decision."
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
    theme: Theme | None,
    write: bool,
    profile: str | None,
    show_config: bool,
) -> None:
    """Edit an inventory in a browser and see it drawn as you go.

    A SOURCE *folder* opens an editing session: the server holds the tree, the
    page lists its files and the documents in them, selecting a node in the
    diagram reveals the document that declares it, and a problem in the list
    navigates to its file and line. Add --write and the browser can save, undo
    and redo -- every change through the same layer `netgraph edit` writes with,
    so comments, quoting and formatting survive it. Edits made in $EDITOR or by
    `git checkout` are noticed and reach the open page.

    Anything else -- a file, a pipe, or no SOURCE at all -- opens the scratchpad
    instead: one YAML document stream, held in the browser, rendered as you
    type, written nowhere. It is the command for a snippet or a paste. With no
    SOURCE it opens on the example topology `netgraph init` writes. Note that a
    stream has no folders and therefore no namespaces.

    Either way the diagram is the same one `netgraph render` draws, and hovering
    a node or a link opens an info box with the detail the picture has no room
    for: every interface, its addresses and VLANs, and what it is cabled to.

    --write is refused unless the server is bound to loopback: publishing a
    write endpoint with --host is not something a flag should let you do by
    accident.

    Render defaults and --profile are read from the netgraph.toml of the
    inventory named by -i, the current directory by default; a session also
    reads the [validate] table of the folder it has open.

    Press Ctrl-C to stop.
    """
    app: AppContext = ctx.obj
    console = app.console(err=True)

    resolutions = _apply_settings(ctx)
    if show_config:
        _print_settings(app.console(), resolutions, config=app.config(), command="web")
        return
    icons = ctx.params["icons"]
    theme = resolve_theme(ctx.params["theme"], inline=app.config().theme)

    session = _web_session(app, source, write=write, host=host, icons=icons)
    text = "" if session is not None else _web_source(console, source)
    exposure = describe_exposure(host, subject="the web interface")
    if exposure is not None:
        console.warn(exposure)
    if find_dot() is None:
        console.warn(
            f"the Graphviz {DOT_EXECUTABLE!r} executable was not found; the page will load "
            f"but every render will report that it cannot draw anything. "
            f"Install it ({graphviz_install_hint()}), or set {DOT_ENV_VAR} to its full path"
        )

    server = WebServer.create(
        source=text,
        session=session,
        icons=icons,
        theme=theme,
        host=host,
        port=port,
        log=lambda message: app.log(f"web: {message}", level=2),
        on_render=lambda preview: app.log(
            f"{preview.status}: {preview.message} ({preview.duration * 1000:.0f} ms)", level=1
        ),
    ).start()
    watcher = _web_watcher(app, session)
    if session is None:
        console.info(f"editing at {server.url}; press Ctrl-C to stop")
    else:
        console.info(
            f"{'editing' if write else 'browsing'} {session.root} at {server.url} "
            f"({'read-write' if write else 'read-only'}); press Ctrl-C to stop"
        )
    if open_browser:
        _open_browser(app, server.url)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        # Ctrl-C is how this command is meant to end, not a failure.
        pass
    finally:
        if watcher is not None:
            watcher.stop()
        server.stop()
    console.info("web interface stopped")


def _web_session(
    app: AppContext,
    source: Path | None,
    *,
    write: bool,
    host: str,
    icons: IconTheme | None,
) -> EditingSession | None:
    """The tree this run edits, or ``None`` for the document-stream scratchpad.

    A folder is a tree and everything else is a stream. That is the whole of the
    decision, and it is made here rather than in the server so that ``--write``
    can be refused with the reason before anything binds a port.

    Raises:
        click.BadParameter: ``--write`` was asked for where it cannot be given —
            over a stream, which has no files, or on a bind that is not loopback.
    """
    if source is None or not source.is_dir():
        if write:
            raise click.BadParameter(
                "--write edits files, and a document stream has none. Give a folder: "
                "'netgraph web ./inventory --write'.",
                param_hint="'--write'",
            )
        return None
    if write and not is_loopback(host):
        raise click.BadParameter(
            f"--write on {host} would publish an endpoint that changes this inventory to "
            f"anyone who can reach this machine. Bind loopback (the default) to edit, or "
            f"drop --write to publish a read-only session.",
            param_hint="'--write'",
        )
    app.log(f"opening {source} as an editing session ({'read-write' if write else 'read-only'})")
    return EditingSession(
        root=source,
        writable=write,
        icons=icons,
        cache=app.cache(),
    )


def _web_watcher(app: AppContext, session: EditingSession | None) -> TreeWatcher | None:
    """Watch the session's folder, so an edit made elsewhere reaches the page.

    The scratchpad has no folder to watch. A watch that cannot start — no
    ``watchfiles`` wheel, a filesystem that delivers no events — is reported and
    then lived with: the editor still works, it just stops noticing changes it
    did not make itself, and saying so is better than a page that is quietly
    stale.
    """
    if session is None:
        return None
    console = app.console(err=True)
    return TreeWatcher(
        session,
        on_change=lambda batch: app.log(f"changed on disk: {', '.join(sorted(batch))}", level=1),
        on_error=lambda message: console.warn(
            f"the inventory is no longer being watched ({message}); changes made outside "
            f"this editor will not reach the page until it is reloaded"
        ),
    ).start()


def _web_source(console: Console, source: Path | None) -> str:
    """The document stream the scratchpad opens with.

    Only reached when there is no folder to open as a session: a file, a pipe,
    or nothing. A pipe wins over the rest, because ``netgraph render -f dot |
    ...`` taught users that netgraph reads stdin when it is not a terminal, and
    a stream is what the scratchpad edits.
    """
    del console  # kept for symmetry with the session opener, which does warn
    if source is None and not _is_a_terminal(sys.stdin):
        return sys.stdin.read()
    if source is None:
        return _example_stream()
    if str(source) == "-":  # pragma: no cover - click resolves '-' to a path first
        return sys.stdin.read()
    return source.read_text(encoding="utf-8-sig")


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
# lsp
# --------------------------------------------------------------------------- #


@cli.command("lsp")
@click.option(
    "--stdio",
    "stdio",
    is_flag=True,
    default=True,
    help="Speak the protocol over stdin and stdout. The only transport; accepted "
    "because most clients pass it.",
)
@click.option(
    "--watch/--no-watch",
    "watch",
    default=True,
    help="Watch the folder, so an edit made outside the editor refreshes diagnostics.",
)
@click.option(
    "--log",
    "log_path",
    metavar="FILE",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Append a trace of what the server did to FILE. Never stderr: some "
    "clients treat anything there as a crash.",
)
@click.pass_obj
def lsp_command(app: AppContext, stdio: bool, watch: bool, log_path: Path | None) -> None:
    """Serve inventory YAML to an editor over the Language Server Protocol.

    Reads JSON-RPC from stdin and writes it to stdout, so it is started by the
    editor rather than by you. ``docs/lsp.md`` has the configuration for VS Code
    and Neovim, and ``schema/netgraph.schema.json`` covers the editors that want
    a JSON Schema instead.

    The inventory is whatever the client says it opened; ``-i`` is only the
    fallback for a client that opens a lone file, and in that case the checks
    that need the rest of the tree are held back rather than reported against a
    file that cannot satisfy them.

    Everything it answers comes from the commands you already run: ``validate``
    for the diagnostics, ``fmt`` for formatting, ``validate --fix`` for the quick
    fixes, ``edit rename`` for renaming, and the JSON Schema for completion.
    """
    from netgraph.lsp import Connection, serve

    del stdio  # There is one transport; the flag exists so clients may pass it.
    # Binary streams, because the protocol frames by *byte* count and Python's
    # text layer would translate newlines on Windows and desynchronise the
    # stream on the first frame.
    # ``getattr`` rather than ``.buffer``: a harness that replaced the streams
    # with text buffers should meet a protocol error rather than an
    # ``AttributeError`` from inside an argument list.
    reader = cast("IO[bytes]", getattr(sys.stdin, "buffer", sys.stdin))
    writer = cast("IO[bytes]", getattr(sys.stdout, "buffer", sys.stdout))
    with _lsp_log(log_path) as log:
        code = serve(
            Connection(reader, writer),
            root=app.inventory if app.inventory.is_dir() else None,
            watch=watch,
            log=log,
        )
    _exit_from_stdio(code)


def _exit_from_stdio(code: int) -> None:
    """End the process now, without waiting for the reader thread.

    The thread that frames the client's stream is parked in a blocking read on
    stdin, and only the client can end that read. A client that has sent ``exit``
    is entitled to leave the pipe open — several do — so a normal interpreter
    shutdown would find a thread inside a buffered read and abort the process
    with ``_enter_buffered_busy``. The editor then reports a crash immediately
    after a clean shutdown, which is both alarming and false.

    Everything that had to be written has been: every response is flushed as it
    is framed, and the log file was closed by the caller's ``with``. So the
    honest thing left to do is stop.
    """
    for stream in (sys.stdout, sys.stderr):
        with suppress(ValueError, OSError):
            stream.flush()
    os._exit(code)


@contextmanager
def _lsp_log(path: Path | None) -> Iterator[Callable[[str], None] | None]:
    """A line-per-message log, or nothing at all.

    Opened for the life of the session and flushed on every line: a log that
    only appears once the server exits is no use for diagnosing a server that
    did not.
    """
    if path is None:
        yield None
        return
    with path.open("a", encoding="utf-8") as handle:

        def write(message: str) -> None:
            handle.write(f"{message}\n")
            handle.flush()

        yield write


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


@cli.command("list")
@click.argument(
    "what",
    type=click.Choice(LISTING_SUBJECTS),
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
    """List the devices, cables, tunnels, VLANs, BSSs, subnets, PDUs or identities."""
    console = app.console()
    inventory = app.load()
    _warn_about_load_errors(console, inventory)

    listing = LISTINGS[what](inventory)
    if output_format == "table":
        console.table(
            listing.headers, listing.rows, aligns=listing.aligns, empty=f"no {what} declared"
        )
    else:
        console.print(_serialise(listing.records, output_format).rstrip("\n"))


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
    """The utilisation table, from the shared column set, coloured for a terminal.

    The columns and the cells are :func:`netgraph.listing.utilisation` — the same
    ones a report's address plan carries. All this adds is the colour, which is
    the one part that has no meaning outside a terminal.
    """
    listing = utilisation_listing(rows, aggregated=aggregated)
    util = listing.headers.index("UTIL")
    for cells, row in zip(listing.rows, rows, strict=True):
        cells[util] = _utilisation_cell(console, row)
    console.table(
        listing.headers, listing.rows, aligns=listing.aligns, empty="no addresses declared"
    )


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

#: How ``export power`` lays the load schedule out. Not the same choice as
#: :data:`TABLE_FORMATS`, and deliberately a separate flag: a schedule's second
#: form carries the per-PDU totals a sheet has no row for, so the two options
#: offer different things and folding them into one would advertise ``markdown``
#: for a schedule and ``json`` for a pull list, neither of which exists.
SCHEDULE_FORMATS: Final[tuple[str, ...]] = ("csv", "json")

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
    "schedule_format": ("--schedule-format", ("power",)),
    "out_dir": ("--out", CONFIG_FORMATS),
    "view": ("--view", ("drawio",)),
    "icon_theme_option": ("--icons", ("drawio",)),
    "compress": ("--compress", ("drawio",)),
    "frames": ("--frames", ("drawio",)),
    "annotations": ("--annotations", ("drawio",)),
}

#: What ``export drawio`` draws nodes as when nobody says. The shipped theme
#: rather than plain boxes: the file is going to be opened by somebody who was
#: sent it, and a page of grey rectangles is not a network diagram.
DEFAULT_DRAWIO_ICONS: Final = "cisco"


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
    click.option(
        "--schedule-format",
        type=click.Choice(SCHEDULE_FORMATS),
        default="csv",
        show_default=True,
        help=(
            "How the power load schedule is laid out. json adds the per-PDU and per-PSE "
            "totals; the feed rows are the same either way."
        ),
    ),
)

#: What ``export drawio`` settles. A ``.drawio`` file is a *picture*, so it takes
#: the two things a picture needs and no other command's format does: which view
#: it draws, and what the nodes are drawn as.
_DRAWIO_OPTIONS: Final[tuple[Callable[[Any], Any], ...]] = (
    click.option(
        "--view",
        type=click.Choice([layer.value for layer in Layer]),
        default=Layer.L1.value,
        show_default=True,
        shell_complete=complete_layer,
        help=(
            "Which view the drawio diagram draws. Unlike the other formats this one is a "
            "picture, and the arrangement it opens with is the one stored for that view."
        ),
    ),
    click.option(
        "--icons",
        "icon_theme_option",
        default=DEFAULT_DRAWIO_ICONS,
        show_default=True,
        callback=_resolve_icons,
        metavar="THEME|DIR",
        help=(
            "Icon theme inlined into the drawio file as data URIs, so the file needs nothing "
            f"beside it. Built in: {', '.join(theme_choices())}. 'none' draws coloured boxes."
        ),
    ),
    click.option(
        "--theme",
        callback=_resolve_theme,
        default=None,
        metavar="NAME|PATH",
        help=(
            "Apply a stylesheet to the exported diagram (§22). mxGraph spells netgraph's "
            "style vocabulary almost one for one, so a colour chosen here opens in draw.io "
            f"as that colour and survives a round-trip. Built in: {', '.join(style_theme_choices())}."
        ),
    ),
    click.option(
        "--style/--no-style",
        "styling",
        default=True,
        show_default=True,
        help="Honour the styles the inventory and the theme declare. --no-style exports plain.",
    ),
    click.option(
        "--compress/--no-compress",
        "compress",
        default=False,
        show_default=True,
        help=(
            "Write the deflate+base64 encoding draw.io writes by default. Off here: a plain "
            "diagram is one that reviews and diffs, and draw.io opens both."
        ),
    ),
    click.option(
        "--frames/--no-frames",
        "frames",
        default=True,
        show_default=True,
        help="Draw a container frame per namespace, so dragging a site carries its devices.",
    ),
    click.option(
        "--annotations/--no-annotations",
        "annotations",
        default=True,
        show_default=True,
        help=(
            "Draw the notes, areas and legends the inventory declares for this view as "
            "native draw.io shapes, which the reader can edit and 'netgraph import drawio' "
            "reads back. Turn them off for a file that should carry the topology alone."
        ),
    ),
)


def _export_flags(command: _Command) -> _Command:
    """Apply every option ``export`` takes, in ``--help`` order."""
    return _apply(
        (
            *_FILTER_OPTIONS,
            *_DNS_OPTIONS,
            *_TARGET_OPTIONS,
            *_DRAWIO_OPTIONS,
            *_VALIDATION_OPTIONS,
        ),
        command,
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
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, writable=True, path_type=Path),
    default=None,
    help=(
        "Write a configuration dialect as a tree: one directory per device, named after its "
        "fully-qualified name, holding the files at the paths the device keeps them at. "
        "Without it a single device's configuration goes to stdout."
    ),
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
    out_dir: Path | None,
    manifest_path: Path | None,
    **_options: Any,
) -> None:
    """Turn the inventory into an operational artefact.

    FORMAT is a description of the network — hosts, dns-zone,
    ansible-inventory, prometheus-sd, cable-list, routes, power, drawio — or
    the configuration a device would actually run: netplan, networkd, ifupdown,
    frr, wireguard, interfaces. Every one of them is deterministic and
    text-diffable, is scoped by the same filters a render takes, and is lossy in
    its own way, so what it could not represent is reported as a JSON manifest
    on stderr rather than dropped in silence.

    A configuration dialect writes a tree: --out DIR gives one directory per
    device, and stdout carries a single device's configuration. A dialect that
    cannot express something a device declares writes nothing and says which
    field, rather than generating a device that is almost right.

    Validation runs first, exactly as it does for a render: an artefact
    generated from an inventory with a dangling cable would misrepresent the
    network, so errors refuse the export unless --force is given.

    \b
    netgraph -i net export hosts -o /etc/hosts.d/net
    netgraph -i net export netplan --out build/config
    netgraph -i net export netplan --name pc-desk | ssh pc-desk 'cat >/etc/netplan/10.yaml'
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
        for layer in layers_for(export_format, options)
    }
    result = export(
        export_format,
        lambda recorder: ExportContext(
            inventory=inventory, graphs=graphs, options=options, recorder=recorder
        ),
    )

    if result.bundle is not None and out_dir is not None:
        destination = _write_config_tree(
            console,
            result.bundle,
            out_dir,
            force=bool(params["force"]),
            inventory_root=inventory.root,
        )
    else:
        _refuse_multi_device_stdout(result, output=output, out_dir=out_dir)
        _write_output(result.encode(), output=output)
        destination = f", written to {output}" if output is not None else ""
    _report_manifest(console, result, manifest_path=manifest_path)
    console.info(f"exported {export_format}: {result.manifest.summary()}{destination}")


def _write_config_tree(
    console: Console,
    bundle: ConfigSet,
    out_dir: Path,
    *,
    force: bool,
    inventory_root: Path,
) -> str:
    """Write a configuration set under ``out_dir`` and report what happened.

    Returns the clause the run summary ends with. Stale files — generated by an
    earlier run and not rewritten by this one — are named as a count rather than
    removed: a device dropped from the inventory should stop being configured,
    and deciding that is the operator's, not a flag's.
    """
    stale = stale_files(bundle, out_dir)
    written = write_config(bundle, out_dir, force=force, inventory_root=inventory_root)
    if stale:
        console.warn(
            f"{len(stale)} file(s) under {out_dir} were generated by an earlier run and this "
            f"one did not rewrite them; nothing was deleted -- the first is {stale[0]}"
        )
    return f", {len(written)} file(s) for {len(bundle.devices)} device(s) written under {out_dir}"


def _refuse_multi_device_stdout(
    result: ExportResult, *, output: Path | None, out_dir: Path | None
) -> None:
    """Refuse to print more than one device's configuration as one stream.

    A configuration dialect's artefact is a tree, and stdout is not one. A single
    device is the case that works — it is a file, and piping it into ``netplan
    apply`` or ``ssh`` is the point — so that is allowed and anything wider is a
    usage error naming the two ways out: narrow the selection, or ask for the
    tree.

    Raises:
        click.UsageError: The selection holds more than one device and no
            ``--out`` was given.
    """
    bundle = result.bundle
    if bundle is None or len(bundle.devices) <= 1 or out_dir is not None:
        return
    listed = ", ".join(device.element for device in bundle.devices[:4])
    if len(bundle.devices) > 4:
        listed += f", and {len(bundle.devices) - 4} more"
    target = "the file" if output is not None else "stdout"
    raise click.UsageError(
        f"{result.export_format} would write {bundle.file_count} file(s) for "
        f"{len(bundle.devices)} device(s), and {target} holds one: {listed}. Write the tree "
        f"with '--out DIR', or narrow the selection to one device with '--name NAME'"
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
            applies = _join_alternatives(formats)
            raise click.UsageError(f"{option} applies to {applies}, not to '{export_format}'")
    if export_format == "dns-zone" and not ctx.params.get("origin"):
        raise click.UsageError(
            "dns-zone needs --origin: a zone file has no meaning without the domain its "
            "records hang under, e.g. --origin example.com"
        )


def _join_alternatives(names: Sequence[str]) -> str:
    """``'a'``, ``'a' or 'b'``, ``'a', 'b' or 'c'`` — a readable list of formats.

    ``' or '.join`` was fine when the longest scope held two names; the six
    configuration dialects made it a sentence nobody could parse.
    """
    quoted = [f"'{name}'" for name in names]
    if len(quoted) < 2:
        return "".join(quoted)
    return f"{', '.join(quoted[:-1])} or {quoted[-1]}"


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
        schedule_format=params["schedule_format"],
        view=params["view"],
        icons=params["icon_theme_option"],
        theme=params.get("theme"),
        styling=params.get("styling", True),
        compress=bool(params["compress"]),
        frames=bool(params["frames"]),
        annotations=bool(params["annotations"]),
        complete=not _is_narrowed(params),
    )


def _is_narrowed(params: Mapping[str, Any]) -> bool:
    """Was the export scoped to less than the whole inventory?

    Stamped into a ``.drawio`` file, where it decides something serious: a
    diagram that holds every element of its view may be imported back with
    deletions honoured, and a narrowed one may not. Absence proves nothing
    about a diagram that was filtered before it was drawn, and the cost of
    getting that wrong is an inventory with a site missing from it.
    """
    return bool(
        params.get("namespaces")
        or params.get("vlans")
        or params.get("kinds")
        or params.get("names")
        or params.get("neighbors_of")
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
# report
# --------------------------------------------------------------------------- #

#: Which layers a report draws. Deliberately not :data:`_LAYER_OPTION`: that one
#: defaults to layer 1, and a report's default is "every layer this inventory has
#: earned" — an inventory with no patch panel gets no cabling diagram — which is
#: a decision :func:`~netgraph.report.layers_for` makes from the documents.
_REPORT_LAYER_OPTION: Final[Callable[[Any], Any]] = click.option(
    "--layer",
    "layers",
    multiple=True,
    type=click.Choice([layer.value for layer in Layer]),
    default=(),
    shell_complete=complete_layer,
    show_default="every layer the inventory declares something for",
    help=(
        "Draw this layer on every page, instead of the ones the inventory has earned. "
        "Repeatable, and honoured verbatim: a layer with nothing in it is reported as empty "
        "rather than dropped."
    ),
)


@cli.command("report")
@click.option(
    "-f",
    "--format",
    "report_format",
    type=click.Choice(REPORT_FORMATS),
    default="markdown",
    show_default=True,
    help=(
        "markdown is committed next to the inventory and diffed; html is one "
        "self-contained site, where a device in a diagram links to its page; json is the "
        "whole document in one file, for downstream tooling."
    ),
)
@click.option(
    "-o",
    "--out",
    "out",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Directory to write the bundle into; created if absent. Required for markdown and "
        "html, which write several files. json writes one document, and goes to stdout "
        "when no directory is named."
    ),
)
@click.option(
    "--template",
    "template_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    metavar="DIR",
    help=(
        "Take page templates from this directory before the bundled ones. A directory "
        "holding only 'device.md.j2' overrides the device page and nothing else."
    ),
)
@_REPORT_LAYER_OPTION
@click.option("--title", default=None, metavar="TEXT", help="Title for the overview page.")
@click.option(
    "--group-depth",
    type=click.IntRange(0),
    default=None,
    metavar="N",
    show_default="1 when the namespace tree branches below the site level, else 0",
    help=(
        "How many namespace levels below the shared prefix one site page covers. "
        "0 puts the whole selection on a single site page."
    ),
)
@click.option(
    "--diagrams/--no-diagrams",
    default=True,
    show_default=True,
    help=(
        "Draw the layer diagrams. Off writes the tables alone, which is faster and needs "
        "no Graphviz; each figure then says so rather than going missing."
    ),
)
@click.option(
    "--generated-at",
    default=None,
    metavar="WHEN",
    show_default=f"${EPOCH_ENV_VAR} if set, otherwise the current time",
    help=(
        "Pin the generated-at stamp to this ISO-8601 timestamp, or to 'none' to leave it "
        "out. The stamp is the only part of a report that is not a function of the "
        "inventory, so pinning it is what makes two runs byte-identical."
    ),
)
@click.option(
    "--revision",
    default=None,
    metavar="REV",
    show_default="the inventory's git commit, when it is in a work tree",
    help="Record this as the inventory's revision instead of asking git for it.",
)
@click.option(
    "--prune",
    is_flag=True,
    default=False,
    help=(
        "Delete the .md, .html, .svg and .json files in --out that this report does not "
        "write — the pages of an element that has since been deleted. They are reported "
        "either way."
    ),
)
@_report_flags
@click.pass_context
def report_command(
    ctx: click.Context,
    /,
    report_format: str,
    out: Path | None,
    template_dir: Path | None,
    title: str | None,
    group_depth: int | None,
    diagrams: bool,
    generated_at: str | None,
    revision: str | None,
    prune: bool,
    **_options: Any,
) -> None:
    """Write the as-built documentation of an inventory.

    An overview, a page per site and a page per element: identity and placement,
    interfaces with their addresses and VLANs, the cables and tunnels that
    terminate on the element, its routing, the address plan with utilisation, the
    VLAN matrix, the cable schedule, the patch-panel port maps, the wireless plan
    and the open validation findings — every table from the same derivation the
    matching command prints, so no two pages can disagree.

    The output is deterministic: two runs over one inventory produce the same
    bytes, so a report can be committed and reviewed as a diff. Pin
    --generated-at to make that literally true.
    """
    app: AppContext = ctx.obj
    params = ctx.params
    console = app.console(err=True)

    if out is None and report_format != "json":
        raise click.UsageError(
            f"{report_format} output is a directory of pages; name one with '--out DIR'. "
            "Only '-f json' writes a single document and can go to stdout."
        )

    inventory = app.load()
    findings = _run_validation(app, inventory, strict=bool(params["strict"]))
    if _is_rejected(inventory, findings):
        _report_problems(console, inventory.errors, findings, commentary=True)
        if not params["force"]:
            console.error(
                "refusing to document an inventory with errors; fix them, or pass --force to "
                "write the report anyway (the findings are part of it either way)"
            )
            raise click.exceptions.Exit(EXIT_INVALID)
        console.warn("documenting despite errors (--force): the report may not match the network")
    elif findings:
        _report_problems(console, (), findings, commentary=True)

    spec = _filter_spec(params)
    selection = _report_selection(app, inventory, spec)
    found_revision, revision_state = _revision(app, inventory.root, revision)
    options = ReportOptions(
        format=report_format,
        layers=tuple(dict.fromkeys(Layer(value) for value in params["layers"])),
        title=title or "",
        group_depth=group_depth,
        diagrams=diagrams,
        generated_at=resolve_timestamp(generated_at or ""),
        scope=spec.describe() if not spec.is_empty else "the whole inventory",
        revision=found_revision,
        revision_state=revision_state,
    )
    bundle, drawings = generate_report(
        selection,
        options=options,
        diagnostics=build_diagnostics(inventory, findings).diagnostics,
        full=inventory,
        templates=template_dir,
    )

    for problem in drawings.problems:
        console.warn(f"a layer could not be drawn: {problem}")
    if out is None:
        _write_output(bundle.files[REPORT_JSON_FILE], output=None)
        return
    stale = bundle.write(out, prune=prune)
    _report_bundle(console, bundle, out=out, stale=stale, pruned=prune)


def _revision(app: AppContext, root: Path, given: str | None) -> tuple[str, str]:
    """``(revision, state)``: ``--revision``, or what git says about the inventory.

    An explicit value is trusted as written and its state is ``given``: it usually
    comes from a pipeline that knows the commit better than a work tree does — a
    tag, or the revision the checkout was made from. An empty ``--revision``
    suppresses the line, which is what a report generated outside version control
    wants to say.
    """
    if given is not None:
        return (given, "given" if given else "")
    # ``git -C`` wants a directory: a single-file inventory is still in whatever
    # work tree its file sits in.
    found = git_revision(root if root.is_dir() else root.parent)
    if found is None:
        app.log("no git revision for the inventory; the report will say so", level=1)
        return ("", "")
    return (found.commit, found.state)


def _report_selection(app: AppContext, inventory: Inventory, spec: FilterSpec) -> Inventory:
    """The inventory narrowed to what the filters select.

    The filters are graph filters (``--vlan`` and ``--neighbors-of`` are only
    answerable from a topology), and a report documents *elements* — so they are
    applied to the graphs and the surviving elements become the inventory every
    page is then built from. The union of three layers, because no single one
    holds every kind of element: a patch panel is spliced out above the cabling, a
    PDU only exists in the power view, and a tunnel's endpoints only meet in the
    overlay.

    Raises:
        click.BadParameter: ``--neighbors-of`` names no element in any layer.
    """
    if spec.is_empty:
        return inventory

    app.log(f"applying filters: {spec.describe()}", level=1)
    selected: set[str] = set()
    unknown: UnknownElementError | None = None
    for layer in (Layer.PHYSICAL, Layer.OVERLAY, Layer.POWER):
        try:
            filtered = filter_graph(build_graph(inventory, layer=layer), spec)
        except UnknownElementError as exc:
            unknown = unknown or exc
            continue
        selected.update(fqn for fqn, node in filtered.nodes.items() if node.is_element)
    if not selected and unknown is not None:
        raise _unknown_element(unknown)
    app.log(f"the filters select {len(selected)} element(s)", level=1)
    # The cables and tunnels are offered rather than selected: ``subset`` keeps
    # each one only where everything it joins survived, which is the same rule a
    # site page is built with. Selecting them by name instead would leave a
    # scoped report with no cabling record at all.
    return subset(inventory, selected | set(inventory.cables) | set(inventory.tunnels))


def _report_bundle(
    console: Console, bundle: Bundle, *, out: Path, stale: Sequence[str], pruned: bool
) -> None:
    """Say what was written, and what was already there and is not part of it."""
    console.info(
        f"wrote {_plural(len(bundle.files), 'file')} ({format_bytes(bundle.size)}) to {out}"
    )
    if not stale:
        return
    verb = "deleted" if pruned else "left in place"
    console.warn(
        f"{_plural(len(stale), 'file')} in {out} {'is' if len(stale) == 1 else 'are'} not part "
        f"of this report and {verb}: {', '.join(stale[:5])}"
        + (f" and {len(stale) - 5} more" if len(stale) > 5 else "")
        + ("" if pruned else ". Pass --prune to remove them.")
    )


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


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #


@cli.group("cache")
def cache_command() -> None:
    """Inspect or clear the parse cache for this inventory."""


@cache_command.command("info")
@click.pass_obj
def cache_info_command(app: AppContext) -> None:
    """Report where this inventory's parse cache is and what is in it.

    Nothing is loaded and nothing is written: this reads the cache directory and
    describes it. The identity table is what a cached entry is keyed by besides
    the file's own bytes, so a cache that keeps missing is explained by whichever
    line of it changed.
    """
    console = app.console()
    info = _cache_info(app)
    console.info("cache")
    console.table(
        ("SETTING", "VALUE"),
        [
            ["enabled", "true" if info.enabled else f"false ({info.reason})"],
            ["directory", str(info.directory)],
            ["location from", info.origin or "default"],
            ["entries", f"{info.entries:,}"],
            ["size", format_bytes(info.used_bytes)],
            ["stale entries", f"{info.stale_entries:,} ({format_bytes(info.stale_bytes)})"],
            ["maximum size", format_bytes(info.max_bytes)],
        ],
    )
    console.print()
    console.info("identity (an entry is keyed by this and the file's contents)")
    console.table(("INPUT", "VALUE"), [list(pair) for pair in info.identity.describe()])
    if not info.exists:
        console.info(
            "nothing cached yet; the next command that loads this inventory fills it"
            if info.enabled
            else "nothing cached, and the cache is off"
        )


@cache_command.command("clear")
@click.option(
    "--all",
    "every",
    is_flag=True,
    help="Clear the cache of every inventory, not just this one.",
)
@click.pass_obj
def cache_clear_command(app: AppContext, every: bool) -> None:
    """Delete this inventory's cached documents.

    Only ever a way to reclaim space or to rule the cache out of an
    investigation: entries are keyed by file contents and by the code that read
    them, so a stale one cannot be served and clearing fixes nothing that
    editing the inventory would not.
    """
    console = app.console()
    info = _cache_info(app)
    target = info.directory.parent.parent if every else info.directory
    removed, freed = clear_cache(target)
    scope = "every inventory" if every else "this inventory"
    console.info(
        f"cleared {scope}: {removed:,} entr{'y' if removed == 1 else 'ies'} under {target}"
    )
    console.print(f"{removed} entr{'y' if removed == 1 else 'ies'}, {format_bytes(freed)} freed")


def _cache_info(app: AppContext) -> CacheInfo:
    """Describe the cache without opening it, disabled or not.

    ``AppContext.cache`` returns ``None`` for a cache that is switched off, which
    is the right answer for a *load* and the wrong one for a report: "off, and
    here is where it would be, and here is what is still lying there" is exactly
    what somebody asking has asked.
    """
    settings = app.config().cache
    reason = ""
    if app.no_cache:
        reason = "--no-cache"
    elif disabled_by_environment():
        reason = f"{DISABLE_ENV_VAR} is set"
    elif not settings.enabled:
        reason = f"{CONFIG_FILE_NAME} [{CACHE_TABLE}] enabled = false"
    store = open_cache(app.inventory, directory=settings.directory, max_bytes=settings.max_bytes)
    return inspect_cache(
        store.directory,
        origin=store.origin,
        enabled=not reason,
        reason=reason,
        identity=store.identity,
        max_bytes=store.max_bytes,
    )


def format_bytes(count: int) -> str:
    """A byte count as a short, readable string. Decimal units, as disks use."""
    if count < 1000:
        return f"{count} B"
    size = float(count)
    for unit in ("kB", "MB", "GB"):
        size /= 1000
        if size < 1000 or unit == "GB":
            return f"{size:.1f} {unit}"
    raise AssertionError  # pragma: no cover - the loop always returns


@cli.command("version")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the report as a JSON object instead of aligned text.",
)
def version_command(as_json: bool) -> None:
    """Print the netgraph, Python and Graphviz versions in use.

    'netgraph --version' prints the same text. This command exists for the
    '--json' form, which is what to paste into a bug report: it also carries the
    interpreter path, the platform, which YAML parser was selected and the
    resolved version of every runtime dependency.
    """
    report = collect_version()
    if as_json:
        click.echo(json.dumps(version_as_dict(report), indent=2, sort_keys=False))
        return
    click.echo(format_version(report), nl=False)


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #


@cli.command("rules")
@click.option(
    "--fixable",
    is_flag=True,
    default=False,
    help="List only the rules 'netgraph validate --fix' can repair, and what each repair does.",
)
@click.pass_obj
def rules_command(app: AppContext, fixable: bool) -> None:
    """List the validation rules, their severity and their schema aliases."""
    console = app.console()
    if fixable:
        console.table(
            ("RULE", "SEVERITY", "CHOICES", "WHAT THE FIX DOES"),
            [
                [
                    spec.rule,
                    str(rule_for(spec.rule).severity),
                    ", ".join(choice.key for choice in spec.choices) or "-",
                    spec.summary,
                ]
                for spec in FIXES
            ],
        )
        return
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
    # ``write_text`` from netgraph.fsio rather than Path's: a JSON Schema is
    # checked into a repository next to the inventory it describes, and one
    # written with CRLF on Windows would show up as a whole-file diff against
    # the same schema written anywhere else.
    write_text(output, document)


# --------------------------------------------------------------------------- #
# completion
# --------------------------------------------------------------------------- #


@cli.command("completion")
@click.argument("shell", type=click.Choice(SHELLS))
def completion_command(shell: str) -> None:
    """Print the shell completion script for bash, zsh, fish or PowerShell.

    \b
    bash:  netgraph completion bash > ~/.local/share/bash-completion/completions/netgraph
    zsh:   netgraph completion zsh  > ~/.zfunc/_netgraph   # with ~/.zfunc on $fpath
    fish:  netgraph completion fish > ~/.config/fish/completions/netgraph.fish

    \b
    PowerShell evaluates its script rather than sourcing it, so on Windows (or
    under pwsh anywhere) the line to run -- and to put in $PROFILE -- is:
    netgraph completion powershell | Out-String | Invoke-Expression

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
