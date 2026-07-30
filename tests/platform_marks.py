"""Skip conditions for the handful of tests that are genuinely POSIX-only.

The suite runs on `ubuntu-24.04`, `windows-latest` and `macos-14`; see
``docs/testing.md`` for what each of those covers. The default is that a test
runs everywhere — a test skipped on Windows is a behaviour nothing checks there,
so each mark below has to name a *capability the platform does not have*, not a
platform netgraph has not been made to work on.

That distinction is why these are five narrow marks rather than one
``skip_on_windows``. ``chmod(0o000)`` cannot make a directory unreadable on
Windows because there are no permission bits to set; ``os.mkfifo`` does not exist
there; a symlink needs a privilege an unelevated process does not hold. Those are
facts about the platform. "The loader mishandles paths on Windows" would not be,
and must never be spelled with one of these.

Two of the marks are not about Windows at all. ``requires_dot`` and
``requires_node`` are about a tool that may not be installed, on any platform,
and they live here because the reason to skip is the same kind of reason: the
environment cannot run the test, so the test says so rather than failing.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Final

import pytest

from netgraph.render.dot import find_dot

__all__ = [
    "ON_WINDOWS",
    "PWSH",
    "requires_dot",
    "requires_mkfifo",
    "requires_node",
    "requires_posix_permissions",
    "requires_posix_shell",
    "requires_pwsh",
    "requires_symlinks",
]

#: Windows, spelled once. ``os.name`` rather than ``sys.platform`` because the
#: distinction that matters here is the API family, and ``sys.platform`` is
#: ``"win32"`` on 64-bit Windows, which reads like a bug every time.
ON_WINDOWS: Final = os.name == "nt"


def _can_symlink() -> bool:
    """Can this process create a symlink at all?

    On POSIX, always. On Windows, only with Developer Mode enabled or from an
    elevated process — and the answer cannot be derived from a version number, so
    it is measured: create one in the temporary directory and see.
    """
    if not ON_WINDOWS:
        return True
    import tempfile  # pragma: no cover - Windows only

    with tempfile.TemporaryDirectory() as directory:  # pragma: no cover - Windows only
        link = Path(directory) / "link"
        try:
            link.symlink_to(Path(directory))
        except (OSError, NotImplementedError):
            return False
        return True


#: An unreadable directory, staged with ``chmod(0o000)``. Windows has no POSIX
#: mode bits, so the call succeeds and changes nothing but the read-only flag:
#: the directory stays readable and the test would assert that netgraph fails to
#: report a problem that is not there.
requires_posix_permissions = pytest.mark.skipif(
    ON_WINDOWS,
    reason="POSIX-only: Windows has no permission bits, so chmod(0o000) cannot "
    "make a directory unreadable and there is nothing for the loader to report",
)

#: A symlink. Present on Windows since Vista, but creating one needs
#: SeCreateSymbolicLinkPrivilege, which an ordinary CI process does not have
#: unless Developer Mode is on. Measured rather than assumed, so the tests do run
#: on a developer's machine that has it.
requires_symlinks = pytest.mark.skipif(
    not _can_symlink(),
    reason="POSIX-only in practice: creating a symlink on Windows needs Developer "
    "Mode or an elevated process, and this one is neither",
)

#: A FIFO, used to check that the loader skips what is not a regular file.
#: ``os.mkfifo`` does not exist on Windows; named pipes there are a different API
#: with different semantics and are not something a YAML tree can contain.
requires_mkfifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="POSIX-only: os.mkfifo does not exist on Windows, and a Windows named "
    "pipe is not a filesystem entry the loader could walk into",
)

#: ``sh -n``, which is how the generated route script is syntax-checked. Windows
#: has no POSIX shell; the *script* is still asserted line by line there, only
#: the second opinion from ``sh`` is missing.
requires_posix_shell = pytest.mark.skipif(
    shutil.which("sh") is None,
    reason="POSIX-only: no 'sh' to syntax-check the generated shell script with",
)

#: The Graphviz layout engine. Not a platform question — it is a system package
#: on every platform, and CI installs it on all three — but a test that shells
#: out to it has to say what it needs.
#:
#: Asked through :func:`~netgraph.render.dot.find_dot` rather than
#: :func:`shutil.which`, so the condition is the same one netgraph applies: on
#: Windows Graphviz is frequently installed without being on ``PATH``, and a
#: suite that skipped there while the tool renders happily would be measuring
#: ``PATH`` rather than netgraph.
requires_dot = pytest.mark.skipif(
    find_dot() is None, reason="the Graphviz 'dot' executable is not installed"
)

#: Node, for syntax-checking the JavaScript the HTML renderer inlines.
requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is not installed; 'node --check' cannot run"
)

#: The PowerShell binary that can parse the completion script netgraph generates,
#: or ``None``. ``pwsh`` (PowerShell 7, cross-platform) is preferred over
#: ``powershell`` (5.1, Windows only) because all three GitHub runner images carry
#: it — so the generated script is checked by the shell it is *for* on Linux and
#: macOS too, not only where it is used.
PWSH: Final = shutil.which("pwsh") or shutil.which("powershell")

#: PowerShell itself, for the one thing no Python assertion can establish: that
#: what netgraph emits is script PowerShell accepts.
requires_pwsh = pytest.mark.skipif(
    PWSH is None,
    reason="neither 'pwsh' nor 'powershell' is installed; the generated completion "
    "script cannot be checked by the shell it is for",
)
