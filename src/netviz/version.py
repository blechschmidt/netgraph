"""What ``netviz --version`` reports, and the machine-readable form of it.

A version number on its own answers almost none of the questions a bug report
raises. Nearly every interesting failure in this tool is a failure of something
*around* it: Graphviz is absent, or is a version whose ``dot`` rejects an
attribute; PyYAML was built without libyaml so the parser is the pure-Python one
and the error positions differ; pydantic is a major version behind what the
models were written against. So the version report names the whole set:

* **netviz** itself, from the installed distribution metadata.
* **Python** — version, implementation and the interpreter path, because a
  ``netviz`` on ``PATH`` frequently belongs to an environment the reporter did
  not mean to be in.
* **Graphviz** — the ``dot`` that :func:`netviz.render.dot.find_dot` would
  pick, and the version it reports. Absent Graphviz is recorded as absent rather
  than as an error: every ``dot``, ``mermaid`` and ``json`` render works without
  it, so it is a fact about the installation and not a fault.
* **The YAML parser actually selected** — libyaml or pure Python. This is the one
  entry that cannot be derived from a list of installed packages, and it changes
  loader behaviour (see :mod:`netviz.loader.documents`).
* **The runtime dependencies**, at the versions resolved in this environment.

Two shapes, one source. :func:`format_text` is what a human reads and
:func:`as_dict` is what gets pasted into an issue; both are built from the same
:class:`VersionReport`, so they cannot disagree.

Nothing here raises. A version report that fails is a bug report that never gets
filed, so every lookup degrades to ``None`` and says so.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Final

from netviz import __version__

__all__ = [
    "DEPENDENCIES",
    "REPORT_SCHEMA_VERSION",
    "VersionReport",
    "as_dict",
    "collect",
    "format_text",
    "graphviz_version",
]

#: Version of the ``--output-format json`` document below. Bumped only when an
#: existing key changes meaning or disappears; adding a key does not bump it.
#: Same contract as the ``validate`` and ``drift`` envelopes.
REPORT_SCHEMA_VERSION: Final = 1

#: The distributions worth naming in a bug report: netviz's own runtime
#: requirements, in the order ``pyproject.toml`` lists them. ``tomli`` is absent
#: because it only exists below Python 3.11 and its presence is already implied
#: by the interpreter version; ``ruamel.yaml`` and ``watchfiles`` are here
#: because ``fmt`` and ``watch`` are the two commands whose failures are most
#: often theirs rather than ours.
DEPENDENCIES: Final = (
    "pydantic",
    "PyYAML",
    "click",
    "networkx",
    "jinja2",
    "watchfiles",
    "ruamel.yaml",
)

#: ``dot - graphviz version 2.43.0 (0)`` -- what ``dot -V`` writes, on stderr.
_DOT_VERSION_RE: Final = re.compile(
    r"graphviz\s+version\s+(?P<version>[0-9][^\s(]*)", re.IGNORECASE
)

#: ``dot -V`` prints one line and exits. A second is already pathological, so
#: this is short on purpose: ``--version`` must stay usable as the first thing a
#: confused user types.
_DOT_TIMEOUT_SECONDS: Final = 5.0


@dataclass(frozen=True)
class VersionReport:
    """Every version that decides how this installation behaves."""

    #: The installed netviz distribution, or ``0.0.0.dev0`` from a source tree
    #: that was never installed.
    netviz: str
    #: ``3.12.3``. Not ``sys.version``, which carries a build date and compiler.
    python: str
    #: ``CPython``, ``PyPy``, …
    python_implementation: str
    #: ``sys.executable``. Empty on an embedded interpreter, which is why it is
    #: reported rather than assumed.
    python_executable: str
    #: ``platform.platform()`` -- one string, deliberately not parsed.
    platform: str
    #: The Graphviz version ``dot -V`` reported, or ``None`` if there is no
    #: ``dot`` or it could not be asked.
    graphviz: str | None
    #: The executable :func:`~netviz.render.dot.find_dot` chose, if any.
    graphviz_path: str | None
    #: Why :attr:`graphviz` is ``None`` while :attr:`graphviz_path` is not: a
    #: binary that would not run, timed out, or printed something unparseable.
    graphviz_error: str | None
    #: ``libyaml`` or ``python`` -- which parser :mod:`netviz.loader.documents`
    #: actually selected, honouring ``NETVIZ_YAML_LOADER``.
    yaml_parser: str
    #: Distribution name to version, for everything in :data:`DEPENDENCIES` that
    #: is installed. A missing entry means the distribution is not installed,
    #: which for the lazily-imported ones is a supported state.
    dependencies: dict[str, str]


def graphviz_version(executable: str) -> tuple[str | None, str | None]:
    """Ask ``executable`` for its version. Returns ``(version, error)``.

    Exactly one of the two is ``None``. The error is a short phrase rather than a
    traceback, because it is going into a one-line report next to the path that
    produced it.
    """
    try:
        completed = subprocess.run(
            [executable, "-V"],
            capture_output=True,
            timeout=_DOT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"did not answer '-V' within {_DOT_TIMEOUT_SECONDS:g}s"
    except OSError as exc:
        return None, exc.strerror or str(exc)

    # Graphviz writes its banner to stderr; some builds and some wrappers write
    # it to stdout instead, so both are searched rather than guessed at.
    banner = (completed.stderr + completed.stdout).decode("utf-8", "replace")
    match = _DOT_VERSION_RE.search(banner)
    if match is not None:
        return match.group("version"), None
    if completed.returncode != 0:
        return None, f"exited {completed.returncode}"
    first_line = banner.strip().splitlines()[0] if banner.strip() else "nothing"
    return None, f"printed {first_line!r} rather than a version"


def _distribution_versions() -> dict[str, str]:
    """The installed versions of :data:`DEPENDENCIES`, skipping what is absent."""
    found: dict[str, str] = {}
    for name in DEPENDENCIES:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return found


def _yaml_parser() -> str:
    """Which loader ``netviz.loader.documents`` bound, as one word.

    Imported here rather than at module scope so that ``--version`` does not pay
    for the loader on a machine where the interesting question is why the import
    fails; the ``except`` covers exactly that case.
    """
    try:
        from netviz.loader.documents import StrictSafeLoader
    except Exception:  # pragma: no cover - a broken PyYAML install
        return "unknown"
    return "python" if StrictSafeLoader.__name__.startswith("Pure") else "libyaml"


def collect() -> VersionReport:
    """Gather the report. Costs one ``dot -V`` and nothing else measurable."""
    # Local import: ``netviz.render.dot`` pulls in Jinja2 and the template
    # environment, which ``--version`` has no other use for.
    from netviz.render.dot import find_dot

    executable = find_dot()
    version, error = graphviz_version(executable) if executable is not None else (None, None)

    return VersionReport(
        netviz=__version__,
        python=platform.python_version(),
        python_implementation=platform.python_implementation(),
        python_executable=sys.executable,
        platform=platform.platform(),
        graphviz=version,
        graphviz_path=executable,
        graphviz_error=error,
        yaml_parser=_yaml_parser(),
        dependencies=_distribution_versions(),
    )


def _graphviz_line(report: VersionReport) -> str:
    """The one line that has four different things to say."""
    if report.graphviz_path is None:
        return "Graphviz     not found (dot, mermaid and json still render)"
    where = f" at {report.graphviz_path}"
    if report.graphviz is not None:
        return f"Graphviz     {report.graphviz}{where}"
    return f"Graphviz     unknown{where} ({report.graphviz_error})"


def format_text(report: VersionReport) -> str:
    """The human form: the version on the first line, the context under it.

    The first line is ``netviz <version>`` and nothing else, so that the
    thousand scripts which do ``netviz --version | cut -d' ' -f2`` keep
    working across this change.
    """
    dependencies = ", ".join(f"{name} {value}" for name, value in report.dependencies.items())
    lines = [
        f"netviz {report.netviz}",
        f"Python       {report.python} ({report.python_implementation})"
        + (f" at {report.python_executable}" if report.python_executable else ""),
        _graphviz_line(report),
        f"Platform     {report.platform}",
        f"YAML parser  {report.yaml_parser}",
    ]
    if dependencies:
        lines.append(f"Dependencies {dependencies}")
    return "\n".join(lines) + "\n"


def as_dict(report: VersionReport) -> dict[str, Any]:
    """The machine form, for pasting into an issue or asserting on in a test.

    ``graphviz`` is an object rather than a string because "absent" and "present
    but unaskable" are different states and a consumer deciding whether to
    expect an SVG needs to tell them apart.
    """
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "netviz": report.netviz,
        "python": {
            "version": report.python,
            "implementation": report.python_implementation,
            "executable": report.python_executable,
        },
        "graphviz": {
            "version": report.graphviz,
            "path": report.graphviz_path,
            "error": report.graphviz_error,
        },
        "platform": {
            "description": report.platform,
            "system": platform.system(),
            "machine": platform.machine(),
            "os": os.name,
        },
        "yamlParser": report.yaml_parser,
        "dependencies": dict(report.dependencies),
    }
