"""The `relaymind openclaw` helper (RM33, RM12, RM45, RM46; spec 7.5, 7.7, 14.1; AC24, AC41).

The helper runs only the host's own public commands. Here the host is a stub
that keeps the real command forms and logs every call, so a test can say
exactly what ran, in what order, and what was sent on standard input. A real
host run is an operator step, never this suite.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from relaymind import launcher, openclaw_setup

TOKEN = "rm-secret-token-0123456789"
TARBALL = "relaymind-openclaw-provider-0.1.0.tgz"

STUB_HOST = r"""
import json, os, sys

args = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "stdin": stdin}) + "\n")
if args == ["--version"]:
    line = "OpenClaw " + os.environ.get("STUB_VERSION", "2026.10.7") + " (stub)"
    stream = sys.stderr if os.environ.get("STUB_VERSION_STREAM") == "stderr" else sys.stdout
    print(line, file=stream)
    raise SystemExit(0)
if args[:2] == ["config", "get"]:
    configured = os.environ.get("STUB_BASE_URL")
    if configured is None:
        print("Unknown config path", file=sys.stderr)
        raise SystemExit(1)
    print(configured)
    raise SystemExit(0)
fail = os.environ.get("STUB_FAIL")
if fail and " ".join(args).startswith(fail):
    print("stub: refused " + fail, file=sys.stderr)
    raise SystemExit(1)
if args[:2] == ["mcp", "probe"]:
    print(os.environ.get("STUB_PROBE", '{"servers": {}, "tools": [], "diagnostics": []}'))
    raise SystemExit(int(os.environ.get("STUB_PROBE_CODE", "0")))
if args[:2] == ["plugins", "enable"] and os.environ.get("STUB_NOISE"):
    sys.stdout.buffer.write(b"\xff\xfe not utf-8\n")
if args[:2] == ["plugins", "inspect"]:
    status = os.environ.get("STUB_STATUS", "loaded")
    print(json.dumps({
        "plugin": {
            "status": status,
            "enabled": os.environ.get("STUB_ENABLED", "true") == "true",
            "providerIds": json.loads(os.environ.get("STUB_PROVIDER_IDS", '["relaymind"]')),
        },
        "diagnostics": json.loads(os.environ.get("STUB_DIAGNOSTICS", "[]")),
    }))
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    script = tmp_path / "stub_host.py"
    script.write_text(STUB_HOST, encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.delenv("STUB_VERSION", raising=False)
    monkeypatch.delenv("STUB_FAIL", raising=False)
    for name in (
        "STUB_STATUS",
        "STUB_ENABLED",
        "STUB_DIAGNOSTICS",
        "STUB_NOISE",
        "STUB_VERSION_STREAM",
        "STUB_PROVIDER_IDS",
        "STUB_PROBE",
        "STUB_PROBE_CODE",
        "STUB_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    class Host:
        command = [sys.executable, str(script)]

        @staticmethod
        def calls() -> list[dict]:
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text("utf-8").splitlines()]

        @staticmethod
        def args() -> list[list[str]]:
            return [call["args"] for call in Host.calls()]

        @staticmethod
        def changes() -> list[list[str]]:
            """The calls that can change the host, leaving out the ones that only read it."""
            return [
                args
                for args in Host.args()
                if args != ["--version"] and args[:2] not in (["config", "get"], ["mcp", "probe"])
            ]

    return Host


@pytest.fixture
def release(tmp_path) -> Path:
    """A directory shaped like the output of `npm run pack:release`."""
    return make_release(tmp_path / "release", openclaw_setup.DECLARED_RANGE)


def make_release(directory: Path, declared: str | None) -> Path:
    directory.mkdir()
    payload = b"plugin bytes"
    (directory / TARBALL).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (directory / "SHA256SUMS").write_text(f"{digest}  {TARBALL}\n", encoding="utf-8")
    record = {"filename": TARBALL, "sha256": digest}
    if declared is not None:
        record["openclaw"] = {"pluginApi": declared, "qualified": "2026.10.7"}
    (directory / "release.json").write_text(json.dumps(record), encoding="utf-8")
    return directory


class Out:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []

    def stdout(self, line: str) -> None:
        self.out.append(line)

    def stderr(self, line: str) -> None:
        self.err.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.out + self.err)


def run_install(release, host, out, base_url="http://127.0.0.1:8080/v1") -> int:
    return openclaw_setup.install_plugin(
        release,
        base_url=base_url,
        host_command=host.command,
        stdout=out.stdout,
        stderr=out.stderr,
    )


# -- install ------------------------------------------------------------------------------


def test_install_runs_the_host_steps_in_order(release, host):
    out = Out()
    assert run_install(release, host, out) == 0
    assert host.args() == [
        ["--version"],
        ["plugins", "install", f"npm-pack:{release / TARBALL}", "--force", "--accept-capabilities"],
        ["config", "set", "plugins.entries.relaymind.config.baseUrl", "http://127.0.0.1:8080/v1"],
        ["plugins", "enable", "relaymind"],
        ["plugins", "inspect", "relaymind", "--runtime", "--json"],
    ]
    # The next step is named; the helper does not start the gateway or store a token.
    assert "relaymind serve --config" in out.text
    assert "relaymind openclaw token" in out.text


@pytest.mark.parametrize("version", ["2026.10.7", "2026.10.7-1"])
def test_the_qualified_release_is_not_called_unqualified(release, host, monkeypatch, version):
    monkeypatch.setenv("STUB_VERSION", version)
    out = Out()
    assert run_install(release, host, out) == 0
    assert "unqualified" not in out.text.lower()


@pytest.mark.parametrize(
    "version",
    [
        "2026.10.8",
        "2026.12.31",
        "2026.10.8-1",
        "2026.10.8-beta.1",
        "2026.11.1-rc.1",
        "2026.12.31-rc.2",
    ],
)
def test_a_later_release_in_range_installs_and_is_called_unqualified(
    release, host, monkeypatch, version
):
    monkeypatch.setenv("STUB_VERSION", version)
    out = Out()
    assert run_install(release, host, out) == 0
    assert any(args[:2] == ["plugins", "install"] for args in host.args())
    assert out.text.lower().count("unqualified") == 1
    assert "2026.10.7" in out.text


@pytest.mark.parametrize(
    "version",
    [
        "2026.10.6",
        "2025.12.1",
        "2027.1.1",
        "2027.0.0",
        "2027.0.0-beta.1",
        # SemVer: a pre-release of the lower bound ranks below it. The host itself
        # would load these (it drops the suffix); the helper refuses them first.
        "2026.10.7-beta.2",
        "2026.10.7-rc.1",
        "2026.10.7-alpha.1",
    ],
)
def test_a_release_outside_the_range_is_refused_before_any_change(
    release, host, monkeypatch, version
):
    monkeypatch.setenv("STUB_VERSION", version)
    out = Out()
    assert run_install(release, host, out) == 2
    assert host.args() == [["--version"]]
    assert version in out.text


@pytest.mark.parametrize(
    "version", ["none-at-all", "2026.10.7.1", "2026.10.7+local", "v2026.10.7rc1", "2026.10.7-x"]
)
def test_an_unreadable_host_version_is_refused(release, host, monkeypatch, version):
    # A fourth part or an unknown suffix is never rounded to the qualified release.
    monkeypatch.setenv("STUB_VERSION", version)
    out = Out()
    assert run_install(release, host, out) == 2
    assert host.args() == [["--version"]]


def test_a_missing_host_is_refused_with_the_fix(release, monkeypatch):
    monkeypatch.setattr(openclaw_setup.shutil, "which", lambda _name: None)
    out = Out()
    code = openclaw_setup.install_plugin(
        release,
        base_url="http://127.0.0.1:8080/v1",
        stdout=out.stdout,
        stderr=out.stderr,
    )
    assert code == 2
    assert "openclaw" in out.text and "PATH" in out.text


def test_a_checksum_mismatch_changes_nothing(release, host):
    (release / TARBALL).write_bytes(b"tampered")
    out = Out()
    assert run_install(release, host, out) == 2
    assert not any(args[:1] == ["plugins"] for args in host.args())
    assert "SHA256SUMS" in out.text


def test_a_release_json_whose_sha256_disagrees_is_refused(release, host):
    record = json.loads((release / "release.json").read_text("utf-8"))
    record["sha256"] = "0" * 64
    (release / "release.json").write_text(json.dumps(record), encoding="utf-8")
    out = Out()
    assert run_install(release, host, out) == 2
    assert host.args() == []
    assert "release.json" in out.text


def test_a_release_json_without_a_sha256_is_refused(release, host):
    record = json.loads((release / "release.json").read_text("utf-8"))
    del record["sha256"]
    (release / "release.json").write_text(json.dumps(record), encoding="utf-8")
    assert run_install(release, host, Out()) == 2
    assert host.args() == []


def test_an_empty_host_command_is_treated_as_no_host(release):
    out = Out()
    code = openclaw_setup.install_plugin(
        release, host_command=[], stdout=out.stdout, stderr=out.stderr
    )
    assert code == 2
    assert "PATH" in out.text
    assert openclaw_setup.host_invocation([], ["x"], platform="win32") == ["x"]


@pytest.mark.parametrize("missing", [TARBALL, "SHA256SUMS", "release.json"])
def test_an_incomplete_release_directory_is_refused(release, host, missing):
    (release / missing).unlink()
    out = Out()
    assert run_install(release, host, out) == 2
    assert host.args() == []
    assert missing in out.text


def test_a_sums_file_that_does_not_name_the_tarball_is_refused(release, host):
    digest = hashlib.sha256((release / TARBALL).read_bytes()).hexdigest()
    (release / "SHA256SUMS").write_text(f"{digest}  other.tgz\n", encoding="utf-8")
    out = Out()
    assert run_install(release, host, out) == 2
    assert host.args() == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com:8080/v1",
        "http://192.168.1.5:8080/v1",
        "ftp://127.0.0.1/v1",
        "nonsense",
        # Loopback but not what the plugin accepts: it would fail at register.
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8080/api",
        "http://user:secret@127.0.0.1:8080/v1",
        "http://127.0.0.1:8080/v1?x=1",
        "http://127.0.0.1:8080/v1#frag",
    ],
)
def test_a_base_url_that_is_not_loopback_http_is_refused(release, host, base_url):
    out = Out()
    assert run_install(release, host, out, base_url=base_url) == 2
    assert host.args() == []


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:9000/v1", "http://127.0.0.2:8080/v1", "http://[::1]:8080/v1"],
)
def test_loopback_base_urls_are_accepted(release, host, base_url):
    out = Out()
    assert run_install(release, host, out, base_url=base_url) == 0
    assert ["config", "set", "plugins.entries.relaymind.config.baseUrl", base_url] in host.args()


def test_a_host_that_prints_its_version_on_standard_error_is_read(release, host, monkeypatch):
    monkeypatch.setenv("STUB_VERSION_STREAM", "stderr")
    assert run_install(release, host, Out()) == 0


def test_the_address_is_stored_in_the_plugin_normal_form(release, host):
    out = Out()
    assert run_install(release, host, out, base_url="http://LOCALHOST:9000/v1/") == 0
    assert [
        "config",
        "set",
        "plugins.entries.relaymind.config.baseUrl",
        "http://localhost:9000/v1",
    ] in (host.args())
    # A re-run sets the address back to the default unless asked; the output says so.
    assert "--base-url" in out.text


def test_the_range_comes_from_the_release_folder(tmp_path, host, monkeypatch):
    """An older folder that declared the exact pin is checked against that, not today's range."""
    old = make_release(tmp_path / "old", "2026.10.7")
    monkeypatch.setenv("STUB_VERSION", "2026.11.1")
    out = Out()
    assert run_install(old, host, out) == 2
    assert host.args() == [["--version"]]
    monkeypatch.setenv("STUB_VERSION", "2026.10.7")
    assert run_install(old, host, Out()) == 0


@pytest.mark.parametrize("declared", [None, "", "^2026.10.7", ">=2026.10.7 <=2027.1.1", 5])
def test_a_release_folder_without_a_readable_range_is_refused(tmp_path, host, declared):
    folder = make_release(tmp_path / "r", None)
    record = json.loads((folder / "release.json").read_text("utf-8"))
    if declared is not None:
        record["openclaw"] = {"pluginApi": declared}
    (folder / "release.json").write_text(json.dumps(record), encoding="utf-8")
    out = Out()
    assert run_install(folder, host, out) == 2
    assert not any(args[:1] == ["plugins"] for args in host.args())


def test_a_failing_step_stops_the_install_and_shows_the_host_message(release, host, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "plugins enable")
    out = Out()
    assert run_install(release, host, out) == 1
    assert ["plugins", "inspect", "relaymind", "--runtime", "--json"] not in host.args()
    assert "refused plugins enable" in out.text
    # The install already ran, so the operator is told the host may be partly changed.
    assert "partly changed" in out.text


def test_a_plugin_that_did_not_load_is_a_failure(release, host, monkeypatch):
    monkeypatch.setenv("STUB_STATUS", "error")
    out = Out()
    assert run_install(release, host, out) == 1
    assert "not loaded" in out.text.lower()


def test_a_plugin_that_is_not_enabled_is_a_failure(release, host, monkeypatch):
    monkeypatch.setenv("STUB_ENABLED", "false")
    out = Out()
    assert run_install(release, host, out) == 1
    assert "not enabled" in out.text


def test_host_diagnostics_are_shown_when_the_plugin_has_problems(release, host, monkeypatch):
    monkeypatch.setenv("STUB_DIAGNOSTICS", json.dumps([{"message": "bad baseUrl"}]))
    out = Out()
    assert run_install(release, host, out) == 1
    assert "bad baseUrl" in out.text


@pytest.mark.parametrize("providers", ["null", "[]", '"relaymind"'])
def test_a_missing_or_malformed_provider_list_is_a_failure_not_a_crash(
    release, host, monkeypatch, providers
):
    monkeypatch.setenv("STUB_PROVIDER_IDS", providers)
    out = Out()
    assert run_install(release, host, out) == 1
    assert "relaymind:" in out.text


def test_host_output_that_is_not_utf8_does_not_crash_the_helper(release, host, monkeypatch):
    monkeypatch.setenv("STUB_NOISE", "1")
    assert run_install(release, host, Out()) == 0


def test_a_step_without_input_gets_no_standard_input(release, host):
    run_install(release, host, Out())
    assert all(call["stdin"] == "" for call in host.calls())


# -- token --------------------------------------------------------------------------------


def test_token_goes_to_the_host_on_standard_input_only(host):
    out = Out()
    code = openclaw_setup.store_token(
        f"{TOKEN}\n", host_command=host.command, stdout=out.stdout, stderr=out.stderr
    )
    assert code == 0
    calls = host.calls()
    assert calls[0] == {
        "args": ["models", "auth", "paste-api-key", "--provider", "relaymind"],
        "stdin": f"{TOKEN}\n",
    }
    assert calls[1]["args"] == ["models", "list", "--refresh", "--provider", "relaymind", "--json"]
    # Never an argument, never printed.
    assert all(TOKEN not in " ".join(call["args"]) for call in calls)
    assert TOKEN not in out.text


def test_at_a_terminal_the_token_is_read_without_echo(monkeypatch):
    class Terminal:
        @staticmethod
        def isatty() -> bool:
            return True

        @staticmethod
        def read() -> str:  # a terminal read() would wait for Ctrl-D
            raise AssertionError("must not read to end of file at a terminal")

    prompts = []
    monkeypatch.setattr(openclaw_setup.sys, "stdin", Terminal)
    monkeypatch.setattr(
        openclaw_setup.getpass, "getpass", lambda prompt: prompts.append(prompt) or TOKEN
    )
    assert openclaw_setup.read_token() == TOKEN
    assert prompts and TOKEN not in prompts[0]


def test_ctrl_d_at_the_prompt_is_a_clean_refusal_not_a_traceback(monkeypatch, host):
    class Terminal:
        @staticmethod
        def isatty() -> bool:
            return True

    def end_of_input(_prompt):
        raise EOFError

    monkeypatch.setattr(openclaw_setup.sys, "stdin", Terminal)
    monkeypatch.setattr(openclaw_setup.getpass, "getpass", end_of_input)
    token = openclaw_setup.read_token()
    assert token == ""
    out = Out()
    code = openclaw_setup.store_token(
        token, host_command=host.command, stdout=out.stdout, stderr=out.stderr
    )
    assert code == 2
    assert host.calls() == []


def test_piped_input_is_read_to_the_end(monkeypatch):
    import io

    monkeypatch.setattr(openclaw_setup.sys, "stdin", io.StringIO(f"{TOKEN}\n"))
    assert openclaw_setup.read_token() == f"{TOKEN}\n"


@pytest.mark.parametrize("token", ["", "   \n", "two words\n", "a\nb\n"])
def test_a_token_that_is_not_one_word_is_refused(host, token):
    out = Out()
    code = openclaw_setup.store_token(
        token, host_command=host.command, stdout=out.stdout, stderr=out.stderr
    )
    assert code == 2
    assert host.calls() == []


def test_a_host_message_that_echoes_the_token_is_not_shown(host, monkeypatch):
    # The host's error text could repeat what it was given; the helper never relays it.
    monkeypatch.setenv("STUB_FAIL", "models auth")
    out = Out()
    code = openclaw_setup.store_token(
        f"{TOKEN}\n", host_command=host.command, stdout=out.stdout, stderr=out.stderr
    )
    assert code == 1
    assert TOKEN not in out.text


# -- the command line ---------------------------------------------------------------------


def test_the_command_line_reaches_install(monkeypatch, tmp_path):
    seen = {}

    def fake(release, *, base_url, stdout, stderr):
        seen.update(release=release, base_url=base_url)
        return 0

    monkeypatch.setattr(openclaw_setup, "install_plugin", fake)
    assert launcher.main(["openclaw", "install", "--release", str(tmp_path)]) == 0
    assert seen == {"release": tmp_path, "base_url": "http://127.0.0.1:8080/v1"}
    launcher.main(
        ["openclaw", "install", "--release", str(tmp_path), "--base-url", "http://[::1]:9/v1"]
    )
    assert seen["base_url"] == "http://[::1]:9/v1"


def test_the_command_line_reaches_token_through_standard_input(monkeypatch, capsys):
    seen = {}

    def fake(token, *, stdout, stderr):
        seen["token"] = token
        return 0

    monkeypatch.setattr(openclaw_setup, "store_token", fake)
    monkeypatch.setattr(openclaw_setup.shutil, "which", lambda _name: "/usr/bin/openclaw")
    monkeypatch.setattr(launcher.sys, "stdin", __import__("io").StringIO(f"{TOKEN}\n"))
    assert launcher.main(["openclaw", "token"]) == 0
    assert seen["token"] == f"{TOKEN}\n"


def test_token_has_no_argument_that_could_carry_it():
    with pytest.raises(SystemExit):
        launcher.main(["openclaw", "token", TOKEN])


def test_serve_still_needs_its_config_file():
    with pytest.raises(SystemExit):
        launcher.main(["serve"])


# -- the declared range has one source -----------------------------------------------------


def test_the_helper_and_the_package_declare_the_same_range():
    """Spec 0.70: package.json holds the text; the helper's numbers must be the same range."""
    package = json.loads(
        (Path(__file__).resolve().parents[1] / "integrations/openclaw/package.json").read_text(
            "utf-8"
        )
    )
    declared = package["openclaw"]["compat"]["pluginApi"]
    assert declared == package["peerDependencies"]["openclaw"] == openclaw_setup.DECLARED_RANGE
    build = package["openclaw"]["build"]["openclawVersion"]
    assert build == openclaw_setup.QUALIFIED_RELEASE_TEXT
    # The range admits the qualified release and nothing before it.
    qualified = openclaw_setup.QUALIFIED_RELEASE
    assert openclaw_setup.release_in_range(qualified, declared) is True
    assert openclaw_setup.release_in_range((2026, 10, 6), declared) is False
    # Pre-releases follow SemVer: below their own release, above the one before it, and a
    # numeric correction is the release itself.
    assert openclaw_setup.release_in_range(qualified, declared, "-beta.2") is False
    assert openclaw_setup.release_in_range(qualified, declared, "-1") is True
    assert openclaw_setup.release_in_range((2026, 10, 8), declared, "-beta.1") is True
    assert openclaw_setup.release_in_range((2027, 0, 0), declared, "-beta.1") is False


def test_the_shared_windows_quoting_refuses_what_cmd_would_rewrite():
    shim = r"C:\x\openclaw.cmd"
    line = openclaw_setup.host_invocation([shim], ["a b"], platform="win32", comspec="cmd.exe")
    assert line == '"cmd.exe" /d /v:off /s /c ""C:\\x\\openclaw.cmd" "a b""'
    with pytest.raises(OSError):
        openclaw_setup.host_invocation([shim], ["100%"], platform="win32")
    assert openclaw_setup.host_invocation(["openclaw"], ["x"], platform="linux") == [
        "openclaw",
        "x",
    ]


# -- guidance -----------------------------------------------------------------------------

PLUGIN_ENTRY = "plugins.entries.relaymind-guidance"
PROBE = json.dumps(
    {
        "servers": {"weather": {"tools": 2}},
        "tools": ["weather__get_weather", "weather__get_alerts"],
        "diagnostics": [],
    }
)


@pytest.fixture
def plugin(tmp_path) -> Path:
    folder = tmp_path / "guidance-plugin"
    folder.mkdir()
    (folder / "openclaw.plugin.json").write_text('{"id": "relaymind-guidance"}', encoding="utf-8")
    (folder / "package.json").write_text(
        '{"openclaw": {"compat": {"pluginApi": "2026.10.7"}}}', encoding="utf-8"
    )
    return folder


def run(host, plugin, out, **options) -> int:
    return openclaw_setup.setup_guidance(
        plugin=plugin, host_command=host.command, stdout=out.stdout, stderr=out.stderr, **options
    )


def config_sets(host) -> dict[str, str]:
    return {call[2]: call[3] for call in host.args() if call[:2] == ["config", "set"]}


def test_it_runs_the_host_steps_in_order_and_empties_the_listed_catalog(host, plugin):
    out = Out()
    assert run(host, plugin, out) == 0
    assert host.changes() == [
        ["plugins", "install", "--link", str(plugin), "--force", "--accept-capabilities"],
        ["config", "set", f"{PLUGIN_ENTRY}.hooks.allowConversationAccess", "true", "--strict-json"],
        ["config", "set", f"{PLUGIN_ENTRY}.config.baseUrl", "http://127.0.0.1:8080/v1"],
        ["plugins", "enable", "relaymind-guidance"],
        ["plugins", "inspect", "relaymind-guidance", "--runtime", "--json"],
        # Last, so a plugin that did not load never leaves the host with no skill list and no hint.
        ["config", "set", "skills.limits.maxSkillsInPrompt", "0", "--strict-json"],
    ]


def test_without_a_tools_file_it_never_connects_to_mcp_servers(host, plugin):
    assert run(host, plugin, Out()) == 0
    assert not [call for call in host.args() if call[0] == "mcp"]
    assert f"{PLUGIN_ENTRY}.config.toolsFile" not in config_sets(host)


def test_the_limit_is_set_only_when_the_operator_asks(host, plugin):
    assert run(host, plugin, Out(), limit="strict") == 0
    assert config_sets(host)[f"{PLUGIN_ENTRY}.config.limit"] == "strict"


def test_the_tools_file_holds_what_the_host_lists(host, plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_PROBE", PROBE)
    path = tmp_path / "state" / "tools.json"
    out = Out()
    assert run(host, plugin, out, tools_file=path) == 0
    assert json.loads(path.read_text("utf-8")) == [
        {"name": "weather__get_alerts", "description": "", "generated": True},
        {"name": "weather__get_weather", "description": "", "generated": True},
    ]
    assert host.args().index(["mcp", "probe", "--json"]) < host.args().index(host.changes()[0])
    assert config_sets(host)[f"{PLUGIN_ENTRY}.config.toolsFile"] == str(path)
    assert "2 tools" in out.text
    assert "no description" in out.text


def test_a_relative_tools_file_is_stored_absolute(host, plugin, tmp_path, monkeypatch):
    # The plugin refuses a relative path, which would turn it off.
    monkeypatch.chdir(tmp_path)
    assert run(host, plugin, Out(), tools_file=Path("tools.json")) == 0
    stored = Path(config_sets(host)[f"{PLUGIN_ENTRY}.config.toolsFile"])
    assert stored.is_absolute()
    assert stored == (tmp_path / "tools.json").resolve()


def test_a_new_run_keeps_descriptions_and_hand_added_tools(host, plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_PROBE", PROBE)
    path = tmp_path / "tools.json"
    path.write_text(
        json.dumps(
            [
                {"name": "weather__get_weather", "description": "Weather for a city"},
                {"name": "mcporter__search", "description": "Added by hand"},
                {"name": "gone__tool", "description": "From a removed server", "generated": True},
            ]
        ),
        encoding="utf-8",
    )
    out = Out()
    assert run(host, plugin, out, tools_file=path) == 0
    assert json.loads(path.read_text("utf-8")) == [
        {"name": "mcporter__search", "description": "Added by hand"},
        {"name": "weather__get_alerts", "description": "", "generated": True},
        {"name": "weather__get_weather", "description": "Weather for a city", "generated": True},
    ]
    assert "no description for 1" in out.text


def test_a_hand_added_tool_with_only_a_name_is_kept_and_counted(
    host, plugin, tmp_path, monkeypatch
):
    path = tmp_path / "tools.json"
    path.write_text('[{"name": "mcporter__search"}]', encoding="utf-8")
    out = Out()
    assert run(host, plugin, out, tools_file=path) == 0
    assert json.loads(path.read_text("utf-8")) == [{"name": "mcporter__search", "description": ""}]
    assert "no description for 1" in out.text


def test_a_tools_file_it_is_not_allowed_to_read_is_refused(host, plugin, tmp_path, monkeypatch):
    path = tmp_path / "tools.json"

    def denied(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_text", denied, raising=True)
    known = openclaw_setup._known_tools(path)
    assert isinstance(known, str)


@pytest.mark.parametrize(
    "existing",
    [
        "not json",
        '{"name": "a"}',
        '[{"description": "no name"}]',
        '["a"]',
        '[{"name": 5, "description": "number"}]',
        '[{"name": "a", "description": 5}]',
    ],
)
def test_a_tools_file_it_cannot_read_is_never_replaced(
    host, plugin, tmp_path, monkeypatch, existing
):
    monkeypatch.setenv("STUB_PROBE", PROBE)
    path = tmp_path / "tools.json"
    path.write_text(existing, encoding="utf-8")
    out = Out()
    assert run(host, plugin, out, tools_file=path) == 2
    assert path.read_text("utf-8") == existing
    assert host.changes() == []
    assert "nothing was changed" in out.text


def test_a_server_that_did_not_answer_changes_nothing(host, plugin, tmp_path, monkeypatch):
    # The list would be short, and a strict limit would then not hold for the missing tools.
    monkeypatch.setenv(
        "STUB_PROBE",
        json.dumps(
            {
                "servers": {},
                "tools": ["weather__get_weather"],
                "diagnostics": [{"serverName": "broken", "message": "spawn ENOENT"}],
            }
        ),
    )
    monkeypatch.setenv("STUB_PROBE_CODE", "1")
    path = tmp_path / "tools.json"
    out = Out()
    assert run(host, plugin, out, tools_file=path) == 2
    assert not path.exists()
    assert host.changes() == []
    assert "broken" in out.text
    assert "nothing was changed" in out.text


@pytest.mark.parametrize(
    "probe",
    [
        "not json",
        "{}",
        '{"tools": "a", "diagnostics": []}',
        '{"tools": [1], "diagnostics": []}',
        '{"tools": [], "diagnostics": 5}',
    ],
)
def test_an_unreadable_probe_changes_nothing(host, plugin, tmp_path, monkeypatch, probe):
    monkeypatch.setenv("STUB_PROBE", probe)
    path = tmp_path / "tools.json"
    assert run(host, plugin, Out(), tools_file=path) == 2
    assert not path.exists()
    assert host.changes() == []


def test_a_host_with_no_servers_gets_an_empty_list(host, plugin, tmp_path):
    path = tmp_path / "tools.json"
    assert run(host, plugin, Out(), tools_file=path) == 0
    assert json.loads(path.read_text("utf-8")) == []


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1:8080/v1", "http://example.com/v1", "http://user:pw@127.0.0.1:8080/v1"],
)
def test_a_base_url_the_plugin_would_refuse_is_refused_first(host, plugin, url):
    out = Out()
    assert run(host, plugin, out, base_url=url) == 2
    assert host.args() == []


def test_a_folder_that_is_not_the_guidance_plugin_is_refused(host, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "openclaw.plugin.json").write_text('{"id": "something-else"}', encoding="utf-8")
    for folder in (other, tmp_path / "missing"):
        out = Out()
        assert run(host, folder, out) == 2
        assert "nothing was changed" in out.text
    assert host.args() == []


def test_it_refuses_without_the_host(plugin, monkeypatch):
    monkeypatch.setattr(openclaw_setup.shutil, "which", lambda _name: None)
    out = Out()
    code = openclaw_setup.setup_guidance(plugin=plugin, stdout=out.stdout, stderr=out.stderr)
    assert code == 2
    assert "not found" in out.text


def test_a_failed_step_stops_and_says_the_host_may_be_partly_changed(host, plugin, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "plugins enable")
    out = Out()
    assert run(host, plugin, out) == 1
    assert ["plugins", "inspect", "relaymind-guidance", "--runtime", "--json"] not in host.args()
    assert "partly changed" in out.text


def test_a_plugin_that_did_not_load_leaves_the_skill_list_alone(host, plugin, monkeypatch):
    monkeypatch.setenv("STUB_STATUS", "error")
    out = Out()
    assert run(host, plugin, out) == 1
    assert "not loaded" in out.text
    assert "skills.limits.maxSkillsInPrompt" not in config_sets(host)


@pytest.mark.parametrize("version", ["2026.11.1", "2026.10.7-beta.1"])
def test_a_host_release_the_plugin_does_not_declare_changes_nothing(
    host, plugin, tmp_path, monkeypatch, version
):
    monkeypatch.setenv("STUB_VERSION", version)
    monkeypatch.setenv("STUB_PROBE", PROBE)
    path = tmp_path / "tools.json"
    out = Out()
    assert run(host, plugin, out, tools_file=path) == 2
    assert not path.exists()
    assert host.changes() == []
    assert "outside the range" in out.text


def test_a_plugin_with_no_declared_release_is_refused(host, plugin):
    (plugin / "package.json").write_text("{}", encoding="utf-8")
    out = Out()
    assert run(host, plugin, out) == 2
    assert host.changes() == []


def test_a_new_run_keeps_the_address_set_earlier(host, plugin, monkeypatch):
    monkeypatch.setenv("STUB_BASE_URL", "http://127.0.0.1:9000/v1")
    assert run(host, plugin, Out()) == 0
    assert config_sets(host)[f"{PLUGIN_ENTRY}.config.baseUrl"] == "http://127.0.0.1:9000/v1"


def test_a_stored_address_the_plugin_would_refuse_gives_way_to_the_default(
    host, plugin, monkeypatch
):
    monkeypatch.setenv("STUB_BASE_URL", "https://example.com/v1")
    assert run(host, plugin, Out()) == 0
    assert config_sets(host)[f"{PLUGIN_ENTRY}.config.baseUrl"] == "http://127.0.0.1:8080/v1"


@pytest.mark.parametrize(
    "stored",
    ["http://127.0.0.1:9000", "http://127.0.0.1:9000/", "http://127.0.0.1:9000/gateway"],
)
def test_a_stored_address_with_another_path_keeps_its_port(host, plugin, monkeypatch, stored):
    # The plugin uses only the origin, so this is still the operator's gateway.
    monkeypatch.setenv("STUB_BASE_URL", stored)
    assert run(host, plugin, Out()) == 0
    assert config_sets(host)[f"{PLUGIN_ENTRY}.config.baseUrl"] == "http://127.0.0.1:9000/v1"


def test_a_stored_address_that_is_not_a_url_gives_way_to_the_default(host, plugin, monkeypatch):
    monkeypatch.setenv("STUB_BASE_URL", "http://[::1")
    assert run(host, plugin, Out()) == 0
    assert config_sets(host)[f"{PLUGIN_ENTRY}.config.baseUrl"] == "http://127.0.0.1:8080/v1"


def test_an_address_the_operator_passes_wins_over_the_stored_one(host, plugin, monkeypatch):
    monkeypatch.setenv("STUB_BASE_URL", "http://127.0.0.1:9000/v1")
    assert run(host, plugin, Out(), base_url="http://127.0.0.1:7000/v1") == 0
    assert config_sets(host)[f"{PLUGIN_ENTRY}.config.baseUrl"] == "http://127.0.0.1:7000/v1"


def test_a_hung_host_call_returns_at_its_time_limit():
    script = "import time; time.sleep(30)"
    code, _out, reason = openclaw_setup._run([sys.executable, "-c", script], [], None, 0.2)
    assert code == -1
    assert "did not finish in time" in reason


def test_a_hung_windows_host_call_ends_the_whole_process_tree(monkeypatch):
    # Killing cmd.exe alone leaves its node child holding the pipes, and the read never ends.
    calls = []

    class Hung:
        pid = 4321

        def __init__(self, *args, **kwargs):
            self.killed = False

        def communicate(self, input=None, timeout=None):
            if not self.killed:
                self.killed = True
                raise openclaw_setup.subprocess.TimeoutExpired("openclaw", timeout)
            return "", ""

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(openclaw_setup.sys, "platform", "win32")
    monkeypatch.setattr(openclaw_setup.subprocess, "Popen", Hung)
    monkeypatch.setattr(openclaw_setup.subprocess, "run", lambda args, **kwargs: calls.append(args))
    code, _out, _reason = openclaw_setup._run(["openclaw"], ["mcp", "probe"], None, 0.1)
    assert code == -1
    assert ["taskkill", "/T", "/F", "/PID", "4321"] in calls


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError("taskkill"), openclaw_setup.subprocess.TimeoutExpired("taskkill", 5.0)],
)
def test_a_taskkill_that_fails_still_kills_and_reaps_the_host_call(monkeypatch, failure):
    calls = []

    class Hung:
        pid = 4321

        def kill(self):
            calls.append("kill")

        def communicate(self, input=None, timeout=None):
            calls.append("reap")
            return "", ""

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(openclaw_setup.sys, "platform", "win32")
    monkeypatch.setattr(openclaw_setup.subprocess, "run", broken)
    openclaw_setup._stop(Hung())
    assert calls == ["kill", "reap"]


def test_a_diagnostic_without_a_server_name_is_reported_as_unknown(
    host, plugin, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "STUB_PROBE",
        json.dumps({"servers": {}, "tools": [], "diagnostics": [{"message": "spawn ENOENT"}]}),
    )
    monkeypatch.setenv("STUB_PROBE_CODE", "1")
    out = Out()
    assert run(host, plugin, out, tools_file=tmp_path / "tools.json") == 2
    assert "did not answer: unknown" in out.text
    assert "None" not in out.text


def test_a_tools_file_under_the_home_folder_is_expanded(host, plugin, tmp_path, monkeypatch):
    # cmd.exe and a quoted argument leave the ~ for the helper to expand.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(plugin)
    assert run(host, plugin, Out(), tools_file=Path("~/state/tools.json")) == 0
    expected = (tmp_path / "state" / "tools.json").resolve()
    assert Path(config_sets(host)[f"{PLUGIN_ENTRY}.config.toolsFile"]) == expected
    assert expected.is_file()
    assert not (plugin / "~").exists()


@pytest.mark.parametrize("given", ["relative", "home"])
def test_the_plugin_folder_is_installed_by_its_absolute_path(
    host, plugin, tmp_path, monkeypatch, given
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    folder = Path(plugin.name) if given == "relative" else Path("~") / plugin.name
    assert run(host, folder, Out()) == 0
    assert host.changes()[0][:4] == ["plugins", "install", "--link", str(plugin.resolve())]


def test_a_path_whose_home_folder_is_unknown_is_refused(host, plugin, monkeypatch):
    def unknown(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(openclaw_setup.Path, "expanduser", unknown)
    out = Out()
    assert run(host, plugin, out) == 2
    assert host.args() == []
    assert "nothing was changed" in out.text


@pytest.mark.parametrize("which", ["tools file", "plugin folder"])
def test_a_path_a_batch_shim_cannot_carry_is_refused_first(plugin, tmp_path, monkeypatch, which):
    # It would otherwise fail at a later host step, with the host already partly changed.
    def must_not_start(*args, **kwargs):
        raise AssertionError("the host was started")

    monkeypatch.setattr(openclaw_setup.sys, "platform", "win32")
    monkeypatch.setattr(openclaw_setup.subprocess, "Popen", must_not_start)
    folder = plugin
    tools_file = tmp_path / "tools.json"
    if which == "plugin folder":
        folder = plugin.rename(tmp_path / "100%plugin")
    else:
        tools_file = tmp_path / "100%tools.json"
    out = Out()
    result = openclaw_setup.setup_guidance(
        plugin=folder,
        tools_file=tools_file,
        host_command=["openclaw.cmd"],
        stdout=out.stdout,
        stderr=out.stderr,
    )
    assert result == 2
    assert "nothing was changed" in out.text
    assert "100%" in out.text
    assert not tools_file.exists()


def test_a_kill_that_fails_still_reaps_the_host_call():
    calls = []

    class Gone:
        pid = 4321

        def kill(self):
            raise ProcessLookupError("already gone")

        def communicate(self, input=None, timeout=None):
            calls.append("reap")
            return "", ""

    openclaw_setup._stop(Gone())
    assert calls == ["reap"]


def test_a_probe_that_fails_with_no_diagnostics_shows_the_host_message():
    report = '{"servers": {}, "tools": [], "diagnostics": []}'
    assert openclaw_setup._probed_tools(1, report, " config is broken\n") == "config is broken"
    assert openclaw_setup._probed_tools(1, report, "") == "the host's MCP list could not be read"


def test_a_write_error_with_no_system_message_is_still_readable(
    host, plugin, tmp_path, monkeypatch
):
    def broken(path, value):
        raise OSError("the volume went away")

    monkeypatch.setattr(openclaw_setup, "atomic_json", broken)
    out = Out()
    assert run(host, plugin, out, tools_file=tmp_path / "tools.json") == 2
    assert "the volume went away" in out.text
    assert "None" not in out.text


def test_paths_given_as_text_are_accepted(host, plugin, tmp_path):
    path = tmp_path / "tools.json"
    assert run(host, str(plugin), Out(), tools_file=str(path)) == 0
    assert path.is_file()


def declare_range(plugin: Path) -> None:
    (plugin / "package.json").write_text(
        json.dumps({"openclaw": {"compat": {"pluginApi": ">=2026.10.7 <2027.0.0-0"}}}),
        encoding="utf-8",
    )


def test_a_later_release_the_plugin_allows_is_called_unqualified(host, plugin, monkeypatch):
    declare_range(plugin)
    monkeypatch.setenv("STUB_VERSION", "2026.11.2")
    out = Out()
    assert run(host, plugin, out) == 0
    assert out.text.lower().count("unqualified") == 1
    assert "2026.11.2" in out.text


def test_the_qualified_release_is_not_called_unqualified_by_guidance(host, plugin):
    declare_range(plugin)
    out = Out()
    assert run(host, plugin, out) == 0
    assert "unqualified" not in out.text.lower()


def test_the_token_is_not_read_when_the_host_is_missing(monkeypatch):
    monkeypatch.setattr(openclaw_setup.shutil, "which", lambda _name: None)

    def must_not_read():
        raise AssertionError("the token was read")

    monkeypatch.setattr(openclaw_setup, "read_token", must_not_read)
    errors: list[str] = []
    assert launcher.main(["openclaw", "token"], stderr=errors.append) == 2
    assert "not found" in errors[0]


def test_an_invalid_argument_names_the_guidance_command(capsys):
    with pytest.raises(SystemExit):
        launcher.main(["openclaw", "guidance", "--limit", "wall"])
    assert "relaymind openclaw guidance" in capsys.readouterr().err.split("error:")[1]


def test_the_closing_steps_name_what_is_still_the_operators(host, plugin):
    out = Out()
    assert run(host, plugin, out) == 0
    assert "[guidance]" in out.text
    assert "RELAYMIND_APP_TOKEN" in out.text
    assert "every agent" in out.text


def test_the_launcher_passes_the_options_through(plugin, tmp_path, monkeypatch):
    seen = {}

    def fake(**options):
        seen.update(options)
        return 0

    monkeypatch.setattr(openclaw_setup, "setup_guidance", fake)
    code = launcher.main(
        [
            "openclaw",
            "guidance",
            "--plugin",
            str(plugin),
            "--tools-file",
            str(tmp_path / "t.json"),
            "--limit",
            "strict",
            "--base-url",
            "http://127.0.0.1:9000/v1",
        ]
    )
    assert code == 0
    assert seen["plugin"] == plugin
    assert seen["tools_file"] == tmp_path / "t.json"
    assert seen["limit"] == "strict"
    assert seen["base_url"] == "http://127.0.0.1:9000/v1"


def test_the_default_plugin_folder_is_the_one_in_this_repository():
    manifest = openclaw_setup.GUIDANCE_PLUGIN_DIR / "openclaw.plugin.json"
    assert json.loads(manifest.read_text("utf-8"))["id"] == openclaw_setup.GUIDANCE_PLUGIN_ID


@pytest.mark.parametrize("name", ["docs/guidance.md", "docs/guidance.zh-CN.md"])
def test_the_tutorials_name_the_command_and_the_early_skill_list(name):
    root = Path(__file__).resolve().parent.parent
    text = (root / name).read_text("utf-8")
    assert "relaymind openclaw guidance --tools-file" in text
    assert "openclaw skills list --eligible --json" in text
    assert "official" not in text and "官方" not in text


def test_the_spec_defines_the_helper_and_its_acceptance_row():
    root = Path(__file__).resolve().parent.parent
    text = (root / "docs/SPECIFICATION.md").read_text("utf-8")
    assert "**Host setup helper (spec 0.71; AC41).**" in text
    assert "| AC41 |" in text
