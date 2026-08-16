"""What a fix *is*: an offer to repair one finding, expressed as operations.

A fix carries no code and touches no file. It is a name, a sentence a person can
read before deciding, and the edit operations that would do it — which is the
same closed vocabulary :mod:`netviz.edit` writes everything else through, so a
fix is logged, refused, undone and replayed exactly like a hand-made edit.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from netviz.edit.operations import Operation

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from netviz.loader.inventory import Inventory
    from netviz.validate import Finding

__all__ = ["Choice", "Fix", "FixProducer", "FixSpec"]


@dataclass(frozen=True, slots=True)
class Fix:
    """One way to repair one finding."""

    #: Short, stable token identifying this repair among the ones the rule
    #: offers, e.g. ``list`` or ``drop``. Stable because it is what
    #: ``--choose W114=drop`` and the editor's Fix menu name.
    key: str
    #: One line, in the imperative, naming what it would do to this inventory.
    title: str
    #: The edit that does it, in the order it must be applied.
    operations: tuple[Operation, ...]

    def __str__(self) -> str:
        return self.title


#: A pure function from a finding and the tree it was found in to the repairs
#: on offer. Total: it returns ``()`` rather than raising when the finding does
#: not match the inventory it is handed, because a finding can outlive the tree
#: that produced it — an editor holds one while the file changes underneath.
FixProducer: TypeAlias = Callable[["Finding", "Inventory"], Sequence[Fix]]


@dataclass(frozen=True, slots=True)
class Choice:
    """One of the repairs a rule offers, as the documentation lists it."""

    key: str
    #: What choosing this one does, in the third person: "adds the VLAN …".
    summary: str


@dataclass(frozen=True, slots=True)
class FixSpec:
    """The entry of :data:`netviz.fixes.FIXES` for one rule."""

    #: Canonical rule id, e.g. ``W138``.
    rule: str
    #: What the fix does, for ``docs/validation-rules.md``. One sentence, no
    #: trailing full stop, third person — it is read as a table cell.
    summary: str
    #: The producer itself.
    produce: FixProducer
    #: The repairs this rule can offer, when it offers more than one. Empty when
    #: there is exactly one and it therefore needs no name to pick it by.
    choices: tuple[Choice, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        """Does this rule admit more than one plausible repair?"""
        return len(self.choices) > 1
