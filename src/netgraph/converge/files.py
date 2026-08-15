"""The per-dialect half: what file has to change, computed by generating both sides.

netplan, systemd-networkd, ifupdown, FRR and wg-quick are *declarative*. None of
them has a command for "give this interface an MTU of 1500" -- the minimal
remediation is genuinely "make the file say this, then reload", and a converge
plan that pretended otherwise would be inventing a command language no box
speaks.

So minimality here is at the file level, and it is real rather than assumed. The
existing emitters in :mod:`netgraph.export.config` are run **twice**:

* over the declared inventory, giving what the device should have;
* over the *observed* inventory -- the declaration with every observation folded
  into it, which is what :func:`netgraph.plan.live.adopt` already builds for
  ``netgraph plan --from-live`` -- giving what the device has now.

A file appears in the plan only if the two differ, ignoring the generated banner
(which names the inventory documents behind the file and is not part of what the
device does). A file the observed side has and the declared side does not is
removed. That means a device that drifted in one interface gets one file, and a
device whose drift the dialect cannot express gets none -- with the difference
still visible in the plan through the neutral intent lines, so nothing is hidden.

The same symmetry gives ``--rollback`` for free: the inverse of "write the
declared file" is "write the observed one", and the observed one was generated
from a measurement rather than reconstructed from a diff.

Which reload command each dialect gets is in :data:`ACTIVATION`, and each is the
narrowest one that works: ``vtysh -b`` re-reads ``frr.conf`` without restarting
the daemons, ``wg-quick`` is bounced per tunnel rather than per host.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Final

from netgraph.converge.model import Command
from netgraph.export.config import ConfigSet, UnsupportedConfigError, generate
from netgraph.export.context import ExportContext

__all__ = [
    "ACTIVATION",
    "DECLARATIVE",
    "FileChange",
    "file_changes",
    "strip_banner",
]

#: The dialects whose remediation is a file rather than a command. ``interfaces``
#: is absent because nothing reads it -- it is netgraph's own projection, so a
#: converge plan in that dialect is the neutral command list and nothing else.
DECLARATIVE: Final[tuple[str, ...]] = ("netplan", "networkd", "ifupdown", "frr", "wireguard")


def _wireguard_activation(path: str) -> tuple[str, ...]:
    """``wg-quick`` down-and-up for the one tunnel this file configures.

    The interface name is the file's stem, because that is what wg-quick means
    by its argument: ``/etc/wireguard/wg0.conf`` is the interface ``wg0``.
    """
    name = path.rsplit("/", 1)[-1].removesuffix(".conf")
    return (f"wg-quick down {name} || true", f"wg-quick up {name}")


#: What to run after a file of each dialect has been put in place. A callable of
#: the file's absolute path, because two of the five are per-interface.
ACTIVATION: Final[Mapping[str, Callable[[str], tuple[str, ...]]]] = {
    "netplan": lambda path: ("netplan apply",),
    "networkd": lambda path: ("networkctl reload",),
    "ifupdown": lambda path: ("systemctl restart networking",),
    "frr": lambda path: ("vtysh -b",),
    "wireguard": _wireguard_activation,
}


@dataclass(frozen=True, slots=True)
class FileChange:
    """One generated file that has to be put in place, or taken away."""

    #: Absolute path on the device: ``/etc/netplan/10-netgraph.yaml``.
    path: str
    #: The file as the declared inventory renders it, or ``None`` when the
    #: declared side does not produce this file at all and it must be removed.
    declared: str | None
    #: The file as the observed state renders it, or ``None`` when the device
    #: does not have it yet. This is what ``--rollback`` writes back.
    observed: str | None
    #: What to run once it is in place.
    activation: tuple[str, ...] = ()

    @property
    def removed(self) -> bool:
        return self.declared is None

    @property
    def created(self) -> bool:
        return self.observed is None

    def commands(self) -> tuple[Command, ...]:
        """Put the declared file in place and make the device use it."""
        if self.declared is None:
            return (
                Command(text=f"rm -f {self.path}"),
                *(Command(text=line) for line in self.activation),
            )
        return (
            Command(
                text=f"write {self.path}",
                kind="write",
                path=self.path,
                content=self.declared,
            ),
            *(Command(text=line) for line in self.activation),
        )

    def rollback(self) -> tuple[Command, ...]:
        """Put back what the capture found, or remove what was not there."""
        if self.observed is None:
            return (
                Command(text=f"rm -f {self.path}"),
                *(Command(text=line) for line in self.activation),
            )
        return (
            Command(
                text=f"write {self.path}",
                kind="write",
                path=self.path,
                content=self.observed,
            ),
            *(Command(text=line) for line in self.activation),
        )


def file_changes(
    dialect: str,
    declared: ExportContext,
    observed: ExportContext,
    elements: Collection[str],
) -> tuple[Mapping[str, tuple[FileChange, ...]], tuple[str, ...]]:
    """Per device, the files of ``dialect`` whose content differs between the two.

    Args:
        dialect: One of :data:`DECLARATIVE`.
        declared: An export context over the declared inventory.
        observed: One over the inventory with the capture folded in.
        elements: The devices the plan covers -- the ones something drifted on,
            and that the *declared* inventory holds. Nothing outside it gets a
            file: a device with no drift is a device whose observable state
            already matches, so writing its configuration would be a change
            nobody asked for, and a device the inventory does not declare has no
            declared configuration to converge on to.

    Returns:
        ``(changes, notes)``. ``changes`` maps a fully-qualified device name to
        its file changes, in the order the dialect writes them, which is the
        order they should be applied: a ``.netdev`` before the ``.network`` that
        refers to it. ``notes`` is what the reader has to know about how the
        comparison was made.

    Raises:
        UnsupportedConfigError: The dialect cannot express a device *as the
            inventory declares it*. Propagated deliberately -- half a
            configuration on a real box is worse than none, and that judgement
            is already made in :func:`netgraph.export.config.generate`.
    """
    wanted = _by_device(generate(dialect, declared))
    current, notes = _observed_side(dialect, observed)
    activation = ACTIVATION[dialect]

    changes: dict[str, tuple[FileChange, ...]] = {}
    for element in sorted({*wanted, *current} & set(elements)):
        mine = wanted.get(element, {})
        theirs = current.get(element, {})
        entries: list[FileChange] = []
        for path in [*mine, *(name for name in theirs if name not in mine)]:
            declared_text = mine.get(path)
            observed_text = theirs.get(path)
            unchanged = (
                declared_text is not None
                and observed_text is not None
                and strip_banner(declared_text) == strip_banner(observed_text)
            )
            if unchanged:
                continue
            absolute = f"/{path}"
            entries.append(
                FileChange(
                    path=absolute,
                    declared=declared_text,
                    observed=observed_text,
                    activation=activation(absolute),
                )
            )
        if entries:
            changes[element] = tuple(entries)
    return (changes, notes)


def _observed_side(
    dialect: str, observed: ExportContext
) -> tuple[dict[str, dict[str, str]], tuple[str, ...]]:
    """What the devices are running now, or an admission that it is not knowable.

    The observed state is the declaration with the capture folded in, and a
    capture can put a device into a shape the dialect has no syntax for -- a
    port found trunking a VLAN, on a host whose dialect only writes access
    ports. That is a refusal about a *measurement*, not about the inventory, and
    failing the whole command over it would mean a device that drifted into an
    unrepresentable state could never be converged back out of one.

    So the refusal is caught here and downgraded: with no baseline, every
    declared file is treated as a change, which is the conservative answer --
    the plan writes each file rather than assuming one already matches -- and
    the reason is carried out as a note so the plan says why it is not minimal.
    The note also warns about the *rollback*, which is where the missing baseline
    actually costs something: with nothing measured to restore, the inverse of
    "install this file" can only be "remove it".
    """
    try:
        return (_by_device(generate(dialect, observed)), ())
    except UnsupportedConfigError as refusal:
        return (
            {},
            (
                f"the {dialect!r} dialect has no syntax for the state the capture found "
                f"({refusal.refusals[0]}), so there is no baseline to diff against: every "
                "generated file is listed whether or not the device already has it, and "
                "--rollback removes each one rather than restoring what was there",
            ),
        )


def _by_device(config: ConfigSet) -> dict[str, dict[str, str]]:
    """``{device: {system path: content}}``, keeping the dialect's file order."""
    return {
        device.element: {entry.path: entry.content for entry in device.files}
        for device in config.devices
    }


def strip_banner(text: str) -> str:
    """``text`` without its leading comment block.

    Every generated configuration opens with
    :func:`~netgraph.export.config.header.config_header`, which names the
    inventory documents the file was rendered from. Those names differ between
    the declared tree and the adopted one -- an adopted document may have a
    synthetic source -- and a file is not *different* because its provenance
    comment is. What the device does starts after the banner, and that is what
    is compared.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines) and (
        not lines[index].strip() or lines[index].lstrip().startswith(("#", "!"))
    ):
        index += 1
    return "\n".join(lines[index:]).strip()
