"""What netviz promises on Windows and macOS, asserted where it can be.

Every test here runs on all three platforms. That is deliberate and it is the
whole point of the file: most of these behaviours are *about* Windows, and if
they were guarded with ``skipif`` they would be checked nowhere until somebody
happened to run the suite there. So the platform-dependent branches are reached
by naming the platform explicitly — ``monkeypatch.setattr(os, "name", "nt")``,
``_WELL_KNOWN_DIRS["nt"]`` — and the platform-independent contract (a canonical
form defined in bytes, a reserved file name refused everywhere) is asserted
directly.

Two things this cannot do, which the ``windows-latest`` and ``macos-14`` CI jobs
are for: run the real ``os.replace`` against a real open handle, and drive a real
``ReadDirectoryChangesW`` / FSEvents watcher. See ``docs/testing.md``.

The PowerShell completion script *is* checked for real, and everywhere: ``pwsh``
is preinstalled on all three runner images, so the script netviz generates is
parsed, registered and then driven through PowerShell's own ``CompleteInput`` on
Linux and macOS as well as on Windows.

The marks in ``tests/platform_marks.py`` are the other half of this: what is
skipped there, and with what reason.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from netviz.cli import cli
from netviz.completion import PROG_NAME, SHELLS, PowerShellComplete, completion_script
from netviz.errors import RenderError
from netviz.fmt import Mode, format_paths
from netviz.fmt.runner import diff_text, display_path
from netviz.fsio import (
    RESERVED_FILE_STEMS,
    is_reserved_file_stem,
    replace_atomically,
    safe_file_stem,
    write_bytes_atomically,
    write_text,
    write_text_atomically,
)
from netviz.httpserve import LocalServer, bind
from netviz.importer import build_files
from netviz.importer.draft import Draft
from netviz.render.dot import (
    _WELL_KNOWN_DIRS,
    DOT_ENV_VAR,
    DOT_EXECUTABLE,
    find_dot,
    graphviz_install_hint,
    missing_dot_message,
)
from netviz.scaffold import build_scaffold, write_scaffold
from netviz.watch.loop import _DEBOUNCE_MS, DEFAULT_DEBOUNCE_MS

from platform_marks import (  # isort: skip -- tests/ is on sys.path, not a package
    HAVE_BASH_COMPLETION,
    PWSH,
    requires_pwsh,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DEVICE = """\
apiVersion: netviz.dev/v1alpha1
kind: switch
metadata:
  name: sw-1
spec:
  interfaces:
    - name: port1
      type: ethernet
      mtu: 1500
"""


# --------------------------------------------------------------------------- #
# The newline policy
# --------------------------------------------------------------------------- #


def test_write_text_never_translates_a_newline(tmp_path: Path) -> None:
    """The bytes on disk are the bytes handed in, on every platform.

    ``Path.write_text`` would emit ``\\r\\n`` here on Windows. Asserted as bytes
    rather than as text, because reading it back as text would undo exactly the
    translation being checked for.
    """
    path = tmp_path / "out.txt"
    write_text(path, "a\nb\n")
    assert path.read_bytes() == b"a\nb\n"


def test_write_text_atomically_never_translates_a_newline(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    write_text_atomically(path, "a\nb\n")
    assert path.read_bytes() == b"a\nb\n"


def test_a_crlf_document_stays_crlf_until_fmt_rewrites_it(tmp_path: Path) -> None:
    """``fmt`` has an opinion about CRLF, and rewrites it to LF exactly once.

    This is the fixed point that matters: whatever the file arrived as, one run
    of ``netviz fmt`` leaves it in a state ``--check`` accepts. Without the
    explicit ``newline=""`` in :mod:`netviz.fsio` this would fail on Windows —
    the rewrite would put the CRLF back, and ``--check`` would report the file as
    unformatted forever.
    """
    path = tmp_path / "sw.yaml"
    path.write_bytes(DEVICE.replace("\n", "\r\n").encode("utf-8"))

    assert format_paths([tmp_path], mode=Mode.CHECK).changed, "CRLF is not canonical"
    format_paths([tmp_path], mode=Mode.WRITE)
    assert b"\r\n" not in path.read_bytes()
    assert not format_paths([tmp_path], mode=Mode.CHECK).changed, "fmt is not a fixed point"


def test_the_scaffold_is_written_with_lf(tmp_path: Path) -> None:
    """``netviz init`` output is committed, so it must not vary by platform."""
    written = write_scaffold(build_scaffold(), tmp_path / "net")
    assert written
    for path in written:
        assert b"\r\n" not in path.read_bytes(), path


def test_an_imported_tree_is_written_with_lf(tmp_path: Path) -> None:
    draft = Draft()
    draft.device("sw-1").interface("port1")
    for relative, _ in build_files(draft).items():
        assert "\\" not in relative, "a draft path is POSIX, whatever wrote it"
    written = Path(tmp_path)
    from netviz.importer import write_files

    for path in write_files(build_files(draft), written):
        assert b"\r\n" not in path.read_bytes(), path


def test_the_schema_file_is_written_with_lf(tmp_path: Path) -> None:
    output = tmp_path / "netviz.schema.json"
    result = CliRunner().invoke(cli, ["schema", "-o", str(output)], catch_exceptions=False)
    assert result.exit_code == 0
    assert b"\r\n" not in output.read_bytes()


def test_gitattributes_pins_the_files_whose_bytes_are_asserted() -> None:
    """The other half of the newline policy, and the half netviz cannot enforce.

    ``core.autocrlf=true`` is the default on a Windows install of Git, and under
    it every YAML file and every golden would arrive CRLF — which would fail
    ``netviz fmt --check examples`` and every byte-for-byte golden comparison
    before a line of netviz ran. There is no fix in the code; the fix is this
    file, so the suite asserts it exists and says what it must cover.
    """
    text = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    for required in ("*.yaml text eol=lf", "tests/fixtures/** text eol=lf"):
        assert required in text, f"missing from .gitattributes: {required!r}"


# --------------------------------------------------------------------------- #
# Paths in a diff header
# --------------------------------------------------------------------------- #


def test_a_displayed_path_uses_forward_slashes(tmp_path: Path, monkeypatch: Any) -> None:
    """``display_path`` feeds a unified diff header, which is always ``/``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    assert display_path(tmp_path / "sub" / "sw.yaml") == "sub/sw.yaml"


@pytest.mark.parametrize(
    ("name", "prefixed"),
    [
        ("examples/sw.yaml", True),
        ("/home/you/net/sw.yaml", False),
        ("C:/net/sw.yaml", False),
        ("c:/net/sw.yaml", False),
    ],
)
def test_only_a_relative_path_gets_the_git_prefix(name: str, prefixed: bool) -> None:
    """An absolute path in ``+++ b/`` is a patch nothing can apply.

    Windows spells absolute a second way — a drive letter rather than a leading
    slash — and getting that wrong produces ``b/C:/net/sw.yaml``.
    """
    diff = diff_text("a\n", "b\n", name=name)
    assert (f"b/{name}" in diff) is prefixed
    assert name in diff


# --------------------------------------------------------------------------- #
# Reserved file names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stem", ["nul", "NUL", "Con", "aux", "prn", "com1", "lpt9", "nul."])
def test_a_reserved_device_name_is_recognised_on_every_platform(stem: str) -> None:
    """Not guarded by ``os.name``: a tree written here is opened there."""
    assert is_reserved_file_stem(stem)


@pytest.mark.parametrize("stem", ["sw-nul", "nullify", "com", "com10", "console", "lpt"])
def test_a_name_that_merely_resembles_one_is_left_alone(stem: str) -> None:
    assert not is_reserved_file_stem(stem)
    assert safe_file_stem(stem) == stem


def test_every_reserved_stem_is_made_nameable() -> None:
    for stem in RESERVED_FILE_STEMS:
        folded = safe_file_stem(stem)
        assert folded != stem
        assert not is_reserved_file_stem(folded)


def test_a_trailing_dot_or_space_is_trimmed() -> None:
    """Windows drops both silently, so two such names would be one file."""
    assert safe_file_stem("sw-1.") == "sw-1"
    assert safe_file_stem("sw-1 ") == "sw-1"


def test_an_imported_device_called_nul_gets_a_nameable_file() -> None:
    """``devices/nul.yaml`` is the null device on Windows, not a file.

    The element keeps the name the network reported; only the file is renamed,
    which is what makes the tree checkable-out everywhere without losing what was
    captured.
    """
    draft = Draft()
    draft.device("nul").interface("eth0")
    files = build_files(draft)
    path = next(name for name in files if name.startswith("devices/"))
    assert path == "devices/nul-file.yaml"
    assert "name: nul" in files[path], "the element name is not the file name's business"


def test_a_reserved_name_and_a_real_clash_do_not_collide() -> None:
    """``nul`` folds to ``nul-file``; a device actually called that keeps its own."""
    draft = Draft()
    for name in ("nul", "nul-file"):
        draft.device(name).interface("eth0")
    stems = sorted(name for name in build_files(draft) if name.startswith("devices/"))
    assert len(set(stems)) == 2


# --------------------------------------------------------------------------- #
# Atomic replacement
# --------------------------------------------------------------------------- #


def test_an_atomic_write_leaves_no_temporary_behind(tmp_path: Path) -> None:
    path = tmp_path / "render.svg"
    write_bytes_atomically(path, b"<svg/>")
    assert path.read_bytes() == b"<svg/>"
    assert [entry.name for entry in tmp_path.iterdir()] == ["render.svg"]


def test_a_failed_atomic_write_removes_its_temporary(tmp_path: Path, monkeypatch: Any) -> None:
    """An interrupted render must not leave a stray file in the inventory."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="No space left"):
        write_bytes_atomically(tmp_path / "render.svg", b"<svg/>")
    assert list(tmp_path.iterdir()) == []


def test_the_temporary_file_is_one_the_loader_would_skip(tmp_path: Path, monkeypatch: Any) -> None:
    """A crash between the write and the rename must not add a document.

    The temporary name starts with a dot, which is the loader's own rule for
    "do not read this" (NV-L002), so a leftover is inert rather than a phantom
    element with a syntax error.
    """
    seen: list[str] = []

    def capture(source: object, destination: object) -> None:
        seen.append(Path(str(source)).name)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "replace", capture)
    with pytest.raises(OSError, match="Input/output"):
        write_bytes_atomically(tmp_path / "sw.yaml", b"x")
    assert seen and seen[0].startswith(".") and seen[0].endswith(".tmp")


def test_a_replacement_over_an_open_file_is_retried_on_windows(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Windows refuses the rename while another handle is open; POSIX never does.

    Simulated rather than staged, so the retry is exercised on the platform this
    suite mostly runs on: a real open handle on Linux would not raise at all.
    """
    attempts = 0
    real = os.replace

    def flaky(source: object, destination: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(13, "The process cannot access the file")
        real(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr("netviz.fsio._REPLACE_BACKOFF_SECONDS", 0.0)

    source = tmp_path / "new"
    source.write_bytes(b"x")
    replace_atomically(source, tmp_path / "old")
    assert attempts == 3
    assert (tmp_path / "old").read_bytes() == b"x"


def test_a_permanently_locked_destination_is_reported_not_waited_out(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An editor holding the file open is a failure, and a prompt one."""

    def always_refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "replace", always_refuse)
    monkeypatch.setattr("netviz.fsio._REPLACE_BACKOFF_SECONDS", 0.0)
    with pytest.raises(PermissionError):
        replace_atomically(tmp_path / "new", tmp_path / "old")


def test_posix_does_not_retry_a_permission_error(tmp_path: Path, monkeypatch: Any) -> None:
    """Nothing makes it transient there, so retrying would only delay the error."""
    attempts = 0

    def refuse(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError):
        replace_atomically(tmp_path / "new", tmp_path / "old")
    assert attempts == 1


def test_no_mode_is_set_on_windows(tmp_path: Path, monkeypatch: Any) -> None:
    """``chmod`` there sets a read-only flag, which is not what 0o644 means."""
    called = False

    def record(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "chmod", record)
    write_bytes_atomically(tmp_path / "render.svg", b"<svg/>", mode=0o644)
    assert not called


# --------------------------------------------------------------------------- #
# Finding Graphviz
# --------------------------------------------------------------------------- #


def test_the_environment_variable_wins_over_path(monkeypatch: Any) -> None:
    """The escape hatch for a Graphviz that PATH cannot be taught about."""
    monkeypatch.setenv(DOT_ENV_VAR, "/opt/graphviz/bin/dot")
    assert find_dot() == "/opt/graphviz/bin/dot"


def test_a_blank_environment_variable_is_not_a_path(monkeypatch: Any) -> None:
    """An unset variable exported empty by a wrapper script must not win."""
    monkeypatch.setenv(DOT_ENV_VAR, "   ")
    monkeypatch.setattr("netviz.render.dot.shutil.which", lambda *a, **k: None)
    assert find_dot() is None


def test_path_is_consulted_before_the_well_known_directories(monkeypatch: Any) -> None:
    monkeypatch.delenv(DOT_ENV_VAR, raising=False)
    monkeypatch.setattr(
        "netviz.render.dot.shutil.which",
        lambda name, path=None: None if path else f"/usr/bin/{name}",
    )
    assert find_dot() == f"/usr/bin/{DOT_EXECUTABLE}"


def test_a_graphviz_installed_but_not_on_path_is_still_found(monkeypatch: Any) -> None:
    """The normal state of Graphviz on Windows, and a common one on macOS.

    Neither the ``choco`` package nor the MSI reliably puts ``bin`` on ``PATH``,
    and a GUI-launched process on macOS does not inherit ``/opt/homebrew/bin``.
    """
    wanted = _WELL_KNOWN_DIRS["nt"][0]

    def which(name: str, path: str | None = None) -> str | None:
        return f"{path}\\{name}.exe" if path == wanted else None

    monkeypatch.delenv(DOT_ENV_VAR, raising=False)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr("netviz.render.dot.shutil.which", which)
    assert find_dot() == f"{wanted}\\{DOT_EXECUTABLE}.exe"


def test_the_executable_name_carries_no_extension() -> None:
    """``shutil.which`` applies PATHEXT itself; ``dot.exe`` here would break POSIX."""
    assert DOT_EXECUTABLE == "dot"
    assert not DOT_EXECUTABLE.endswith(".exe")


def test_nothing_is_cached_between_lookups(monkeypatch: Any) -> None:
    """Installing Graphviz and re-rendering in a live ``watch`` must work."""
    monkeypatch.delenv(DOT_ENV_VAR, raising=False)
    answers = iter([None, "/usr/bin/dot"])
    monkeypatch.setattr(
        "netviz.render.dot.shutil.which", lambda *a, **k: next(answers, None) if not k else None
    )
    assert find_dot() is None
    assert find_dot() == "/usr/bin/dot"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("nt", "winget install Graphviz.Graphviz"), ("posix", "brew install graphviz")],
)
def test_the_install_hint_is_for_the_platform_running(
    name: str, expected: str, monkeypatch: Any
) -> None:
    """A Windows user told to run ``apt install`` has been actively misled."""
    monkeypatch.setattr(os, "name", name)
    assert expected in graphviz_install_hint()


def test_the_missing_graphviz_message_names_every_way_out(monkeypatch: Any) -> None:
    monkeypatch.setattr(os, "name", "nt")
    message = missing_dot_message(subject="svg")
    assert "winget install Graphviz.Graphviz" in message
    assert DOT_ENV_VAR in message
    assert "--format dot" in message
    assert "apt install" not in message, "the wrong platform's advice is worse than none"


def test_a_dot_that_cannot_be_executed_is_a_message_not_a_traceback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The whole point of the check: never a bare ``FileNotFoundError``."""
    monkeypatch.setenv(DOT_ENV_VAR, str(tmp_path / "nowhere" / "dot"))
    from netviz.loader import load_stream
    from netviz.render import build_graph, render

    with pytest.raises(RenderError) as caught:
        render(build_graph(load_stream(DEVICE)), "svg")
    message = str(caught.value)
    assert "could not run" in message
    assert DOT_ENV_VAR in message, "the message must name the thing the user set"


# --------------------------------------------------------------------------- #
# The local HTTP server
# --------------------------------------------------------------------------- #


def test_address_reuse_is_off_on_windows() -> None:
    """The option means opposite things, so it cannot simply be set everywhere.

    POSIX: skip TIME_WAIT, which is what makes ``watch`` restartable on a fixed
    port. Windows: bind a port another process is *listening* on, which silently
    splits incoming connections between two servers.
    """
    assert LocalServer.allow_reuse_address is (os.name != "nt")


def test_exclusive_use_is_requested_when_the_platform_has_it() -> None:
    """``SO_EXCLUSIVEADDRUSE`` is the Windows way to say what a POSIX bind says.

    Looked up with ``getattr`` in the source so a type checker run with
    ``--platform linux`` sees the same code Windows runs; asserted here so the
    lookup cannot be dropped without a failure.
    """
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    assert (exclusive is not None) is (os.name == "nt")


def test_a_second_bind_to_the_same_port_is_refused() -> None:
    """The behaviour the option choice above exists to preserve, on any platform."""
    from netviz.errors import NetvizError
    from netviz.httpserve import LocalHandler

    first = bind(LocalHandler, host="127.0.0.1", port=0)
    try:
        with pytest.raises(NetvizError, match="cannot serve"):
            bind(LocalHandler, host="127.0.0.1", port=first.server_address[1])
    finally:
        first.server_close()


# --------------------------------------------------------------------------- #
# Watch mode
# --------------------------------------------------------------------------- #


def test_the_debounce_default_is_the_one_for_this_platform() -> None:
    assert _DEBOUNCE_MS.get(sys.platform, 300) == DEFAULT_DEBOUNCE_MS


def test_every_platform_has_a_debounce_and_none_is_zero() -> None:
    """A window of 0 would re-render once per filesystem event, not once per save."""
    assert set(_DEBOUNCE_MS) == {"linux", "darwin", "win32"}
    assert all(value > 0 for value in _DEBOUNCE_MS.values())


def test_the_coalescing_backends_get_a_wider_window() -> None:
    """FSEvents and ReadDirectoryChangesW spread one save further than inotify."""
    assert _DEBOUNCE_MS["darwin"] > _DEBOUNCE_MS["linux"]
    assert _DEBOUNCE_MS["win32"] > _DEBOUNCE_MS["linux"]


def test_the_watcher_backend_is_the_cross_platform_one() -> None:
    """``watchfiles`` wraps inotify, FSEvents and ReadDirectoryChangesW.

    Asserted rather than assumed because the alternative — a polling loop, or a
    bare ``inotify`` import — would work here and nowhere else, and the failure
    would only show up on somebody else's laptop.
    """
    import watchfiles

    assert callable(watchfiles.watch)


# --------------------------------------------------------------------------- #
# PowerShell completion
# --------------------------------------------------------------------------- #


def test_powershell_is_one_of_the_shells_on_offer() -> None:
    assert "powershell" in SHELLS


def test_the_powershell_script_registers_a_native_completer() -> None:
    script = completion_script("powershell", cli)
    assert "Register-ArgumentCompleter -Native" in script
    assert f"-CommandName {PROG_NAME}, {PROG_NAME}.exe" in script
    assert "_NETVIZ_COMPLETE" in script
    assert script.endswith("\n")


def test_the_powershell_script_restores_the_variables_it_sets() -> None:
    """A completer runs in the user's session; clearing their variables is a bug."""
    script = completion_script("powershell", cli)
    assert "$saved[$name] = [Environment]::GetEnvironmentVariable($name)" in script
    assert "finally" in script


def test_the_powershell_script_holds_no_stray_percent() -> None:
    """The template is interpolated with ``%``, so a literal one must be doubled.

    ``%`` is also PowerShell's alias for ``ForEach-Object``, which is why there
    are none at all: an escaped one and an intended one would look identical.
    """
    assert "%" not in completion_script("powershell", cli)


def test_the_powershell_script_is_written_with_lf() -> None:
    """It is redirected to a file and committed, like every other artefact."""
    assert "\r" not in completion_script("powershell", cli)


def test_powershell_completion_reads_the_words_it_is_given(monkeypatch: Any) -> None:
    """Newline-separated, because no shell word can contain one.

    Splitting on whitespace would turn ``-i 'my net'`` into two arguments the
    parser then rejects, which is how a path with a space in it would stop
    completing.
    """
    monkeypatch.setenv("COMP_WORDS", "netviz\n-i\nmy net\nrender\n--lay")
    monkeypatch.setenv("COMP_CWORD", "4")
    complete = PowerShellComplete(cli, {}, PROG_NAME, "_NETVIZ_COMPLETE")
    assert complete.get_completion_args() == (["-i", "my net", "render"], "--lay")


def test_powershell_completion_survives_a_missing_cursor(monkeypatch: Any) -> None:
    """A completer that raises is a traceback under the user's cursor."""
    monkeypatch.setenv("COMP_WORDS", "netviz\nrend")
    monkeypatch.delenv("COMP_CWORD", raising=False)
    complete = PowerShellComplete(cli, {}, PROG_NAME, "_NETVIZ_COMPLETE")
    assert complete.get_completion_args() == ([], "rend")


def test_powershell_completion_survives_a_nonsense_cursor(monkeypatch: Any) -> None:
    monkeypatch.setenv("COMP_WORDS", "netviz\nrend")
    monkeypatch.setenv("COMP_CWORD", "99")
    complete = PowerShellComplete(cli, {}, PROG_NAME, "_NETVIZ_COMPLETE")
    assert complete.get_completion_args() == ([], "rend")


def test_powershell_completion_offers_the_same_candidates(monkeypatch: Any) -> None:
    """The transport is new; the completers are the ones every shell uses."""
    monkeypatch.setenv("COMP_WORDS", f"{PROG_NAME}\nrender\n--layer\n")
    monkeypatch.setenv("COMP_CWORD", "3")
    complete = PowerShellComplete(cli, {}, PROG_NAME, "_NETVIZ_COMPLETE")
    lines = complete.complete().splitlines()
    values = [line.split("\t")[1] for line in lines]
    assert "l2" in values and "rack" in values


def test_a_candidate_is_three_tab_separated_fields(monkeypatch: Any) -> None:
    """Tab, not comma: several of these summaries contain a comma."""
    monkeypatch.setenv("COMP_WORDS", f"{PROG_NAME}\nrender\n--layer\n")
    monkeypatch.setenv("COMP_CWORD", "3")
    complete = PowerShellComplete(cli, {}, PROG_NAME, "_NETVIZ_COMPLETE")
    for line in complete.complete().splitlines():
        fields = line.split("\t")
        assert len(fields) == 3, line
        assert all(field for field in fields), "PowerShell rejects an empty tooltip"


def test_a_help_text_is_flattened_onto_one_line() -> None:
    """A newline in a tooltip would be read as the end of the candidate."""
    from click.shell_completion import CompletionItem

    complete = PowerShellComplete(cli, {}, PROG_NAME, "_NETVIZ_COMPLETE")
    formatted = complete.format_completion(CompletionItem("sw-1", help="a switch\nover two lines"))
    assert formatted == "plain\tsw-1\ta switch over two lines"


#: What the two tests below ask PowerShell to do with the generated script.
#: Written to a file rather than passed with ``-Command`` so that no quoting rule
#: of the invoking shell gets a say in it.
_PARSE_CHECK = """\
$ErrorActionPreference = 'Stop'
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $args[0], [ref]$null, [ref]$errors)
if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Output $_.ToString() }
    exit 1
}
Write-Output 'parsed'
"""

_REGISTER_CHECK = """\
$ErrorActionPreference = 'Stop'
. $args[0]
Write-Output 'registered'
"""


def _powershell(tmp_path: Path, checker: str) -> subprocess.CompletedProcess[str]:
    """Run ``checker`` against a freshly generated completion script."""
    script = tmp_path / "netviz-completion.ps1"
    write_text(script, completion_script("powershell", cli))
    path = tmp_path / "check.ps1"
    write_text(path, checker)
    assert PWSH is not None
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(path), str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@requires_pwsh
def test_powershell_parses_the_script_netviz_generates(tmp_path: Path) -> None:
    """The only opinion worth having about generated PowerShell is PowerShell's.

    Python can assert what the script contains; it cannot assert that the shell
    accepts it. An unbalanced brace, a type literal that does not resolve or a
    stray ``%`` left by the template interpolation would all pass every other test
    in this file and then do nothing at all in a user's session.
    """
    result = _powershell(tmp_path, _PARSE_CHECK)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "parsed" in result.stdout


@requires_pwsh
def test_powershell_accepts_the_completer_registration(tmp_path: Path) -> None:
    """Parsing is not running: ``Register-ArgumentCompleter`` must also accept it.

    This is what catches a malformed ``-CommandName`` list or a script block
    PowerShell parses but refuses.
    """
    result = _powershell(tmp_path, _REGISTER_CHECK)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "registered" in result.stdout


#: Register the completer, then ask PowerShell to complete a line through it —
#: which is what the shell itself does on Tab. ``CompleteInput`` is the public
#: entry point for that, so this exercises the whole round trip: the script block,
#: the environment variables it sets, the ``netviz`` re-invocation, and the
#: tab-separated candidates coming back.
_COMPLETE_CHECK = """\
$ErrorActionPreference = 'Stop'
. $args[0]
$line = $args[1]
$result = [System.Management.Automation.CommandCompletion]::CompleteInput(
    $line, $line.Length, $null)
foreach ($match in $result.CompletionMatches) {
    Write-Output ("{0}`t{1}" -f $match.CompletionText, $match.ToolTip)
}
"""


def _complete(tmp_path: Path, line: str) -> dict[str, str]:
    """What PowerShell offers for ``line``, as ``candidate -> tooltip``.

    The completer re-invokes ``netviz`` by name, so the console script has to be
    findable from inside PowerShell. It is put on ``PATH`` explicitly rather than
    assumed: ``pytest`` run as ``.venv/bin/pytest`` does not add the venv to
    ``PATH``, and a test that quietly returned no candidates because of that would
    assert nothing at all.

    Which directory that is comes from ``sysconfig`` rather than from
    ``sys.executable``'s parent: the two agree on POSIX and do not on Windows,
    where console scripts are installed into ``Scripts\\`` beside ``python.exe``.
    """
    scripts = Path(sysconfig.get_path("scripts"))
    assert (scripts / "netviz").exists() or (scripts / "netviz.exe").exists(), (
        f"the netviz console script is not in {scripts}; install with 'pip install -e .'"
    )

    script = tmp_path / "netviz-completion.ps1"
    write_text(script, completion_script("powershell", cli))
    checker = tmp_path / "complete.ps1"
    write_text(checker, _COMPLETE_CHECK)
    assert PWSH is not None
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(checker), str(script), line],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={**os.environ, "PATH": f"{scripts}{os.pathsep}{os.environ.get('PATH', '')}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(  # type: ignore[misc] - split(maxsplit=1) on a row holding a tab is a pair
        row.split("\t", 1) for row in result.stdout.splitlines() if "\t" in row
    )


@requires_pwsh
def test_powershell_completes_a_static_value_space_with_its_descriptions(tmp_path: Path) -> None:
    """The whole round trip, driven by PowerShell's own completion machinery.

    Everything above asserts one link of the chain. This asserts the chain: the
    registered script block, the environment it sets, the ``netviz``
    re-invocation, the tab-separated candidates, and the tooltip PowerShell shows
    beside each one — which is the half a ``click.Choice`` alone would not give.
    """
    offered = _complete(tmp_path, "netviz render --layer ")
    assert {"l1", "l2", "l3", "rack"} <= set(offered)
    assert offered["l2"] == "the same topology annotated with VLANs"


@requires_pwsh
def test_powershell_completes_an_element_name_from_the_inventory(tmp_path: Path) -> None:
    """The half no other shell integration could guess, through a path with a space.

    ``-i 'my net'`` is why the words travel newline-separated: split on whitespace
    the quoted path would arrive as two arguments, ``-i`` would name a directory
    that does not exist, and this would silently offer nothing.
    """
    inventory = tmp_path / "my net"
    shutil.copytree(REPO_ROOT / "examples" / "home-lab", inventory)
    offered = _complete(tmp_path, f"netviz -i '{inventory}' show pc-")
    assert offered == {"pc-desk": "computer"}


def test_an_unknown_shell_lists_the_ones_that_exist() -> None:
    result = CliRunner().invoke(cli, ["completion", "tcsh"])
    assert result.exit_code == 2
    for shell in SHELLS:
        assert shell in result.output


def test_the_bash_capability_matches_what_click_actually_does() -> None:
    """:data:`HAVE_BASH_COMPLETION` predicts whether Click adds a line, or it is useless.

    ``tests/test_docs.py`` skips the documented ``netviz completion bash``
    transcript on the strength of that prediction, and a prediction nothing
    checks is a silent skip: wrong in one direction it hides a real difference in
    what netviz prints, wrong in the other it fails a job for a line the
    documentation is right not to show.

    Both halves are worth having on every runner. macOS is where the answer is
    ``False`` — ``/bin/bash`` 3.2 — and Windows is where getting there is
    delicate, because ``bash`` resolved through ``PATH`` and ``bash`` handed to
    ``CreateProcess`` are two different programs.
    """
    result = CliRunner().invoke(cli, ["completion", "bash"])
    assert result.exit_code == 0
    assert "_netviz_completion_setup;" in result.output, "the script itself is always emitted"

    warned = "shell completion is not supported" in result.output.lower()
    assert warned is not HAVE_BASH_COMPLETION, (
        f"HAVE_BASH_COMPLETION={HAVE_BASH_COMPLETION} but Click "
        f"{'warned' if warned else 'did not warn'}: {result.output.splitlines()[-1]!r}"
    )


# --------------------------------------------------------------------------- #
# The suite's own portability
# --------------------------------------------------------------------------- #


def test_the_generated_route_script_is_lf_whatever_wrote_it(tmp_path: Path) -> None:
    """It runs under ``sh``, and ``\\r`` after a shebang is "bad interpreter"."""
    script = tmp_path / "routes.sh"
    result = CliRunner().invoke(
        cli,
        ["-i", str(REPO_ROOT / "examples" / "campus"), "export", "routes", "-o", str(script)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert b"\r\n" not in script.read_bytes()


def test_no_test_module_skips_a_whole_platform() -> None:
    """A blanket ``skip_on_windows`` would be a platform nothing checks.

    Every skip has to name a capability the platform lacks, which is what
    ``tests/platform_marks.py`` is for. A bare ``sys.platform``/``os.name``
    comparison inside a ``skipif`` is how that rule gets broken quietly, so it is
    checked rather than documented.

    This module is excluded because it is the checker: the line below holds the
    pattern it looks for, and would otherwise report itself.
    """
    here = Path(__file__).resolve()
    offenders = []
    for path in sorted(here.parent.glob("test_*.py")):
        if path.resolve() == here:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if "skipif" in line and ("sys.platform" in line or "os.name" in line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        "skip on a named capability from tests/platform_marks.py, not on a platform: "
        + ", ".join(offenders)
    )


@pytest.mark.parametrize("shell", SHELLS)
def test_every_shell_still_generates(shell: str) -> None:
    """Adding PowerShell must not have disturbed the three Click generates."""
    script = completion_script(shell, cli)
    assert script.strip()
    assert script.endswith("\n")
    assert "_NETVIZ_COMPLETE" in script


def test_the_subprocess_call_never_goes_through_a_shell() -> None:
    """``shell=True`` would give cmd.exe a say in what an inventory name means."""
    source = (REPO_ROOT / "src" / "netviz" / "render" / "dot.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "subprocess.run(" in source


def test_nothing_in_the_package_writes_text_without_a_newline_policy() -> None:
    """``Path.write_text`` is the one call that silently varies by platform.

    Every text artefact netviz writes goes through :mod:`netviz.fsio`, which
    passes ``newline=""``. This is the guard that keeps a new call site from
    reintroducing the CRLF bug in a file somebody then commits.
    """
    package = REPO_ROOT / "src" / "netviz"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if ".write_text(" in line and "fsio" not in line:
                offenders.append(f"{path.relative_to(package)}:{number}")
    assert not offenders, (
        "use netviz.fsio.write_text, which does not translate line endings: " + ", ".join(offenders)
    )


def test_the_subprocess_timeout_is_honoured_the_same_way(monkeypatch: Any) -> None:
    """``subprocess.run(timeout=)`` is one of the few genuinely portable knobs."""

    def timed_out(*_args: object, **kwargs: object) -> None:
        assert kwargs["timeout"] > 0
        raise subprocess.TimeoutExpired(cmd="dot", timeout=float(kwargs["timeout"]))

    from netviz.loader import load_stream
    from netviz.render import build_graph, render

    monkeypatch.setattr("netviz.render.dot.find_dot", lambda: "dot")
    monkeypatch.setattr("netviz.render.dot.subprocess.run", timed_out)
    with pytest.raises(RenderError, match="did not finish"):
        render(build_graph(load_stream(DEVICE)), "svg")
