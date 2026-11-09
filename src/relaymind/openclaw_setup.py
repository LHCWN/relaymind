"""The `relaymind openclaw` setup helper (RM33, RM12, RM45, RM46; spec sections 7.5, 7.7, 14.1).

It runs the OpenClaw host's own public commands, one after the other, so an
operator does not type them by hand. It never starts the gateway, never edits
a host file, and never puts the application token in an argument or in its own
output. The host owns the plugin, its configuration, and its credentials. The
one file it writes is the guidance plugin's own tool list.
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
from urllib.parse import urljoin

from relaymind.evaluation.checkpoint import atomic_json
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
GUIDANCE_PLUGIN_ID = "relaymind-guidance"
# Present in a source checkout; a package install passes --plugin.
GUIDANCE_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "integrations/openclaw/guidance-plugin"
GUIDANCE_LIMITS = ("hint", "strict")
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
_STOP_TIMEOUT = 5.0

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
    try:
        process = subprocess.Popen(
            host_invocation(command, args),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return -1, "", f"the host could not be started: {error.strerror or error}"
    try:
        out, err_text = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(process)
        return -1, "", "the host did not finish in time"
    return process.returncode, out, err_text


def _stop(process: subprocess.Popen) -> None:
    """End a host call that ran out of time.

    On Windows killing cmd.exe leaves its node child holding the pipes, so a later read would never
    end. The whole process tree is ended there, and the final read has its own limit.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=_STOP_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # The direct kill below still runs.
    try:
        process.kill()
    except OSError:
        pass  # It may be gone already; the final read below still runs.
    try:
        process.communicate(timeout=_STOP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        pass


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


def _inspect_problem(code: int, out: str, err_text: str, provider: str | None) -> str | None:
    """Why `plugins inspect` does not show a loaded, enabled plugin; None when it does.

    ``provider`` names the provider the plugin must register; None for a plugin with no provider.
    """
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
    if provider is not None and provider not in providers:
        return f"the plugin loaded but registered no {provider} provider{shown}"
    if diagnostics:
        return f"the host reports problems with the plugin{shown}"
    return None


def _release_in_declared_range(command: Sequence[str], declared: str) -> tuple[Release, str] | str:
    """The host's release when the plugin declares it, or why it is refused."""
    code, version_out, version_err = _run(command, ["--version"], None, _STEP_TIMEOUT)
    if code != 0:
        return f"`openclaw --version` failed: {version_err.strip() or 'no output'}"
    # Some hosts print the version on standard error; read both, as the trial kit does.
    host = parse_release(version_out + version_err)
    if host is None:
        return "cannot read the OpenClaw release from `openclaw --version`"
    release, suffix = host
    in_range = release_in_range(release, declared, suffix)
    if in_range is None:
        return f"cannot read the declared OpenClaw range {declared!r}; nothing was changed"
    if not in_range:
        return (
            f"OpenClaw {_dotted(release)}{suffix} is outside the range this plugin declares "
            f"({declared}); nothing was changed"
        )
    return host


def _note_if_unqualified(host: tuple[Release, str], stdout: Sink) -> None:
    """Say so when a host release inside the declared range is not the qualified one."""
    release, suffix = host
    if not is_qualified(release, suffix):
        stdout(
            f"Note: OpenClaw {_dotted(release)}{suffix} is unqualified. RelayMind is tested on "
            f"{QUALIFIED_RELEASE_TEXT} only; the plugin will load, but this release "
            "has not been checked."
        )


def _run_steps(
    command: Sequence[str],
    steps: Sequence[tuple[str, list[str], float]],
    plugin_id: str,
    stdout: Sink,
    err: Sink,
) -> int:
    """Run host steps in order: 0, or 1 at the first failure."""
    for label, args, timeout in steps:
        stdout(f"- {label}")
        code, _out, step_err = _run(command, args, None, timeout)
        if code != 0:
            err(f"relaymind: could not {label}: {step_err.strip() or 'the host gave no message'}")
            err(
                "relaymind: the host may be partly changed; check it with "
                f"`openclaw plugins inspect {plugin_id}`, then run this again"
            )
            return 1
    return 0


def _apply_steps(
    command: Sequence[str],
    steps: Sequence[tuple[str, list[str], float]],
    plugin_id: str,
    provider: str | None,
    stdout: Sink,
    err: Sink,
) -> int:
    """Run host steps in order and check that the plugin loaded: 0, or 1 at the first failure."""
    if _run_steps(command, steps, plugin_id, stdout, err):
        return 1
    stdout("- check that the plugin loaded")
    code, inspect_out, inspect_err = _run(
        command, ["plugins", "inspect", plugin_id, "--runtime", "--json"], None, _STEP_TIMEOUT
    )
    problem = _inspect_problem(code, inspect_out, inspect_err, provider)
    if problem:
        err(f"relaymind: {problem}")
        return 1
    return 0


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
    host = _release_in_declared_range(command, declared)
    if isinstance(host, str):
        err(f"relaymind: {host}")
        return 2
    _note_if_unqualified(host, stdout)
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
    if _apply_steps(command, steps, PLUGIN_ID, PLUGIN_ID, stdout, err):
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


def _probed_tools(code: int, out: str, err_text: str) -> list[str] | str:
    """The tool names `mcp probe --json` printed, or why they cannot be used.

    A server that did not answer means a short list, and a strict limit would not hold for the
    tools left out, so a partial answer is refused and not written down.
    """
    try:
        report = json.loads(out.strip())
        names, diagnostics = report["tools"], report["diagnostics"]
    except (ValueError, KeyError, TypeError):
        return err_text.strip()[:300] or "the host's MCP list could not be read"
    if (
        not isinstance(names, list)
        or not isinstance(diagnostics, list)
        or not all(isinstance(name, str) for name in names)
    ):
        return "the host's MCP list could not be read"
    if diagnostics:
        servers = sorted(
            {
                str(item["serverName"])
                for item in diagnostics
                if isinstance(item, dict) and item.get("serverName")
            }
        )
        return f"these MCP servers did not answer: {', '.join(servers) or 'unknown'}"
    if code != 0:  # The probe command itself failed, so no server is to blame.
        return err_text.strip()[:300] or "the host's MCP list could not be read"
    return sorted(set(names))


def _known_tools(path: Path) -> dict[str, dict] | str:
    """The entries already in the tools file, by name, or why the file cannot be read."""
    try:
        tools = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(tools, list):
            raise TypeError("not a list")
        known = {tool["name"]: tool for tool in tools}
        if not all(
            isinstance(name, str) and isinstance(tool.get("description", ""), str)
            for name, tool in known.items()
        ):
            raise TypeError("a name or description is not text")
        return known
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return f"{path} is not a readable list of {{name, description}}; fix or remove it"


def _refresh_tools_file(
    command: Sequence[str], path: Path, stdout: Sink
) -> list[dict[str, object]] | str:
    """List the host's MCP tools and write them to the file, or say why nothing was written.

    A listed tool is marked ``generated``, so a later run can drop it once the host no longer lists
    it. An entry without the mark was added by hand and stays.
    """
    known = _known_tools(path)
    if isinstance(known, str):
        return known
    stdout("- list the tools of the host's MCP servers")
    names = _probed_tools(*_run(command, ["mcp", "probe", "--json"], None, _INSTALL_TIMEOUT))
    if isinstance(names, str):
        return names
    listed = [
        {"name": name, "description": known.get(name, {}).get("description", ""), "generated": True}
        for name in names
    ]
    by_hand = [
        {"name": name, "description": tool.get("description", "")}
        for name, tool in known.items()
        if name not in names and not tool.get("generated")
    ]
    tools = sorted(listed + by_hand, key=lambda tool: tool["name"])
    try:
        atomic_json(path, tools)  # In one step, so the plugin never reads half of it.
    except OSError as error:
        return f"cannot write {path}: {error.strerror or error}"
    return tools


def _guidance_plugin_range(plugin: Path) -> tuple[str] | str:
    """The OpenClaw range this guidance plugin declares, or why the folder is refused."""
    try:
        manifest = json.loads((plugin / "openclaw.plugin.json").read_text(encoding="utf-8"))
        package = json.loads((plugin / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"{plugin} is not a plugin folder; pass the guidance plugin with --plugin"
    if not isinstance(manifest, dict) or manifest.get("id") != GUIDANCE_PLUGIN_ID:
        return f"{plugin} is not the {GUIDANCE_PLUGIN_ID} plugin"
    try:
        declared = package["openclaw"]["compat"]["pluginApi"]
    except (KeyError, TypeError):
        declared = None
    if not isinstance(declared, str):
        return f"{plugin} does not say which OpenClaw releases it declares"
    return (declared,)


def _guidance_base_url(url: str) -> str | None:
    """The normal form of an address the guidance plugin accepts, or None."""
    try:
        checked = check_base_url(url)
    except ValueError:
        return None
    # The provider plugin's rules also allow https; the guidance plugin takes plain http only.
    return checked if checked.startswith("http://") else None


def _configured_base_url(command: Sequence[str], key: str) -> str:
    """The address an earlier run set, so running again does not move it; else the default."""
    code, out, _err_text = _run(command, ["config", "get", key], None, _STEP_TIMEOUT)
    if code != 0:
        return DEFAULT_BASE_URL
    try:
        # The plugin uses only the origin, so a path set by hand must not lose the port.
        stored = urljoin(out.strip(), "/v1")
    except ValueError:
        return DEFAULT_BASE_URL
    return _guidance_base_url(stored) or DEFAULT_BASE_URL


def setup_guidance(
    *,
    plugin: Path | str = GUIDANCE_PLUGIN_DIR,
    base_url: str | None = None,
    tools_file: Path | str | None = None,
    limit: str | None = None,
    host_command: Sequence[str] | None = None,
    stdout: Sink = print,
    stderr: Sink | None = None,
) -> int:
    """Install the guidance plugin, write its tool list, and empty the host's listed skills.

    An option left out keeps what an earlier run set. Returns 0 on success, 2 when refused before
    any change, 1 when a host step fails.
    """
    err = stderr or _stderr_line
    if base_url is not None:
        base_url = _guidance_base_url(base_url)
        if base_url is None:
            err(
                "relaymind: the base URL must be the gateway's plain http loopback /v1 address, "
                f"such as {DEFAULT_BASE_URL}; nothing was changed"
            )
            return 2
    try:  # Absolute, because the host keeps the link and the plugin refuses a relative tools path.
        plugin = Path(plugin).expanduser().resolve()
        tools_file = Path(tools_file).expanduser().resolve() if tools_file is not None else None
    except RuntimeError:  # A ~ with no known home folder.
        err("relaymind: a path starts with ~ but that home folder is unknown; nothing was changed")
        return 2
    declared = _guidance_plugin_range(plugin)
    if isinstance(declared, str):
        err(f"relaymind: {declared}; nothing was changed")
        return 2
    command = _require_host(host_command, err)
    if command is None:
        return 2
    try:  # A path a batch shim cannot carry would otherwise stop a later step, mid-change.
        host_invocation(command, [str(path) for path in (plugin, tools_file) if path is not None])
    except OSError as error:
        err(f"relaymind: {error}; nothing was changed")
        return 2
    host = _release_in_declared_range(command, declared[0])
    if isinstance(host, str):
        err(f"relaymind: {host}")
        return 2
    _note_if_unqualified(host, stdout)
    entry = f"plugins.entries.{GUIDANCE_PLUGIN_ID}"
    if base_url is None:
        base_url = _configured_base_url(command, f"{entry}.config.baseUrl")
    config = {"baseUrl": base_url}
    if limit is not None:
        config["limit"] = limit
    tools = None
    if tools_file is not None:
        # Before any host change: a refused list leaves the host as it was.
        tools = _refresh_tools_file(command, tools_file, stdout)
        if isinstance(tools, str):
            err(f"relaymind: {tools}; nothing was changed")
            return 2
        config["toolsFile"] = str(tools_file)
    steps = [
        (
            "install the guidance plugin",
            ["plugins", "install", "--link", str(plugin), "--force", "--accept-capabilities"],
            _INSTALL_TIMEOUT,
        ),
        (
            "allow its prompt hook",
            ["config", "set", f"{entry}.hooks.allowConversationAccess", "true", "--strict-json"],
            _STEP_TIMEOUT,
        ),
        *[
            (f"set {name}", ["config", "set", f"{entry}.config.{name}", value], _STEP_TIMEOUT)
            for name, value in config.items()
        ],
        ("enable the plugin", ["plugins", "enable", GUIDANCE_PLUGIN_ID], _STEP_TIMEOUT),
    ]
    # Host-wide, so only after the plugin that replaces the list has loaded.
    empty_the_list = (
        "stop listing skills in the prompt",
        ["config", "set", "skills.limits.maxSkillsInPrompt", "0", "--strict-json"],
        _STEP_TIMEOUT,
    )
    if _apply_steps(command, steps, GUIDANCE_PLUGIN_ID, None, stdout, err) or _run_steps(
        command, [empty_the_list], GUIDANCE_PLUGIN_ID, stdout, err
    ):
        return 1
    stdout("The guidance plugin is installed and loaded. Two steps are left:")
    stdout(
        "  1. Add a [guidance] table with enabled = true to the gateway's launch file and start it."
    )
    stdout(
        "  2. Put the printed application token in RELAYMIND_APP_TOKEN for the OpenClaw process."
    )
    stdout("Skills are no longer listed in the prompt for every agent of this OpenClaw.")
    if tools is not None:
        count = f"{len(tools)} tool" + ("" if len(tools) == 1 else "s")
        stdout(f"Wrote {count} to {tools_file}. Run this again when your MCP servers change.")
        missing = sum(1 for tool in tools if not tool["description"])
        if missing:
            stdout(
                f"There is no description for {missing} of them, because the host lists only "
                "names. They match on their name; descriptions you add to the file are kept."
            )
    return 0


def host_available(stderr: Sink | None = None) -> bool:
    """Whether the host is on PATH, after saying where to get it when it is not."""
    return _require_host(None, stderr or _stderr_line) is not None


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
