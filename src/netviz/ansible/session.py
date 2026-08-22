"""One inventory tree, loaded once, asked many times.

A playbook asks the same tree a great many questions. Rendering one systemd unit
is one query; rendering it for forty hosts is forty, and a template that reads
three facts is a hundred and twenty. Loading, validating and flattening the tree
each time would be the whole cost of the run, and it would be paid for an answer
that cannot have changed: Ansible holds a play's inventory still for the length
of the play, and so does this.

So a session is the unit. It carries the loaded :class:`~netviz.loader.inventory
.Inventory`, the :class:`~netviz.nql.World` built from it and any graphs a
selector needed, and :func:`open_session` hands back the same one for the same
root — which is what makes a lookup plugin a dictionary lookup after the first
call.

**A session is a snapshot.** It is not invalidated when a file changes, because
the alternative — re-reading the tree between two tasks of one play — would let
a template rendered at the top of a run and one rendered at the bottom disagree
about the network, and no amount of speed is worth a run that is internally
inconsistent. A process that means to see an edit calls :func:`forget`; the
processes this is written for exit instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netviz.config import load_config
from netviz.errors import NetvizError
from netviz.loader import load_tree
from netviz.loader.inventory import Inventory
from netviz.nql import Query as RelationalQuery
from netviz.nql import World, build_world, is_relational
from netviz.nql import bind as bind_params
from netviz.nql import declare as declare_params
from netviz.nql import execute as execute_relational
from netviz.nql import parse as parse_relational
from netviz.nql.types import ValueType
from netviz.query import QueryError
from netviz.query import evaluate as evaluate_selector
from netviz.query import parse as parse_selector
from netviz.render.graph import FilterSpec, Graph, Layer, build_graph, filter_graph
from netviz.validate import validate

__all__ = ["Answer", "InventoryRejected", "Session", "forget", "layer_named", "open_session"]

#: The layer a question is asked at unless one is named. Layer 2 is what the
#: Ansible exporter reads and what a device-shaped question means: every element
#: that is a machine, with its ports and its VLANs, and no derived subnet nodes.
DEFAULT_LAYER = Layer.L2


def layer_named(layer: Layer | str | None) -> Layer:
    """The layer a caller named, as the enum, defaulting when nothing was said.

    Here rather than in each plugin because a plugin's options arrive as text
    and the list of layers is netviz's to know.

    Raises:
        NetvizError: No layer is spelled that way.
    """
    if layer is None:
        return DEFAULT_LAYER
    if isinstance(layer, Layer):
        return layer
    try:
        return Layer(str(layer).lower())
    except ValueError:
        known = ", ".join(one.value for one in Layer)
        raise NetvizError(f"{layer!r} is not a layer; the layers are {known}") from None


class InventoryRejected(NetvizError):
    """The tree does not load or does not validate, so no answer would be true.

    An answer computed from documents that did not load is a lie of omission —
    it reports what is left rather than what is declared — and a playbook is
    exactly the wrong place to discover that, because by then something has been
    written to a machine. ``force`` is how a caller says they know.
    """

    exit_code = 4


@dataclass(frozen=True, slots=True)
class Answer:
    """What a query produced, in the shape a plugin hands to Ansible.

    :attr:`rows` is JSON-ready in both dialects: a relational query projects
    scalars and objects, and a selector answers with the fully-qualified name of
    every element it picked. A caller that wants one value asks for one value —
    ``rows[0]`` — and the plugins say so when there is not exactly one, rather
    than quietly templating the first of several.
    """

    expression: str
    rows: tuple[Any, ...]
    #: Was this the relational language, or the selector?
    relational: bool

    def __len__(self) -> int:
        return len(self.rows)

    def __bool__(self) -> bool:
        return bool(self.rows)


@dataclass
class Session:
    """A loaded tree and everything derived from it, kept for the next question."""

    root: Path
    inventory: Inventory
    strict: bool = False
    force: bool = False
    _world: World | None = None
    _graphs: dict[Layer, Graph] = field(default_factory=dict)
    #: Parsed queries, by text, origin and parameter types. See :meth:`_parsed`.
    _queries: dict[tuple[Any, ...], RelationalQuery] = field(default_factory=dict)
    #: Whatever a caller wants memoised for the life of this session, keyed by
    #: something of its own choosing. :mod:`netviz.ansible.inventory` keeps the
    #: built document here, so a lookup that resolves a host name and an
    #: inventory plugin that lists the hosts build it once between them.
    derived: dict[Any, Any] = field(default_factory=dict)

    @property
    def world(self) -> World:
        """The flattened object graph a relational query reads."""
        if self._world is None:
            self._world = build_world(self.inventory)
        return self._world

    def graph(self, layer: Layer = DEFAULT_LAYER) -> Graph:
        """The graph a selector query is answered against."""
        if layer not in self._graphs:
            self._graphs[layer] = build_graph(self.inventory, layer=layer)
        return self._graphs[layer]

    def ask(
        self,
        expression: str,
        params: Mapping[str, Any] | None = None,
        *,
        layer: Layer = DEFAULT_LAYER,
        source: str = "query",
    ) -> Answer:
        """Answer ``expression`` in whichever of the two languages it is written.

        The same dispatch ``netviz query`` makes, on the same first word, so a
        query developed at the terminal is the query a template runs.

        Raises:
            QueryError: The query does not parse, names something the schema
                does not have, or names a parameter that was not supplied.
        """
        if is_relational(expression):
            values = dict(params or {})
            query = self._parsed(expression, source=source, params=declare_params(values))
            result = execute_relational(query, self.world, bind_params(values))
            return Answer(expression, result.rows, relational=True)
        if params:
            raise QueryError(
                "a parameter binds a '$name' in a relational query, one beginning with "
                "'select' or 'with'; this is a selector, which has none",
                text=expression,
                source=source,
            )
        selected = evaluate_selector(parse_selector(expression, source=source), self.graph(layer))
        return Answer(expression, tuple(sorted(selected.nodes)), relational=False)

    def _parsed(
        self, expression: str, *, source: str, params: Mapping[str, ValueType]
    ) -> RelationalQuery:
        """``expression``, parsed — or the tree the last host's copy of it made.

        The same query is asked once per host: forty templates over forty hosts
        is one *question* and sixteen hundred parses, and a parse tree depends on
        nothing but the text, the schema and the parameter *types*. So it is
        keyed by those three and nothing else — the values are substituted at
        execution, which is what makes them safe to leave out of the key.

        Raises:
            QueryError: As :meth:`ask`.
        """
        key = (expression, source, tuple(sorted((name, str(one)) for name, one in params.items())))
        found = self._queries.get(key)
        if found is None:
            found = parse_relational(
                expression, source=source, schema=self.world.schema, params=params
            )
            self._queries[key] = found
        return found

    def select(self, expression: str, *, layer: Layer = DEFAULT_LAYER) -> FilterSpec:
        """A selector, answered, as the filter every other netviz command takes.

        Raises:
            QueryError: As :meth:`ask`.
        """
        answered = self.ask(expression, layer=layer)
        return FilterSpec(selected=frozenset(answered.rows), select=expression)

    def narrowed(self, spec: FilterSpec, *, layer: Layer = DEFAULT_LAYER) -> Graph:
        """The graph ``spec`` leaves standing."""
        graph = self.graph(layer)
        return graph if spec.is_empty else filter_graph(graph, spec)


#: Sessions already opened, by root and by the two flags that change what a
#: session will answer at all.
_OPEN: dict[tuple[Path, bool, bool], Session] = {}


def open_session(
    root: Path | str, *, strict: bool = False, force: bool = False, reuse: bool = True
) -> Session:
    """Load ``root`` — or hand back the session that already did.

    Args:
        root: The inventory tree, or a single YAML file.
        strict: Treat warnings as errors, as ``--strict`` does everywhere.
        force: Answer from a tree that has errors in it. What is returned is
            then what *loaded*, which is not what is declared.
        reuse: Consult the per-process table. ``False`` loads afresh without
            disturbing what other callers are already sharing.

    Raises:
        InventoryRejected: The tree does not load, or does not validate, and
            ``force`` was not given.
    """
    resolved = Path(root).expanduser().resolve()
    key = (resolved, strict, force)
    if reuse and key in _OPEN:
        return _OPEN[key]

    inventory = load_tree(resolved)
    problems = _problems(inventory, strict=strict)
    if problems and not force:
        raise InventoryRejected(
            f"{resolved} has {len(problems)} problem(s) and an answer from it would not "
            f"describe the network: {'; '.join(problems[:3])}"
            + ("; ..." if len(problems) > 3 else "")
            + ". Fix them, run 'netviz validate' to see them all, or pass force=true."
        )
    session = Session(root=resolved, inventory=inventory, strict=strict, force=force)
    if reuse:
        _OPEN[key] = session
    return session


def forget(root: Path | str | None = None) -> None:
    """Drop the memoised session for ``root``, or every one of them.

    For the process that means to see an edit — a long-lived one, or a test.
    """
    if root is None:
        _OPEN.clear()
        return
    resolved = Path(root).expanduser().resolve()
    for key in [one for one in _OPEN if one[0] == resolved]:
        del _OPEN[key]


def _problems(inventory: Inventory, *, strict: bool) -> list[str]:
    """Everything that would make an answer from ``inventory`` untrue.

    A load error always counts: a document that did not parse is an element
    missing from every answer, which no amount of severity configuration can
    make benign. A finding counts when it is fatal — after the inventory's own
    ``netviz.toml`` has had its say, and after ``strict`` has promoted the
    warnings, so a playbook grades the tree exactly as ``netviz validate``
    standing in the same directory would.
    """
    settings = load_config(inventory.root).validation.with_overrides(
        strict=True if strict else None
    )
    problems = [str(error) for error in inventory.errors]
    problems.extend(
        f"{finding.rule}: {finding.message}"
        for finding in validate(inventory, settings)
        if finding.severity.is_fatal
    )
    return problems
