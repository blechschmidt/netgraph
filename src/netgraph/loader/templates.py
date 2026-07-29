"""Device templates: resolving ``spec.from`` and merging (§6.6 of ``docs/schema.md``).

A ``kind: template`` document is a named partial device ``spec``. A device that
declares ``spec.from: <ref>`` is loaded as if the template's spec had been
written underneath its own. The merge happens here, in the loader, before the
document reaches the models — so no consumer downstream needs to know templates
exist.

The merge rules, stated once and enforced in one function
(:func:`merge_spec`):

* **Scalars and mappings.** A key the device declares wins. A key only the
  template declares is inherited. A key both declare whose values are *both*
  mappings is merged recursively, key by key, by these same rules — so a device
  may override ``bridge.address`` without restating ``bridge.type``.
* **``interfaces``** merges by interface ``name``. The result is the template's
  interfaces, in the template's order, each merged with the device's entry of
  the same name if there is one, followed by the device's remaining interfaces
  in the device's order.
* **Every other list** — ``vlans``, ``members``, ``addresses``,
  ``trunk_vlans`` — is *replaced* wholesale when the device declares it. A list
  netgraph has no key for cannot be merged without inventing one, and a rule
  that only holds sometimes is worse than a rule that never does.
* **Only ``spec`` merges.** ``metadata`` is the device's own: a template
  contributes no name, no labels and no description.
* **Templates layer.** A template may itself declare ``from``; it is resolved
  first, so the device sees one fully-resolved spec however deep the chain is.

Provenance survives all of it. Every field the device did not write carries a
redirect to the file and line of the template that did, so a value the template
got wrong is reported against the template rather than against the fiftieth
device that inherited it. See :mod:`netgraph.loader.provenance`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from netgraph.errors import SchemaIssue, echo_value
from netgraph.loader.documents import RawDocument
from netgraph.loader.provenance import FieldPath, Provenance, Site
from netgraph.loader.ranges import expand_interfaces
from netgraph.models.template import INHERIT_KEY, Template

__all__ = [
    "INHERIT_KEY",
    "INTERFACES_KEY",
    "ResolvedTemplate",
    "TemplateRegistry",
    "merge_spec",
    "resolved_spec",
]

#: The one list netgraph knows a key for, and therefore the one it merges.
INTERFACES_KEY: Final = "interfaces"


@dataclass(frozen=True, slots=True)
class ResolvedTemplate:
    """A template with its own ``from`` chain merged and its ranges expanded."""

    #: Fully-qualified name, ``templates/c9200l-48p``.
    fqn: str
    #: The partial spec, ready to be merged underneath a device's own.
    spec: Mapping[str, Any]
    #: Where each field of :attr:`spec` was written.
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class _Entry:
    """One registered template document."""

    template: Template
    document: RawDocument
    namespace: str
    fqn: str


@dataclass
class _MergeContext:
    """The two provenances a merge reads and the redirect table it writes."""

    device: Provenance
    template: Provenance
    redirects: dict[FieldPath, Site] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def _ancestors(namespace: str) -> Iterator[str]:
    """``namespace`` and every parent of it, nearest first, the root last."""
    current = namespace
    while current:
        yield current
        current, _, _ = current.rpartition("/")
    yield ""


class TemplateRegistry:
    """The templates of one inventory, resolved with the reference rules of §2.2.

    Templates are looked up exactly the way elements are — the referring
    document's own namespace first, then each ancestor, then the inventory as a
    whole provided the short name is unique — so a template may live wherever it
    is convenient, including a ``templates/`` directory next to the sites that
    use it. They are indexed *separately* from elements: a template and a switch
    may share a name, because no field ever accepts both.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._by_namespace: dict[str, dict[str, str]] = {}
        self._by_short_name: dict[str, list[str]] = {}
        self._resolved: dict[str, ResolvedTemplate | None] = {}
        self._resolving: list[str] = []

    def add(self, template: Template, *, document: RawDocument, namespace: str) -> str | None:
        """Index ``template``; ``None`` when the name is taken (``NG-M002``)."""
        name = template.metadata.name
        fqn = f"{namespace}/{name}" if namespace else name
        if fqn in self._entries:
            return None
        self._entries[fqn] = _Entry(
            template=template, document=document, namespace=namespace, fqn=fqn
        )
        self._by_namespace.setdefault(namespace, {})[name] = fqn
        self._by_short_name.setdefault(name, []).append(fqn)
        return fqn

    def source_of(self, fqn: str) -> RawDocument | None:
        """The document a registered template was declared in."""
        entry = self._entries.get(fqn)
        return None if entry is None else entry.document

    def lookup(self, ref: str, *, namespace: str) -> tuple[str | None, tuple[str, ...]]:
        """Resolve ``ref`` written in ``namespace`` to ``(fqn, ambiguous)``."""
        if "/" in ref:
            for candidate in (f"{namespace}/{ref}" if namespace else ref, ref):
                if candidate in self._entries:
                    return candidate, ()
            return None, ()
        for scope in _ancestors(namespace):
            fqn = self._by_namespace.get(scope, {}).get(ref)
            if fqn is not None:
                return fqn, ()
        matches = self._by_short_name.get(ref, [])
        if len(matches) == 1:
            return matches[0], ()
        return None, tuple(matches)

    def names(self) -> tuple[str, ...]:
        """Every registered template, in registration order."""
        return tuple(self._entries)

    # -- resolution ------------------------------------------------------

    def resolve_all(self) -> list[tuple[RawDocument, SchemaIssue]]:
        """Resolve every template once, in registration order.

        Doing this up front rather than on first use is what keeps a broken
        template reported against itself, once, wherever in the tree the devices
        that use it happen to live. It also means an *unused* template with a
        malformed range is still caught.

        Returns:
            One ``(document, issue)`` pair per problem, the document being the
            template the problem belongs to.
        """
        problems: list[tuple[RawDocument, SchemaIssue]] = []
        for fqn in self._entries:
            self.resolve(fqn, problems)
        return problems

    def resolve(
        self, fqn: str, problems: list[tuple[RawDocument, SchemaIssue]]
    ) -> ResolvedTemplate | None:
        """The fully merged, range-expanded spec of one template.

        Returns ``None`` when the template could not be resolved; the reason has
        been appended to ``problems`` exactly once, however many times the
        template is asked for.
        """
        if fqn in self._resolved:
            return self._resolved[fqn]
        if fqn in self._resolving:  # NG-M003
            cycle = " -> ".join((*self._resolving[self._resolving.index(fqn) :], fqn))
            entry = self._entries[fqn]
            problems.append(
                (
                    entry.document,
                    SchemaIssue(
                        path=("spec", INHERIT_KEY),
                        message=f"template inheritance is cyclic: {cycle}",
                        rule="NG-M003",
                    ),
                )
            )
            self._resolved[fqn] = None
            return None

        self._resolving.append(fqn)
        try:
            resolved = self._build(self._entries[fqn], problems)
        finally:
            self._resolving.pop()
        self._resolved[fqn] = resolved
        return resolved

    def _build(
        self, entry: _Entry, problems: list[tuple[RawDocument, SchemaIssue]]
    ) -> ResolvedTemplate | None:
        # Order matters: a template's own ranges expand *before* its parent is
        # merged underneath, because the merge keys on interface names and a
        # range entry has none yet.
        spec, provenance, issues = resolved_spec(
            entry.template.spec, provenance=Provenance(base=entry.document)
        )
        if issues:
            problems.extend((entry.document, issue) for issue in issues)
            return None

        parent_ref = entry.template.spec.get(INHERIT_KEY)
        if parent_ref is not None:
            parent = self._parent(entry, parent_ref, problems)
            if parent is None:
                return None
            spec, redirects = merge_spec(spec, template=parent, device_provenance=provenance)
            provenance = provenance.with_redirects({**provenance.redirects, **redirects})

        return ResolvedTemplate(fqn=entry.fqn, spec=spec, provenance=provenance)

    def merge_into(
        self,
        spec: Mapping[str, Any],
        *,
        reference: Any,
        namespace: str,
        provenance: Provenance,
    ) -> tuple[Mapping[str, Any], Provenance, list[SchemaIssue], str | None]:
        """Resolve one device's ``spec.from`` and merge the template underneath.

        :meth:`resolve_all` must have run first: a template that could not be
        resolved has already been reported against itself, and this reports only
        that the device cannot use it.

        Returns:
            ``(spec, provenance, issues, template fqn)``. A non-empty ``issues``
            means the device must not be built.
        """
        body, provenance, issues = resolved_spec(spec, provenance=provenance)
        if issues:
            return body, provenance, issues, None

        bad = _bad_reference(reference, self, namespace=namespace)
        if bad is not None:
            return body, provenance, [bad], None

        assert isinstance(reference, str)
        fqn, _ = self.lookup(reference, namespace=namespace)
        assert fqn is not None
        template = self.resolve(fqn, [])
        if template is None:  # NG-M004
            return (
                body,
                provenance,
                [
                    SchemaIssue(
                        path=("spec", INHERIT_KEY),
                        message=(
                            f"template {fqn!r} is itself invalid, so it cannot be merged "
                            "here; fix the errors reported against the template"
                        ),
                        rule="NG-M004",
                    )
                ],
                fqn,
            )

        merged, redirects = merge_spec(body, template=template, device_provenance=provenance)
        return (
            merged,
            provenance.with_redirects({**provenance.redirects, **redirects}),
            [],
            fqn,
        )

    def _parent(
        self,
        entry: _Entry,
        ref: Any,
        problems: list[tuple[RawDocument, SchemaIssue]],
    ) -> ResolvedTemplate | None:
        issue = _bad_reference(ref, self, namespace=entry.namespace)
        if issue is not None:
            problems.append((entry.document, issue))
            return None
        assert isinstance(ref, str)
        fqn, _ = self.lookup(ref, namespace=entry.namespace)
        assert fqn is not None
        parent = self.resolve(fqn, problems)
        if parent is None:
            problems.append(
                (
                    entry.document,
                    SchemaIssue(
                        path=("spec", INHERIT_KEY),
                        message=(
                            f"template {fqn!r} could not be resolved, so this template "
                            "cannot be either"
                        ),
                        rule="NG-M004",
                    ),
                )
            )
        return parent


def _bad_reference(ref: Any, registry: TemplateRegistry, *, namespace: str) -> SchemaIssue | None:
    """``NG-M001`` — why ``spec.from`` does not name exactly one template."""
    if not isinstance(ref, str):
        return SchemaIssue(
            path=("spec", INHERIT_KEY),
            message=f"'from' must name a template, got {type(ref).__name__}",
            rule="NG-M001",
        )
    fqn, ambiguous = registry.lookup(ref, namespace=namespace)
    if fqn is not None:
        return None
    if ambiguous:
        return SchemaIssue(
            path=("spec", INHERIT_KEY),
            message=(
                f"template reference {echo_value(ref)} is ambiguous; it matches "
                f"{', '.join(ambiguous)}. Use the fully-qualified name."
            ),
            rule="NG-M001",
        )
    known = registry.names()
    hint = (
        f" Known templates: {', '.join(known)}."
        if known
        else " This inventory declares no 'kind: template' document."
    )
    return SchemaIssue(
        path=("spec", INHERIT_KEY),
        message=f"no template named {echo_value(ref)}.{hint}",
        rule="NG-M001",
    )


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def resolved_spec(
    spec: Mapping[str, Any],
    *,
    provenance: Provenance,
    prefix: FieldPath = ("spec",),
) -> tuple[Mapping[str, Any], Provenance, list[SchemaIssue]]:
    """Strip ``from`` and expand the interface ranges of one spec.

    Returns:
        ``(spec, provenance, issues)``. A non-empty ``issues`` means the
        document must not be built: the returned spec is then the input, so a
        caller that reports and stops never sees a half-expanded list. When
        there is nothing to do — no ``from``, no ``range`` — the input mapping
        is returned as-is, which is what keeps the common document off the
        allocating path.
    """
    body: Mapping[str, Any] = (
        spec
        if INHERIT_KEY not in spec
        else {key: value for key, value in spec.items() if key != INHERIT_KEY}
    )
    expansion = expand_interfaces(
        body.get(INTERFACES_KEY),
        prefix=(*prefix, INTERFACES_KEY),
        provenance=provenance,
    )
    if expansion.issues:
        return body, provenance, expansion.issues
    if not expansion.expanded:
        return body, provenance, []
    expanded = dict(body)
    expanded[INTERFACES_KEY] = expansion.entries
    return (
        expanded,
        provenance.with_redirects({**provenance.redirects, **expansion.redirects}),
        [],
    )


def merge_spec(
    device_spec: Mapping[str, Any],
    *,
    template: ResolvedTemplate,
    device_provenance: Provenance,
    prefix: FieldPath = ("spec",),
) -> tuple[dict[str, Any], dict[FieldPath, Site]]:
    """Merge ``template`` underneath ``device_spec``.

    Args:
        device_spec: The device's own ``spec``, with its interface ranges
            already expanded. ``from`` is dropped.
        template: The resolved template to inherit from.
        device_provenance: Where the device's own fields were written.
        prefix: Path of ``spec`` inside the document.

    Returns:
        The merged spec and the redirect table describing it. The table is
        complete for ``spec.interfaces``: every entry carries one, because the
        merge reorders the list.
    """
    ctx = _MergeContext(device=device_provenance, template=template.provenance)
    merged = _merge_mapping(
        device_spec,
        template.spec,
        out=prefix,
        device_path=prefix,
        template_path=prefix,
        ctx=ctx,
    )
    return merged, ctx.redirects


def _merge_mapping(
    device: Mapping[str, Any],
    inherited: Mapping[str, Any],
    *,
    out: FieldPath,
    device_path: FieldPath,
    template_path: FieldPath,
    ctx: _MergeContext,
) -> dict[str, Any]:
    """Merge two mappings, the device's keys winning. Template order first.

    Neither side still carries ``from``: :func:`resolved_spec` strips it from
    each spec before it gets here, which is what makes ``from`` invisible to
    the merge rather than a key it has to remember to skip at every depth.
    """
    merged: dict[str, Any] = {}
    for key, value in inherited.items():
        if key not in device:
            merged[key] = copy.deepcopy(value)
            ctx.redirects[(*out, key)] = ctx.template.locate((*template_path, key))
            continue

        own = device[key]
        if key == INTERFACES_KEY and isinstance(own, list) and isinstance(value, list):
            merged[key] = _merge_interfaces(
                own,
                value,
                out=(*out, key),
                device_path=(*device_path, key),
                template_path=(*template_path, key),
                ctx=ctx,
            )
        elif isinstance(own, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_mapping(
                own,
                value,
                out=(*out, key),
                device_path=(*device_path, key),
                template_path=(*template_path, key),
                ctx=ctx,
            )
        else:
            # A scalar, or a list netgraph has no key for: the device replaces
            # the template's value outright.
            merged[key] = own

    for key, own in device.items():
        if key not in inherited:
            merged[key] = own
    return merged


def _merge_interfaces(
    device: list[Any],
    inherited: list[Any],
    *,
    out: FieldPath,
    device_path: FieldPath,
    template_path: FieldPath,
    ctx: _MergeContext,
) -> list[Any]:
    """Merge two interface lists by ``name``; see the module docstring."""
    by_name: dict[str, int] = {}
    for index, entry in enumerate(device):
        name = _name_of(entry)
        # A device that declares one name twice keeps both entries; NG-I001
        # reports it a moment later, and swallowing the second here would turn
        # a duplicate into a silent override.
        if name is not None and name not in by_name:
            by_name[name] = index

    merged: list[Any] = []
    consumed: set[int] = set()
    for template_index, entry in enumerate(inherited):
        name = _name_of(entry)
        own_index = by_name.get(name) if name is not None else None
        position = (*out, len(merged))
        if own_index is None:
            ctx.redirects[position] = ctx.template.locate((*template_path, template_index))
            merged.append(copy.deepcopy(entry))
            continue

        consumed.add(own_index)
        # Both sides yielded a name, so both are mappings: ``_name_of`` says so.
        own = device[own_index]
        ctx.redirects[position] = ctx.device.locate((*device_path, own_index))
        merged.append(
            _merge_mapping(
                own,
                entry,
                out=position,
                device_path=(*device_path, own_index),
                template_path=(*template_path, template_index),
                ctx=ctx,
            )
        )

    for index, entry in enumerate(device):
        if index in consumed:
            continue
        ctx.redirects[(*out, len(merged))] = ctx.device.locate((*device_path, index))
        merged.append(entry)
    return merged


def _name_of(entry: Any) -> str | None:
    """The ``name`` of an interface entry, when it has a usable one."""
    if isinstance(entry, Mapping):
        name = entry.get("name")
        if isinstance(name, str):
            return name
    return None
