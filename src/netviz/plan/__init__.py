"""The diff engine and the changeset it produces: ``netviz plan`` and ``netviz apply``.

An inventory is a description of a network that lives in files. Two such
descriptions — this branch and ``main``, this folder and that one, what is
declared and what the network reports — differ in ways that a text diff can
show and cannot explain. This package computes the difference *between the
networks*, as an ordered list of typed changes to addressable elements, and
executes it back onto the files.

The pieces, in the order a change flows through them:

:mod:`~netviz.plan.address`
    Every element gets a stable address: ``device.core/sw-1``.
:mod:`~netviz.plan.document`
    The normalised form an element is compared in, and the field-level diff.
:mod:`~netviz.plan.identity`
    Structural rename detection, so a renamed switch is one entry and not two.
:mod:`~netviz.plan.order`
    Dependency ordering: a device is created before the cable that lands on it,
    and destroyed after it.
:mod:`~netviz.plan.diff`
    The pure function that ties those together: two inventories in, one plan out.
:mod:`~netviz.plan.live`
    The target state ``--from-live`` plans against, adopted from a capture.
:mod:`~netviz.plan.state`
    The hash that stops a stored plan being applied to a tree that has moved on.
:mod:`~netviz.plan.execute`
    Each entry translated into :mod:`netviz.edit` operations, which is what
    makes ``apply`` preserve comments and formatting.
:mod:`~netviz.plan.report`
    The terraform-shaped summary and the JSON document.

Applying to the **live network** is deliberately out of scope. ``netviz
apply`` writes YAML files and nothing else: it never opens a session to a
device, and there is no flag that makes it. The loop it closes is
``capture → plan → files``, so that the declared inventory can be brought up to
date with what the network reports — not the other way round.
"""

from __future__ import annotations

from netviz.plan.address import (
    ADDRESS_TYPES,
    ANNOTATION_TYPES,
    LAYOUT_TYPE,
    SIDECAR_TYPES,
    Address,
    AddressSyntaxError,
    address_of,
    parse_address,
)
from netviz.plan.diff import diff, elements_by_address
from netviz.plan.document import body_of, diff_documents, document_of
from netviz.plan.execute import PlanExecutionError, operations_for, translate
from netviz.plan.identity import (
    DECISIVE,
    EVIDENCE,
    STABLE_ID_ANNOTATION,
    detect_renames,
    fingerprints,
)
from netviz.plan.live import Adoption, adopt
from netviz.plan.model import (
    ACTION_SIGILS,
    PLAN_SCHEMA_VERSION,
    Action,
    Change,
    FieldChange,
    Plan,
    PlanFormatError,
    StateRef,
    plan_from_dict,
)
from netviz.plan.order import dependencies, order_changes
from netviz.plan.paths import MISSING, PathError, Selector, format_path, parse_path
from netviz.plan.report import PLAN_FORMATS, render_plan, summary_line, write_plan
from netviz.plan.sources import PlanSourceError, Side, git_ref, load_side
from netviz.plan.state import state_digest

__all__ = [
    "ACTION_SIGILS",
    "ADDRESS_TYPES",
    "ANNOTATION_TYPES",
    "DECISIVE",
    "EVIDENCE",
    "LAYOUT_TYPE",
    "MISSING",
    "PLAN_FORMATS",
    "PLAN_SCHEMA_VERSION",
    "SIDECAR_TYPES",
    "STABLE_ID_ANNOTATION",
    "Action",
    "Address",
    "AddressSyntaxError",
    "Adoption",
    "Change",
    "FieldChange",
    "PathError",
    "Plan",
    "PlanExecutionError",
    "PlanFormatError",
    "PlanSourceError",
    "Selector",
    "Side",
    "StateRef",
    "address_of",
    "adopt",
    "body_of",
    "dependencies",
    "detect_renames",
    "diff",
    "diff_documents",
    "document_of",
    "elements_by_address",
    "fingerprints",
    "format_path",
    "git_ref",
    "load_side",
    "operations_for",
    "order_changes",
    "parse_address",
    "parse_path",
    "plan_from_dict",
    "render_plan",
    "state_digest",
    "summary_line",
    "translate",
    "write_plan",
]
