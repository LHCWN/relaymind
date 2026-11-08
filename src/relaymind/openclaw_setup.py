"""The `relaymind openclaw` install helper (RM33, RM12; spec sections 7.5 and 14.1).

It runs the OpenClaw host's own public commands, one after the other, so an
operator does not type them by hand. It never starts the gateway, never edits
a host file, and never puts the application token in an argument or in its own
output. The host owns the plugin, its configuration, and its credentials.
"""

import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from relaymind.evaluation.plugin_base_url import check_base_url

PLUGIN_ID = "relaymind"
# The one OpenClaw release the plugin is built and qualified against, and the range it
# declares to the host (spec 0.70, section 7.5). DECLARED_RANGE is the text in
# integrations/openclaw/package.json; tests/test_openclaw_setup.py fails when they differ.
# The upper bound ends in -0 so a 2027 pre-release is outside it, as the host reads it.
QUALIFIED_RELEASE: tuple[int, int, int] = (2026, 10, 7)
QUALIFIED_RELEASE_TEXT = ".".join(str(part) for part in QUALIFIED_RELEASE)
DECLARED_RANGE = f">={QUALIFIED_RELEASE_TEXT} <2027.0.0-0"
DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
SUMS_NAME = "SHA256SUMS"
RECORD_NAME = "release.json"

Release = tuple[int, int, int]
Sink = Callable[[str], None]

# A whole version token: a fourth part, a build suffix or an unknown suffix leaves no match,
# so "2026.10.7.1" or "2026.10.7+local" is never read as the qualified release. The suffixes
# are the ones the host itself reads (a numeric correction, a pre-release).
_RELEASE = re.compile(
    r"(?<![\d.])(\d{4})\.(\d{1,2})\.(\d{1,2})(-\d+|-(?:alpha|beta|rc)\.\d+)?(?![\w+-]|\.\w)"
)
_BOUND = re.compile(r"(>=|<)?(\d{4})\.(\d{1,2})\.(\d{1,2})(-0)?")
_NUMERIC_CORRECTION = re.compile(r"-\d+")
_INSTALL_TIMEOUT = 300.0
_STEP_TIMEOUT = 60.0

# cmd.exe expands % (and ! with delayed expansion) and ends a command at a line
# break even inside quotes; a quote inside an argument would end its quoting.
BATCH_SUFFIXES = (".cmd", ".bat")
BATCH_UNSAFE = frozenset('"%!\r\n')


def _dotted(release: Release) -> str:
    return ".".join(str(part) for part in release)


def _stderr_line(line: str) -> None:
    print(line, file=sys.stderr)


def parse_release(text: str) -> tuple[Release, str] | None:
    """The OpenClaw release in the host's version output, with its suffix; None if unreadable."""
    found = _RELEASE.search(text)
    if found is None:
        return None
    return (int(found[1]), int(found[2]), int(found[3])), found[4] or ""


def is_qualified(release: Release, suffix: str) -> bool:
    """True only for the qualified release itself; a numeric correction is the same release."""
    return release == QUALIFIED_RELEASE and (
        not suffix or bool(_NUMERIC_CORRECTION.fullmatch(suffix))
    )


def release_in_range(release: Release, declared: str, suffix: str = "") -> bool | None:
    """Whether a host release satisfies a declared range; None when the range cannot be read.

    The range is the ``pluginApi`` text of the release being installed: space-separated
    ``>=X.Y.Z`` and ``<X.Y.Z[-0]`` bounds, or one exact ``X.Y.Z`` (releases before spec 0.70).
    Pre-releases follow SemVer: ``X.Y.Z-beta.1`` ranks below ``X.Y.Z``, so it does not satisfy
    ``>=X.Y.Z``. A numeric correction (``X.Y.Z-1``) is the release itself. A bound that ends in
    ``-0`` excludes every pre-release of its own version. The host is looser (it drops a
    pre-release suffix before it compares), so the helper may refuse a release the host would
    load, never the other way round.
    """
    tokens = declared.split()
    if not tokens:
        return None
    prerelease = bool(suffix) and not _NUMERIC_CORRECTION.fullmatch(suffix)
    for token in tokens:
        found = _BOUND.fullmatch(token)
        if found is None:
            return None
        operator, bound = found[1], (int(found[2]), int(found[3]), int(found[4]))
        if operator == ">=":
            satisfied = release > bound or (release == bound and not prerelease)
        elif operator == "<":
            satisfied = release < bound or (release == bound and prerelease and not found[5])
        else:
            satisfied = release == bound and not prerelease
        if not satisfied:
            return False
    return True


def _host_command(host_command: Sequence[str] | None) -> list[str] | None:
    if host_command is not None:
        return list(host_command) or None
    found = shutil.which("openclaw")
    return [found] if found else None


def _require_host(host_command: Sequence[str] | None, err: Sink) -> list[str] | None:
    """The host command, or None after saying where to get it."""
    command = _host_command(host_command)
    if command is None:
        err(
            "relaymind: openclaw was not found on PATH; install OpenClaw first, then run this again"
        )
    return command


def _batch_quote(arg: str) -> str:
    # Inside quotes cmd.exe takes & | < > ^ ( ) literally. A backslash run before the
    # closing quote is doubled so the program's own argument parsing does not read it
    # as an escaped quote.
    trailing = len(arg) - len(arg.rstrip("\\"))
    return '"' + arg + "\\" * trailing + '"'


def host_invocation(
    command: Sequence[str],
    args: Sequence[str],
    platform: str | None = None,
    comspec: str | None = None,
) -> list[str] | str:
    """What ``subprocess.run`` gets for one host call.

    A program runs as an argument list with no shell. A Windows batch file
    (``openclaw.cmd``, the npm shim on PATH) only runs through cmd.exe, which
    Windows would start with the arguments as they are, under cmd's own
    parsing. So cmd.exe is started here, ``/d /v:off /s /c``, with every
    argument quoted, and an argument cmd.exe would rewrite is refused.
    ``scripts/openclaw_install_check.py`` uses this same function.
    """
    argv = [*command, *args]
    platform = sys.platform if platform is None else platform
    if not command or platform != "win32" or not str(command[0]).lower().endswith(BATCH_SUFFIXES):
        return argv
    for arg in argv:
        if BATCH_UNSAFE & set(arg):
            raise OSError(
                f"cannot pass {arg!r} to a batch file through cmd.exe;"
                " run the host with node and its entry script instead"
            )
    comspec = comspec or os.environ.get("COMSPEC", "cmd.exe")
    line = " ".join(_batch_quote(arg) for arg in argv)
    return f'"{comspec}" /d /v:off /s /c "{line}"'


def _run(
    command: Sequence[str], args: Sequence[str], stdin: str | None, timeout: float
) -> tuple[int, str, str]:
    """Run one host call. (-1, "", reason) when it could not run or timed out.

    A step with no input gets no standard input at all, so a host that asks a question
    fails at once instead of waiting on the operator's terminal behind captured output.
    """
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        completed = subprocess.run(
            host_invocation(command, args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **feed,
        )
    except subprocess.TimeoutExpired:
        return -1, "", "the host did not finish in time"
    except OSError as error:
        return -1, "", f"the host could not be started: {error.strerror or error}"
    return completed.returncode, completed.stdout, completed.stderr


def _verified_release(release: Path) -> tuple[Path, str] | str:
    """The checked tarball and the range its release declares, or the reason it is refused."""
    for name in (RECORD_NAME, SUMS_NAME):
        if not (release / name).is_file():
            return f"{release / name} is missing: use the directory `npm run pack:release` wrote"
    try:
        record = json.loads((release / RECORD_NAME).read_text(encoding="utf-8"))
        filename = record["filename"]
        declared = record["openclaw"]["pluginApi"]
        recorded = record["sha256"]
        listed = [
            line.split(None, 1)
            for line in (release / SUMS_NAME).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return f"{RECORD_NAME} or {SUMS_NAME} cannot be read"
    if not isinstance(filename, str) or filename != Path(filename).name:
        return f"{RECORD_NAME} does not name a plain file"
    if not isinstance(declared, str):
        return f"{RECORD_NAME} does not say which OpenClaw releases the plugin declares"
    tarball = release / filename
    if not tarball.is_file():
        return f"{tarball} is missing"
    expected = [parts[0] for parts in listed if len(parts) == 2 and parts[1].strip() == filename]
    if len(expected) != 1:
        return f"{SUMS_NAME} does not list {filename} exactly once"
    with tarball.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != expected[0].lower():
        return f"{filename} does not match {SUMS_NAME}; do not install it"
    if recorded != digest:
        return f"{filename} does not match the sha256 in {RECORD_NAME}; do not install it"
    return tarball, declared


def _inspect_problem(code: int, out: str, err_text: str) -> str | None:
    """Why `plugins inspect` does not show a loaded, enabled plugin; None when it does."""
    if code != 0:
        return err_text.strip() or "the host gave no message"
    try:
        report = json.loads(out.strip())
        plugin = report["plugin"]
        status, enabled, providers = plugin["status"], plugin["enabled"], plugin["providerIds"]
        diagnostics = report["diagnostics"]
        if not isinstance(providers, list):
            raise TypeError("providerIds is not a list")
    except (ValueError, KeyError, TypeError):
        return "the plugin is not loaded: the host's inspect output could not be read"
    shown = f": {json.dumps(diagnostics, default=str)[:300]}" if diagnostics else ""
    if status != "loaded":
        return f"the plugin is not loaded (status {status!r}){shown}"
    if enabled is not True:
        return f"the plugin is installed but not enabled{shown}"
    if PLUGIN_ID not in providers:
        return f"the plugin loaded but registered no {PLUGIN_ID} provider{shown}"
    if diagnostics:
        return f"the host reports problems with the plugin{shown}"
    return None


def install_plugin(
    release: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    host_command: Sequence[str] | None = None,
    stdout: Sink = print,
    stderr: Sink | None = None,
) -> int:
    """Install, configure and enable the plugin.

    Returns 0 on success, 2 when refused before any change, 1 when a host step fails.
    """
    err = stderr or _stderr_line
    # The plugin's own rules (loopback, /v1, no credentials); the normal form is what it stores.
    try:
        base_url = check_base_url(base_url)
    except ValueError:
        err(
            "relaymind: the base URL must be the gateway's loopback /v1 address, "
            f"such as {DEFAULT_BASE_URL} (no user name, query or fragment)"
        )
        return 2
    command = _require_host(host_command, err)
    if command is None:
        return 2
    checked = _verified_release(release)
    if isinstance(checked, str):
        err(f"relaymind: {checked}; nothing was changed")
        return 2
    tarball, declared = checked
    code, version_out, version_err = _run(command, ["--version"], None, _STEP_TIMEOUT)
    if code != 0:
        err(f"relaymind: `openclaw --version` failed: {version_err.strip() or 'no output'}")
        return 2
    # Some hosts print the version on standard error; read both, as the trial kit does.
    host = parse_release(version_out + version_err)
    if host is None:
        err("relaymind: cannot read the OpenClaw release from `openclaw --version`")
        return 2
    host_release, suffix = host
    shown = _dotted(host_release) + suffix
    in_range = release_in_range(host_release, declared, suffix)
    if in_range is None:
        err(f"relaymind: cannot read the declared OpenClaw range {declared!r}; nothing was changed")
        return 2
    if not in_range:
        err(
            f"relaymind: OpenClaw {shown} is outside the range this plugin declares "
            f"({declared}); nothing was changed"
        )
        return 2
    if not is_qualified(host_release, suffix):
        stdout(
            f"Note: OpenClaw {shown} is unqualified. RelayMind is tested on "
            f"{QUALIFIED_RELEASE_TEXT} only; the plugin will load, but this release "
            "has not been checked."
        )
    steps = [
        (
            "install the plugin",
            ["plugins", "install", f"npm-pack:{tarball}", "--force", "--accept-capabilities"],
            _INSTALL_TIMEOUT,
        ),
        (
            f"set the gateway address to {base_url}",
            ["config", "set", f"plugins.entries.{PLUGIN_ID}.config.baseUrl", base_url],
            _STEP_TIMEOUT,
        ),
        ("enable the plugin", ["plugins", "enable", PLUGIN_ID], _STEP_TIMEOUT),
    ]
    for label, args, timeout in steps:
        stdout(f"- {label}")
        code, _out, step_err = _run(command, args, None, timeout)
        if code != 0:
            err(f"relaymind: could not {label}: {step_err.strip() or 'the host gave no message'}")
            err(
                "relaymind: the host may be partly changed; check it with "
                f"`openclaw plugins inspect {PLUGIN_ID}`, then run this again"
            )
            return 1
    stdout("- check that the plugin loaded")
    code, inspect_out, inspect_err = _run(
        command, ["plugins", "inspect", PLUGIN_ID, "--runtime", "--json"], None, _STEP_TIMEOUT
    )
    problem = _inspect_problem(code, inspect_out, inspect_err)
    if problem:
        err(f"relaymind: {problem}")
        return 1
    stdout("The RelayMind plugin is installed and loaded. Two steps are left:")
    stdout("  1. Start the gateway:  relaymind serve --config relaymind.toml")
    stdout("     It prints an application token. The token changes at every start.")
    stdout(
        "  2. Store the token:    relaymind openclaw token   (paste the token, then press Enter)"
    )
    stdout("Then pick the model relaymind/relaymind-client-tools in OpenClaw.")
    stdout(
        f"The gateway address is set to {base_url}. Running this command again sets it "
        "back to the default unless you pass --base-url."
    )
    return 0


def read_token() -> str:
    """The token from standard input. At a terminal it is read without echo and ends at Enter."""
    if sys.stdin.isatty():
        try:
            return getpass.getpass("Application token (input is hidden): ")
        except EOFError:  # Ctrl-D: no token; store_token then refuses cleanly
            return ""
    return sys.stdin.read()


def store_token(
    token: str,
    *,
    host_command: Sequence[str] | None = None,
    stdout: Sink = print,
    stderr: Sink | None = None,
) -> int:
    """Hand the application token to the host on standard input; never print or relay it."""
    err = stderr or _stderr_line
    words = token.split()
    if len(words) != 1:
        err("relaymind: expected the application token on standard input: one word, nothing else")
        return 2
    command = _require_host(host_command, err)
    if command is None:
        return 2
    code, _out, _err_text = _run(
        command,
        ["models", "auth", "paste-api-key", "--provider", PLUGIN_ID],
        words[0] + "\n",
        _STEP_TIMEOUT,
    )
    if code != 0:
        # The host's message is not shown: it may repeat what it was given.
        err("relaymind: the host did not accept the token; run `openclaw models auth --help`")
        return 1
    code, _out, _err_text = _run(
        command,
        ["models", "list", "--refresh", "--provider", PLUGIN_ID, "--json"],
        None,
        _STEP_TIMEOUT,
    )
    if code != 0:
        err("relaymind: the token is stored, but the model list could not be refreshed")
        return 1
    stdout("Token stored. Pick the model relaymind/relaymind-client-tools in OpenClaw.")
    stdout("After every gateway restart, run `relaymind openclaw token` again with the new token.")
    return 0
