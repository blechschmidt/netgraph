"""Skip conditions for the handful of tests that are genuinely POSIX-only.

The suite runs on `ubuntu-24.04`, `windows-latest` and `macos-14`; see
``docs/testing.md`` for what each of those covers. The default is that a test
runs everywhere — a test skipped on Windows is a behaviour nothing checks there,
so each mark below has to name a *capability the platform does not have*, not a
platform netgraph has not been made to work on.

That distinction is why these are six narrow marks rather than one
``skip_on_windows``. ``chmod(0o000)`` cannot make a directory unreadable on
Windows because there are no permission bits to set; ``os.mkfifo`` does not exist
there; a symlink needs a privilege an unelevated process does not hold. Those are
facts about the platform. "The loader mishandles paths on Windows" would not be,
and must never be spelled with one of these.

Three of the marks are not about Windows at all. ``requires_dot``,
``requires_node`` and ``requires_nft`` are about a tool the environment may not
be able to run, on any platform, and they live here because the reason to skip
is the same kind of reason: the environment cannot run the test, so the test
says so rather than failing.

Not everything here is a mark. :data:`ON_WINDOWS`, :data:`PWSH` and
:data:`HAVE_BASH_COMPLETION` are the same measurements exposed as values, for the
tests that do not skip on the answer but change what they expect — a directory
name the platform will accept, a transcript the platform's own shell adds a line
to.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

import pytest

from netgraph.render.dot import find_dot

__all__ = [
    "HAVE_BASH_COMPLETION",
    "NFT",
    "ON_WINDOWS",
    "PWSH",
    "requires_dot",
    "requires_mkfifo",
    "requires_nft",
    "requires_node",
    "requires_posix_permissions",
    "requires_posix_shell",
    "requires_pwsh",
    "requires_symlinks",
    "requires_unexpanded_globs",
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

#: A wildcard that reaches the program it was typed for. On Windows it does not:
#: the MSYS runtime Git Bash is built on expands wildcards in the arguments it
#: hands to a *native* program, after the shell has finished with them — so
#: ``set -f`` in the script cannot keep a glob a glob. ``MSYS=noglob`` would,
#: and also changes how the command line is taken apart, which broke more than
#: it fixed; the platform is documented instead.
requires_unexpanded_globs = pytest.mark.skipif(
    ON_WINDOWS,
    reason="POSIX-only: the MSYS runtime expands wildcards in the arguments Git Bash "
    "hands to a native program, which no 'set -f' in the script can prevent",
)


def _bash_is_modern_enough_for_completion() -> bool:
    """Is the ``bash`` on ``PATH`` new enough for Click's completion script?

    Click refuses to vouch for the script it generates on anything older than
    4.4 and says so on stderr — which is a line of output, and therefore part of
    any transcript that runs ``netgraph completion bash``.

    macOS is where this bites: Apple has shipped ``/bin/bash`` 3.2 since 2007,
    for licensing reasons that have nothing to do with the shell, so the warning
    is the *correct* behaviour there and the documented transcript is the correct
    one everywhere else.

    The three details below are Click's, copied deliberately rather than
    approximated (``BashComplete._check_version``), and
    ``tests/test_platform.py`` fails if the two ever disagree:

    * The **resolved** path is run, not the bare name ``bash``. On Windows those
      are different programs: ``shutil.which`` finds Git Bash on ``PATH``, while
      ``CreateProcess`` searches ``System32`` first and finds WSL's ``bash.exe``,
      which on a runner with no distro installed answers nothing at all.
    * ``--norc``, so a login file's banner cannot appear where the version is.
    * The version must have three components at the very start of the output.
    """
    bash = shutil.which("bash")
    if bash is None:
        return False
    try:
        completed = subprocess.run(
            [bash, "--norc", "-c", 'echo "${BASH_VERSION}"'],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no bash to ask
        return False
    match = re.search(r"^(\d+)\.(\d+)\.\d+", completed.stdout)
    return match is not None and (int(match[1]), int(match[2])) >= (4, 4)


#: Bash 4.4 or newer, which is what Click's generated completion script needs.
#: See :func:`_bash_is_modern_enough_for_completion`.
HAVE_BASH_COMPLETION: Final = _bash_is_modern_enough_for_completion()

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


def _nft_that_can_check() -> str | None:
    """The ``nft`` binary that can syntax-check a ruleset here, or ``None``.

    ``nft --check`` is documented as parsing a file without committing it, and
    that is what it is used for -- but before it reads a byte it opens a netlink
    socket and populates its cache, which needs ``CAP_NET_ADMIN``. So "is nft
    installed" is the wrong question and :func:`shutil.which` is the wrong way to
    ask it: on the GitHub Linux runners nft *is* on ``PATH`` and the job is
    unprivileged, so every ``--check`` failed with ``cache initialization
    failed: Operation not permitted`` -- a verdict about the process, reported
    as though the generated ruleset were malformed.

    The capability is therefore measured, by handing nft the smallest ruleset
    every version of it accepts. If that comes back clean, nft can parse here and
    a later failure is netgraph's; if it does not, nothing about the ruleset has
    been established either way, and the tests that need it say so rather than
    failing. CI grants the capability (see ``.github/workflows/ci.yml``) so the
    gate runs there rather than skipping.
    """
    nft = shutil.which("nft")
    if nft is None:
        return None
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "probe.nft"
        probe.write_text(
            "table inet netgraph_probe {\n\tchain c {\n\t\ttype filter hook input priority 0;\n\t}\n}\n",
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                [nft, "--check", "-f", str(probe)], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - nft is on PATH
            return None
    return nft if completed.returncode == 0 else None


#: The ``nft`` that can syntax-check the generated nftables ruleset, or ``None``.
#: See :func:`_nft_that_can_check`.
NFT: Final = _nft_that_can_check()

#: ``nft --check``, the one gate in tests/test_firewall.py that is not netgraph
#: reading its own output.
requires_nft = pytest.mark.skipif(
    NFT is None,
    reason="no usable 'nft' to syntax-check the generated ruleset with: it is either not "
    "installed, or this process may not open the netlink socket '--check' needs",
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
