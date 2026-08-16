"""The command line that would make one operation.

An edit explored in the browser has to be able to leave it. A pull-request
description, a runbook, a colleague's terminal — none of those take a JSON
operation list, and all of them take ``netviz edit …``. This module renders
one into the other.

The rule is that what comes out must **do the same thing**, not merely look
like it. Where a subcommand exists that takes exactly the operation's arguments,
the operation becomes that subcommand. Where one does not — a whole-file write,
a stored arrangement, an interface mapping richer than ``--field`` can express —
the operation is emitted as ``netviz edit apply``, which takes the JSON form
verbatim and is therefore exact by construction. There is deliberately no third
case where a rendering *approximates* an operation: a command list that quietly
drops the length of a cable is worse than one with a JSON blob in it.

Quoting is POSIX (:func:`shlex.quote`), because the output is meant to be pasted
into a shell. A Windows reader gets something they have to adjust, which is the
same trade every ``docs/`` transcript already makes.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from netviz.edit.operations import (
    AddInterface,
    Connect,
    CopyElement,
    CreateElement,
    DeleteElement,
    Disconnect,
    MoveElement,
    Operation,
    RemoveInterface,
    RenameElement,
    SetField,
    UnsetField,
)
from netviz.models.scalars import format_bitrate

__all__ = ["INVENTORY_PLACEHOLDER", "command_for", "command_list", "commands_text"]

#: What ``-i`` is filled in with when the caller names no inventory. A reader
#: pasting this has to point it at their own tree, and a placeholder that looks
#: like a path is a placeholder somebody runs by accident.
INVENTORY_PLACEHOLDER: Final = "INVENTORY"

#: Fields of an interface mapping that ``edit add-interface`` has a flag for.
#: Anything else has to go through ``--field``, and anything ``--field`` cannot
#: express — a nested mapping, a list — sends the whole operation to
#: ``edit apply``.
_INTERFACE_FLAGS: Final[dict[str, str]] = {"type": "--type", "description": "--description"}


def command_list(
    operations: Iterable[Operation], *, inventory: str | None = None
) -> tuple[str, ...]:
    """One command line per operation, in the order they were applied.

    Args:
        operations: What the session did, forward.
        inventory: What to put after ``-i``. ``None`` uses
            :data:`INVENTORY_PLACEHOLDER`.

    Returns:
        Lines ready to paste into a shell, in order. Replaying them against the
        state the session started from reproduces the session.
    """
    return tuple(command_for(operation, inventory=inventory) for operation in operations)


def command_for(operation: Operation, *, inventory: str | None = None) -> str:
    """The ``netviz edit`` invocation equivalent to ``operation``.

    Never lossy: an operation no subcommand takes exactly comes back as
    ``netviz edit apply -f -`` with the operation's own JSON on standard
    input, which is the same write path by a different door.
    """
    prefix = ["netviz", "-i", inventory or INVENTORY_PLACEHOLDER, "edit"]
    arguments = _arguments(operation)
    if arguments is None:
        payload = json.dumps([operation.to_dict()], separators=(",", ":"), sort_keys=True)
        return f"echo {shlex.quote(payload)} | {shlex.join([*prefix, 'apply', '-f', '-'])}"
    return shlex.join([*prefix, *arguments])


def _arguments(operation: Operation) -> list[str] | None:
    """The subcommand and its arguments, or ``None`` for "no exact spelling"."""
    if isinstance(operation, CreateElement):
        return _create(operation)
    if isinstance(operation, CopyElement):
        return _copy(operation)
    if isinstance(operation, DeleteElement):
        return ["delete", operation.address, *(["--cascade"] if operation.cascade else [])]
    if isinstance(operation, RenameElement):
        return ["rename", operation.address, operation.new_name]
    if isinstance(operation, MoveElement):
        # ``--index`` has no flag: an inverse that puts a document back at
        # position 2 of its file cannot be spelled, so it goes through apply.
        return None if operation.index is not None else ["move", operation.address, operation.file]
    if isinstance(operation, SetField):
        return ["set", operation.address, operation.path, _scalar(operation.value)]
    if isinstance(operation, UnsetField):
        return ["unset", operation.address, operation.path]
    if isinstance(operation, AddInterface):
        return _add_interface(operation)
    if isinstance(operation, RemoveInterface):
        return [
            "remove-interface",
            operation.address,
            operation.name,
            *(["--cascade"] if operation.cascade else []),
        ]
    if isinstance(operation, Connect):
        return _connect(operation)
    if isinstance(operation, Disconnect):
        return ["disconnect", operation.address]
    # ``set-geometry``, ``write-file`` and ``remove-file`` have no subcommand at
    # all: the first is what ``netviz layout`` writes, and the other two are
    # inverses that restore bytes rather than intentions.
    return None


def _create(operation: CreateElement) -> list[str] | None:
    arguments = ["create", operation.kind, operation.name]
    if operation.namespace:
        arguments += ["--namespace", operation.namespace]
    if operation.spec:
        arguments += ["--spec", json.dumps(operation.spec, sort_keys=True)]
    if operation.metadata:
        arguments += ["--metadata", json.dumps(operation.metadata, sort_keys=True)]
    if operation.file is not None:
        arguments += ["--file", operation.file]
    return arguments


def _copy(operation: CopyElement) -> list[str] | None:
    """``copy``, or ``duplicate`` where that is the same request said shorter.

    ``duplicate`` is a copy that stays where it is and takes the name it is
    given: no ``--to``, no ``--name``. Rendering that as ``duplicate`` is exact
    rather than approximate — the CLI builds the identical operation — and it is
    what somebody reading the script pressed.

    A copy carrying a ``rewrite`` map has no spelling at all: the map is what
    makes a cloned cable join the cloned switches, and it is decided over the
    *whole selection* by :func:`~netviz.edit.clipboard.copy_plan`. There is no
    flag that could carry it, and a rendering that dropped it would produce a
    cable joining the originals — so it goes through ``apply``, verbatim.
    """
    if operation.rewrite or operation.keep_unique or operation.file is not None:
        return None
    named = operation.namespace is not None or operation.name is not None
    arguments = ["copy" if named else "duplicate", operation.address]
    if operation.namespace is not None:
        # Including the empty string, which is the root namespace and is a
        # different request from "leave it where it is".
        arguments += ["--to", operation.namespace]
    if operation.name:
        arguments += ["--name", operation.name]
    if operation.suffix != "copy":
        arguments += ["--suffix", operation.suffix]
    return arguments


def _connect(operation: Connect) -> list[str] | None:
    arguments = ["connect", operation.a, operation.b]
    for key, flag in (("medium", "--medium"), ("speed", "--speed"), ("label", "--label")):
        if key not in operation.spec:
            continue
        value = operation.spec[key]
        if key == "speed" and isinstance(value, int) and not isinstance(value, bool):
            # ``--speed`` takes ``1Gbps``, not a bare count of bits per second,
            # and ``format_bitrate`` picks the largest *exact* unit — so this
            # round-trips rather than rounding the rate on the way out.
            value = format_bitrate(value)
        arguments += [flag, _scalar(value)]
    if set(operation.spec) - {"medium", "speed", "label"}:
        # ``length_m``, ``category``, ``connector`` … have no flag on ``connect``.
        # Dropping them would change the cable, so the whole thing goes to apply.
        return None
    for value, flag in (
        (operation.name, "--name"),
        (operation.namespace, "--namespace"),
        (operation.file, "--file"),
    ):
        if value is not None:
            arguments += [flag, value]
    return arguments


def _add_interface(operation: AddInterface) -> list[str] | None:
    interface: Mapping[str, Any] = operation.interface
    name = interface.get("name")
    if operation.index is not None or not isinstance(name, str):
        return None
    arguments = ["add-interface", operation.address, name]
    for key, value in interface.items():
        if key == "name":
            continue
        if key in _INTERFACE_FLAGS:
            arguments += [_INTERFACE_FLAGS[key], _scalar(value)]
        elif isinstance(value, (str, int, float, bool)):
            arguments += ["--field", f"{key}={_scalar(value)}"]
        else:
            # A nested mapping — ``ipv4.addresses`` — is not something
            # ``--field`` can carry, and half an interface is not an interface.
            return None
    return arguments


def _scalar(value: Any) -> str:
    """A value as ``netviz edit set`` would be typed it.

    ``edit set`` parses its argument as JSON and falls back to a plain string,
    so a string is passed through and everything else is written as JSON — which
    is what makes ``true`` a boolean and ``1500`` a number on the way back in.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def commands_text(commands: Sequence[str]) -> str:
    """A command list as one block, newline-terminated, ready for a clipboard."""
    return "".join(f"{command}\n" for command in commands)
