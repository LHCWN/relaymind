#!/usr/bin/env python3
"""Real full turns: does the real model load the right skill, and at what input size (RM16, RM45)?

Each run uses the real OpenClaw host, the real relaymind-guidance plugin, a real gateway, and a real
external model through a recording loopback proxy that holds the key. Arms differ only in how the
skill list is prepared. The model always has read-only tools, because skill text is untrusted.

Add --similar holdout --lanes similar_en,similar_zh for similar skills, or --tools for MCP tools.
With --tools the right answer is choosing the tool, by describing or calling it, because a prompt
may hold too little to call it for real.

  RELAYMIND_EXTERNAL_API_KEY=... python scripts/guidance_turn_benchmark.py \\
      --catalog-dir /var/tmp/rm/catalog-v3 \\
      --cases evaluation/assets/matrix-development/cases.v3.json \\
      --output /var/tmp/rm/turns --per-lane 10 \\
      --model-base-url https://host/v1 --model some-model \\
      --local-manifest models/qwen3-0.6b-q4_k_m.manifest.json \\
      --local-artifact models/Qwen3-0.6B-Q4_K_M.gguf --local-url http://127.0.0.1:8033

Development evidence: the expected answers are developer-authored, and loading the right skill is
not task success. Identical arms vary, so run the default arm twice to see that variation.
"""

import argparse
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "integrations/openclaw/guidance-plugin"


def openclaw_command(platform=sys.platform):
    """The host's launcher. On Windows npm writes it as openclaw.cmd."""
    name = "openclaw.cmd" if platform == "win32" else "openclaw"
    return ROOT / "integrations/openclaw/node_modules/.bin" / name


OPENCLAW = openclaw_command()
READ_ONLY = ["read", "skills_read", "skills_search"]
HINT = "[RelayMind guidance, not from the user]"
MCP_SERVER = "gh"
PREFLIGHT_EXTRA = 3
# The whole of a tool result that says the call was refused. A skill's own text may use the same
# words, such as "blocked", so only the envelope of the host or the plugin counts.
REFUSAL_PREFIXES = ("RelayMind limit:", "Validation failed for tool")
REFUSAL_LINE = re.compile(r"^(?:Skill|Conversation) .* is not available to this agent")


def _refused(answer):
    text = answer.strip()
    if text.startswith(REFUSAL_PREFIXES) or REFUSAL_LINE.match(text):
        return True
    try:
        value = json.loads(text)
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("status") == "error"


@dataclass(frozen=True)
class Arm:
    name: str
    guided: bool
    keywords: str = "none"
    picker: str = "none"
    third_party: str | None = None
    limit: str = "hint"


ARMS = {
    "default": Arm("default", guided=False),
    "default_repeat": Arm("default_repeat", guided=False),
    "word": Arm("word", guided=True),
    "local": Arm("local", guided=True, keywords="local"),
    "local_external": Arm("local_external", guided=True, keywords="local", picker="external"),
    "third_party": Arm("third_party", guided=True, third_party="example_intent"),
    "external": Arm("external", guided=True, keywords="external", picker="external"),
    "local_strict": Arm("local_strict", guided=True, keywords="local", limit="strict"),
    "local_external_strict": Arm(
        "local_external_strict", guided=True, keywords="local", picker="external", limit="strict"
    ),
    "external_strict": Arm(
        "external_strict", guided=True, keywords="external", picker="external", limit="strict"
    ),
    "third_party_strict": Arm(
        "third_party_strict", guided=True, third_party="example_intent", limit="strict"
    ),
}


def sample_cases(cases, skills, *, per_lane, seed, lanes):
    """Positive cases with an installed target, chosen by seed before any outcome is seen."""
    rng = random.Random(seed)
    chosen = []
    for lane in lanes:
        group = sorted(
            (
                c
                for c in cases
                if c["lane"] == lane
                and c["reference_class"] == "positive"
                and any(a in skills for a in c["acceptable_asset_ids"])
            ),
            key=lambda c: c["id"],
        )
        rng.shuffle(group)
        chosen.extend(group[:per_lane])
    return chosen


def preflight_cases(sample):
    """The first prompt, and one a skill's Latin name cannot match: only its description can."""
    chosen = sample[:1]
    chosen += [c for c in sample[1:] if c["lane"] in ("zh_hans_to_zh_hans", "similar_zh")][:1]
    return chosen


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [])


def hinted_names(user_text):
    """The skill names of the plugin's hint in one user message."""
    found = re.search(
        r"(?:Skills that look relevant to this request|The skill that fits this request|"
        r"Similar skills fit this request): (.*?)\. ",
        user_text,
    )
    tools = re.search(r"Tools that look relevant: (.*?)\. ", user_text)
    names = found.group(1).split(", ") if HINT in user_text and found else []
    if HINT in user_text and tools:
        names += [n.removeprefix(f"{MCP_SERVER}__") for n in tools.group(1).split(", ")]
    return names


def _skill_file_folder(path):
    """The folder name of a SKILL.md path written with / or \\, or None."""
    # The host's read tool also drops a broken XML end a model can write, as SKILL.md</arg_value>>.
    path = re.sub(r"(?i)(skill\.md)\s*<.*$", r"\1", str(path or ""))
    parts = [part for part in re.split(r"[\\/]", path) if part]
    return parts[-2] if len(parts) >= 2 and parts[-1].lower() == "skill.md" else None


def _call_key(value):
    """A tool call's ID in a form that survives the host's rewriting: it drops the underscore."""
    return re.sub(r"[^A-Za-z0-9]", "", value) if isinstance(value, str) else value


_SELECTORS = ("id", "toolId", "name")
# Tools every catalog has, which a tool_call can name besides the MCP tools.
_CORE_TOOLS = {"read", "skills_read", "skills_search"}


def _first_set(params, keys):
    """JavaScript's a ?? b ?? c: the first value that is not missing or null."""
    return next((params[k] for k in keys if params.get(k) is not None), None)


def _host_call(arguments, known):
    """The target and its arguments of a tool_call, as OpenClaw 2026.10.7 reads them.

    prepareToolSearchDispatcherArguments lifts a target named inside args or input; then
    readToolSearchCallArgs takes the selector whose value names a tool (else the first one set) and
    reads a non-empty args or input with args.* keys under it, or else every other key. `known`
    holds the tool names. The plugin's tests check the same table against the host's own code.
    """
    params = arguments if isinstance(arguments, dict) else {}
    if not any(k in params for k in _SELECTORS):
        nested = _first_set(params, ("args", "input"))
        lifted = _first_set(nested, _SELECTORS) if isinstance(nested, dict) else None
        if isinstance(lifted, str) and lifted.strip():
            rest = {k: v for k, v in params.items() if k not in ("args", "input")}
            params = {**rest, **nested, "id": lifted}
    dotted = {k[5:]: v for k, v in params.items() if k.startswith("args.") and len(k) > 5}
    nested = _first_set(params, ("args", "input"))
    if isinstance(nested, str):  # A model may write them as a JSON string.
        try:
            nested = json.loads(nested)
        except ValueError:
            nested = {}
    if nested is not None and nested != {}:
        target = _first_set(params, _SELECTORS)
        args = {**dotted, **nested} if isinstance(nested, dict) else {}
        return (target.strip() if isinstance(target, str) and target.strip() else None), args

    def names(value):
        parts = value.strip().split(":")
        return any(":".join(parts[i:]) in known for i in range(len(parts)))

    matching = [k for k in _SELECTORS if isinstance(params.get(k), str) and names(params[k])]
    selector = (
        matching[0]
        if matching
        else next((k for k in _SELECTORS if params.get(k) is not None), None)
    )
    wrapper = {"args", "input", *matching} | (set() if matching else {selector or "id"})
    flat = {k: v for k, v in params.items() if k not in wrapper and not k.startswith("args.")}
    target = params.get(selector) if selector else None
    return (target.strip() if isinstance(target, str) and target.strip() else None), {
        **dotted,
        **flat,
    }


def _host_tool_name(raw, offered):
    """The tool the host runs for a written name (resolveExactAllowedToolName, then the structured
    forms: the part before a quote or <, / read as ., and the parts after a dot). Unknown: as is."""
    if not isinstance(raw, str) or not offered:
        return raw

    def match(name):
        if name in offered:
            return name
        folded = [o for o in offered if o.lower() == name.lower()]
        return folded[0] if len(set(folded)) == 1 else None

    seeds, trimmed = [], raw.strip()
    cut = [i for i in (trimmed.find(c) for c in "\"'<") if i > 0]
    for seed in [trimmed, *([trimmed[: min(cut)]] if cut else [])]:
        seeds.append(seed)
        dotted = seed.replace("/", ".")
        parts = [p for p in dotted.split(".") if p.strip()]
        seeds += [dotted, *(".".join(parts[i:]) for i in range(1, len(parts)))]
    for seed in seeds:
        if (found := match(seed.strip())) is not None:
            return found
    return raw


def _result_ids(rows):
    """For each call, the ID the host gave it, which can differ from the model's own.

    The host shortens long IDs and renames one used before, so a call is paired by position with
    the same assistant message in the next recorded request, not by name.
    """
    ids = {}
    for position, row in enumerate(rows[:-1]):
        following = rows[position + 1]["request"]["messages"]
        sent = next((m for m in reversed(following) if m.get("role") == "assistant"), None)
        given = [c.get("id") for c in (sent or {}).get("tool_calls") or []]
        if len(given) == len(row["calls"]):
            for index, name in enumerate(given):
                ids[(position, index)] = name
    return ids


def grade(rows, acceptable):
    """What the model did, from the recorded exchanges only."""
    results = {}
    for row in rows:
        for m in row["request"]["messages"]:
            if m.get("role") == "tool":
                results[m.get("tool_call_id")] = _text(m.get("content"))
                results.setdefault(_call_key(m.get("tool_call_id")), _text(m.get("content")))
    given = _result_ids(rows)
    offered = {
        (t.get("function") or {}).get("name")
        for row in rows
        for t in row["request"].get("tools") or []
        if isinstance(t, dict)
    } - {None}
    known = offered | _CORE_TOOLS
    loaded, searched, called, refused, unmatched = [], 0, [], 0, 0
    for position, row in enumerate(rows):
        for index, call in enumerate(row["calls"]):
            call = dict(call, name=_host_tool_name(call.get("name"), offered))
            answer = results.get(given.get((position, index)))
            if answer is None:
                answer = results.get(_call_key(call.get("id")))
            if answer is None:  # No result was recorded, so nothing shows the call worked.
                # A later exchange should hold the result; a run can end after its last call.
                unmatched += position < len(rows) - 1
                continue
            if _refused(answer):
                refused += 1
                continue
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except ValueError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            if call["name"] in ("tool_call", "tool_describe"):
                # The model chose this tool. A prompt may hold too little to call it for real.
                # The host takes id, then toolId, then name (OpenClaw 2026.10.7).
                mcp = {n for n in offered if n.startswith(f"{MCP_SERVER}__")}
                target, inner_args = _host_call(arguments, known | mcp)
                inner = str(target or "").rsplit(":", 1)[-1]
                if inner.startswith(f"{MCP_SERVER}__"):
                    called.append(inner.removeprefix(f"{MCP_SERVER}__"))
                if call["name"] == "tool_describe":
                    continue
                call = dict(call, name=inner)
                arguments = inner_args
            elif call["name"].startswith(f"{MCP_SERVER}__"):
                called.append(call["name"].removeprefix(f"{MCP_SERVER}__"))
            if call["name"] == "skills_read":
                name = arguments.get("name")
                loaded.append(name.strip() if isinstance(name, str) else name)  # The host trims it.
            elif call["name"] == "read" and (
                folder := _skill_file_folder(arguments.get("path") or arguments.get("file_path"))
            ):
                loaded.append(folder)
            elif call["name"] == "skills_search":
                searched += 1
    # A run with any exchange of unknown size has an unknown total, never a smaller one.
    sizes = [(r.get("usage") or {}).get("prompt_tokens") for r in rows]
    outputs = [(r.get("usage") or {}).get("completion_tokens") for r in rows]
    known_size = bool(rows) and None not in sizes
    first = rows[0]["request"]["messages"] if rows else []
    system = "".join(_text(m["content"]) for m in first if m["role"] in ("system", "developer"))
    users = [m for m in first if m["role"] == "user"]
    block = re.search(r"<available_skills>.*?</available_skills>", system, re.S)
    hinted = hinted_names(_text(users[-1]["content"])) if users else []
    return dict(
        loaded=loaded,
        searched=searched,
        correct=bool((set(loaded) | set(called)) & set(acceptable)),
        any_skill=bool(loaded),
        called=called,
        found=len((set(loaded) | set(called)) & set(acceptable)),
        needed=len(acceptable),
        refused=refused,
        unmatched=unmatched,
        model_calls=len(rows),
        listed=block.group(0).count("<name>") if block else 0,
        sys_chars=len(system),
        hint_seen=bool(users) and HINT in _text(users[-1]["content"]),
        hinted=hinted,
        hint_right=bool(set(hinted) & set(acceptable)),
        first_input=sizes[0] if rows else None,
        total_input=sum(sizes) if known_size else None,
        total_output=sum(outputs) if known_size and None not in outputs else None,
        http=[r["status"] for r in rows],
        # The host got an answer from the proxy, but the model's own last answer was an error,
        # or its stream broke before the end.
        upstream_failed=bool(rows)
        and (rows[-1]["status"] != 200 or rows[-1].get("complete") is False),
    )


def check_applied(arm, outcome):
    """Refuse to spend on a setup that did not really apply. One prompt can rightly have no hint."""
    if not outcome["model_calls"]:
        raise RuntimeError(f"{arm.name}: no model call reached the proxy")
    if outcome.get("upstream_failed"):
        raise RuntimeError(f"{arm.name}: the model's own API failed, so this run shows nothing")
    if outcome.get("unmatched"):
        raise RuntimeError(f"{arm.name}: the grader could not match calls to their results")
    if arm.guided and outcome["listed"] != 0:
        raise RuntimeError(f"{arm.name}: guidance did not apply: {outcome['listed']} listed")
    if not arm.guided and (outcome["listed"] == 0 or outcome["hint_seen"]):
        raise RuntimeError(f"{arm.name}: the default setup did not apply")


def openclaw_config(arm, provider_url, model, gateway_url, tools=None):
    # Past the host's defaults of 200 and 300, a skill would not exist for any arm.
    limits = dict(maxSkillsLoadedPerSource=1000, maxCandidatesPerRoot=1000)
    config = dict(
        models=dict(
            providers=dict(
                glm=dict(
                    baseUrl=provider_url,
                    api="openai-completions",
                    apiKey="local-proxy",
                    models=[
                        dict(
                            id=model,
                            name="Model",
                            reasoning=False,
                            input=["text"],
                            contextWindow=128000,
                            maxTokens=2048,
                        )
                    ],
                )
            )
        ),
        tools=dict(toolSearch=False, allow=READ_ONLY),
        skills=dict(limits=limits),
    )
    if tools:
        # The fake server only answers "called": no real side effect can happen.
        config["tools"] = dict(
            allow=[*READ_ONLY, "tool_search", "tool_describe", "tool_call", f"{MCP_SERVER}__*"]
        )
        config["mcp"] = dict(servers=dict(gh=dict(command="node", args=[str(tools["server"])])))
    if arm.guided:
        limits["maxSkillsInPrompt"] = 0
        config["plugins"] = dict(
            load=dict(paths=[str(PLUGIN)]),
            entries={
                "relaymind-guidance": dict(
                    enabled=True,
                    hooks=dict(allowConversationAccess=True),
                    config=dict(
                        baseUrl=gateway_url,
                        **({"limit": arm.limit} if arm.limit != "hint" else {}),
                        **({"toolsFile": str(tools["file"])} if tools else {}),
                    ),
                )
            },
        )
    return config


def _toml_string(value) -> str:
    """A TOML basic string. JSON escapes are valid TOML ones, so a Windows backslash stays one."""
    return json.dumps(str(value), ensure_ascii=False)


def gateway_toml(arm, directory, *, port, local, third_party_url=None, external=None):
    q = _toml_string
    lines = [
        f'[server]\nhost = "127.0.0.1"\nport = {port}',
        f"[store]\npath = {q(f'{directory}/gateway.sqlite3')}",
        f"[sandbox]\nwork_dir = {q(f'{directory}/sandbox')}",
    ]
    if arm.keywords == "local":
        manifest, artifact, url = local
        lines.append(
            f"[local_model]\nmanifest = {q(manifest)}\nartifact = {q(artifact)}\n"
            f"base_url = {q(url)}"
        )
    base_url, model = external or ("https://unused.example.invalid/v1", "unused")
    lines.append(
        f"[external_model]\nbase_url = {q(base_url)}\nmodel = {q(model)}\n"
        "context_limit = 128000\n"
        '[external_model.request_options]\nreasoning_effort = "low"'
    )
    guidance = [
        "[guidance]",
        "enabled = true",
        f"keywords = {q(arm.keywords)}",
        f"picker = {q(arm.picker)}",
        "total_ms = 12000",
    ]
    if arm.third_party:
        guidance += [
            'primary = "http"',
            f"name = {q(arm.third_party)}",
            'version = "1"',
            f"url = {q(third_party_url)}",
            # A selector that is down must give no hint, so the preflight stops the arm instead
            # of measuring word match under the selector's name.
            "fallback = false",
        ]
    lines.append("\n".join(guidance))
    return "\n".join(lines) + "\n"


class Recorder:
    """A loopback proxy that adds the key, which the host never gets, and records each exchange."""

    def __init__(self, base_url, api_key, directory):
        import httpx

        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.answer(200, b'{"object":"list","data":[]}')

            def answer(self, status, body, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                run = self.path.split("/")[2]
                request = json.loads(self.rfile.read(int(self.headers["content-length"])))
                request["reasoning_effort"] = "low"
                request.setdefault("stream_options", {})["include_usage"] = True
                if "max_completion_tokens" in request:
                    request["max_tokens"] = request.pop("max_completion_tokens")
                text, calls, usage, status, complete = "", {}, None, None, False
                self.close_connection = True
                recorder.begin(run)
                try:
                    with httpx.stream(
                        "POST",
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=request,
                        timeout=120,
                    ) as response:
                        status = response.status_code
                        if status != 200:  # The host sees the provider's own error.
                            self.answer(
                                status,
                                response.read(),
                                response.headers.get("content-type", "application/json"),
                            )
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        broken = False
                        for line in response.iter_lines():
                            self.wfile.write((line + "\n").encode())
                            self.wfile.flush()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                complete = not broken
                                continue
                            try:
                                data = json.loads(payload)
                            except ValueError:  # Not JSON: the run shows nothing reliable.
                                broken = True
                                continue
                            if not isinstance(data, dict) or data.get("error"):
                                broken = True
                                continue
                            usage = data.get("usage") or usage
                            for choice in data.get("choices") or []:
                                delta = choice.get("delta") or {}
                                text += delta.get("content") or ""
                                if choice.get("finish_reason"):
                                    complete = not broken
                                for tool in delta.get("tool_calls") or []:
                                    entry = calls.setdefault(
                                        tool.get("index", 0), dict(name="", arguments="")
                                    )
                                    function = tool.get("function") or {}
                                    entry["id"] = tool.get("id") or entry.get("id")
                                    entry["name"] += function.get("name") or ""
                                    entry["arguments"] += function.get("arguments") or ""
                        complete = complete and not broken
                except httpx.HTTPError:
                    if status is None:  # Nothing was sent yet: the provider was not reached.
                        self.answer(502, b'{"error":"the model provider was not reached"}')
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    row = dict(
                        status=status,
                        request=request,
                        text=text,
                        calls=list(calls.values()),
                        usage=usage,
                        complete=complete,
                    )
                    try:
                        with open(recorder.directory / f"{run}.jsonl", "a", encoding="utf-8") as f:
                            f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    finally:
                        recorder.end(run)

        self.directory = Path(directory)
        self.active = {}
        self.idle = threading.Condition()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def begin(self, run):
        with self.idle:
            self.active[run] = self.active.get(run, 0) + 1

    def end(self, run):
        with self.idle:
            self.active[run] -= 1
            self.idle.notify_all()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def wait_idle(self, run, timeout=130):
        """Wait until every exchange of a run is written: the host can exit before the last one."""
        with self.idle:
            return self.idle.wait_for(lambda: not self.active.get(run), timeout)


# One folder name: letters, digits, . _ - , never .. or a separator, so a skill stays in its target.
_FOLDER = re.compile(r"^(?!\.\.?$)[^\W][\w.-]{0,127}$")


def safe_folder(name):
    return isinstance(name, str) and bool(_FOLDER.match(name))


@contextmanager
def disposable(directory, capture, *, keep):
    """Remove a run's state folder, and its capture unless kept, however the run ends."""
    try:
        yield
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        if not keep:
            Path(capture).unlink(missing_ok=True)


def run_name(arm, case_id):
    """A run's folder and capture name: one safe folder whatever the case ID holds."""
    return f"{arm}__{re.sub(r'[^\w-]+', '_', str(case_id)).strip('_')}"


def install_skills(catalog_dir, target):
    """The catalog's skills as ordinary host skills. Returns asset ID to skill name.

    The folder is emptied first, so skills of an earlier run cannot stay as extra choices."""
    shutil.rmtree(target, ignore_errors=True)
    catalog = json.loads((catalog_dir / "catalog.json").read_text("utf-8"))
    evidence = {e["id"]: e for e in catalog["evidence"]}
    names, taken = {}, set()
    for asset in catalog["assets"]:
        record = asset["record"]
        if record["kind"] != "skill":
            continue
        raw = (catalog_dir / evidence[asset["original_evidence_id"]]["path"]).read_bytes()
        if raw[:2] == b"PK":
            continue
        text = raw.decode("utf-8", "replace").removeprefix("\ufeff")
        front = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
        found = front and re.search(r"^name:[ \t]*(.+?)[ \t]*\r?$", front.group(1), re.M)
        if not found:
            continue
        name = found.group(1).strip()
        if name[:1] in ("'", '"'):  # Quoted: up to the closing quote, as the plugin reads it.
            name = name[1:].split(name[0])[0]
        else:  # Unquoted: a # after a space starts a comment.
            name = re.split(r"\s+#", name)[0].strip()
        if name in taken or not safe_folder(name):  # Taken, or not one folder of the target.
            continue
        taken.add(name)
        folder = target / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
        names[record["asset_id"]] = name
    return names


SIMILAR_DATA = ROOT / "evaluation/assets/guidance-similar/similar.v1.json"


def install_similar(split, target):
    """The families of a split as host skills, with one case per skill and language.

    The folder is emptied first, so skills of another split cannot stay as extra choices."""
    shutil.rmtree(target, ignore_errors=True)
    data = json.loads(SIMILAR_DATA.read_text("utf-8"))
    families = data["splits"][split]
    names, cases = {}, []
    for family in families:
        for skill in family["skills"]:
            name, asset = skill["name"], f"similar:{skill['name']}"
            if not safe_folder(name):
                raise ValueError(f"a similar skill name is not one folder: {name!r}")
            folder = target / name
            folder.mkdir(parents=True, exist_ok=True)
            front = f"---\nname: {name}\ndescription: {json.dumps(skill['description'])}\n---\n"
            (folder / "SKILL.md").write_text(front + "Follow the request.\n", encoding="utf-8")
            names[asset] = name
            for language in ("en", "zh"):
                cases.append(
                    dict(
                        id=f"similar-{language}-{name}",
                        lane=f"similar_{language}",
                        reference_class="positive",
                        task_text=skill[language],
                        acceptable_asset_ids=[asset],
                    )
                )
    for number, request in enumerate(data["complex"][split]):
        for language in ("en", "zh"):
            cases.append(
                dict(
                    id=f"similar-complex-{language}-{number}",
                    lane=f"similar_complex_{language}",
                    reference_class="positive",
                    task_text=request[language],
                    acceptable_asset_ids=[f"similar:{name}" for name in request["skills"]],
                )
            )
    return names, cases


FAKE_MCP = """// A stdio MCP server that lists tools and only answers "called". Nothing else.
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
const listed = JSON.parse(readFileSync(new URL("./tools.server.json", import.meta.url), "utf8"));
const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  let m;
  try { m = JSON.parse(line); } catch { return; }
  if (m.id === undefined) return;
  let result = {};
  if (m.method === "initialize") {
    result = {
      protocolVersion: m.params?.protocolVersion ?? "2025-06-18",
      capabilities: { tools: {} },
      serverInfo: { name: "gh", version: "1" },
    };
  } else if (m.method === "tools/list") {
    result = { tools: listed };
  } else if (m.method === "tools/call") {
    result = { content: [{ type: "text", text: `called ${m.params.name}` }] };
  }
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: m.id, result }) + "\\n");
});
"""


def install_tools(catalog, directory):
    """The catalog's tools behind a fake MCP server. Returns asset ID to name, and the files."""
    names, listed = {}, []
    for asset in catalog["assets"]:
        record = asset["record"]
        if record["kind"] != "mcp_tool":
            continue
        name = record["identity"]["tool_name"]
        names[record["asset_id"]] = name
        listed.append(dict(name=name, description=record["description"]))
    directory.mkdir(parents=True, exist_ok=True)
    server, plugin_list = directory / "server.mjs", directory / "tools.json"
    server.write_text(FAKE_MCP, "utf-8")
    (directory / "tools.server.json").write_text(
        json.dumps(
            [
                dict(
                    name=t["name"],
                    description=t["description"],
                    inputSchema=dict(type="object", additionalProperties=True),
                )
                for t in listed
            ]
        ),
        "utf-8",
    )
    plugin_list.write_text(
        json.dumps(
            [dict(name=f"{MCP_SERVER}__{t['name']}", description=t["description"]) for t in listed]
        ),
        "utf-8",
    )
    return names, dict(server=server, file=plugin_list)


def gateway_start_failed(log_text):
    return "address already in use" in log_text


def run_host(argv, env, *, timeout):
    """The host's exit code, or "timeout". On a timeout its whole process tree is stopped.

    The host can start children of its own, and on Windows the launcher is a .cmd shell, so
    killing only the direct child would leave a host running and spending on its model.
    """
    windows = sys.platform == "win32"
    process = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.DEVNULL,  # Only the exit code is used.
        stderr=subprocess.DEVNULL,
        start_new_session=not windows,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )
    with _LIVE_LOCK:
        _LIVE.add(process)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_tree(process)
        return "timeout"
    except BaseException:  # Ctrl-C too: the host is in its own session and would go on.
        _stop_tree(process)
        raise
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(process)


_LIVE: set = set()
_LIVE_LOCK = threading.Lock()


def _stop_tree(process):
    if sys.platform == "win32":
        if process.poll() is not None:  # taskkill finds children only through a live parent.
            return
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def stop_all_hosts():
    """Stop every host still running, as on Ctrl-C while worker threads wait on theirs."""
    with _LIVE_LOCK:
        live = list(_LIVE)
    for process in live:
        _stop_tree(process)


def stop_gateway(process):
    """Stop a gateway, and kill it if it does not stop in time, so no process holds its port."""
    if sys.platform == "win32":
        # terminate() ends only the parent there, and then the tree cannot be found.
        _stop_tree(process)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)  # The whole group, so no child keeps the port.
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        _stop_tree(process)


def start_gateway(toml_path, env):
    code = (
        "from relaymind.launcher import cli; import sys; "
        f"sys.argv=['relaymind','serve','--config',{str(toml_path)!r}]; cli()"
    )
    with open(f"{toml_path}.log", "w", encoding="utf-8") as log:
        # The child keeps its own handle; closing ours lets Windows remove the folder later.
        windows = sys.platform == "win32"
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            # Its own process group, so stopping it can take its children too.
            start_new_session=not windows,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
        )
    try:
        for _ in range(600):
            time.sleep(0.25)
            text = Path(f"{toml_path}.log").read_text(encoding="utf-8")
            if "Serving on" in text:
                # The server prints this before it binds, and another process may hold the port.
                time.sleep(2)
                text = Path(f"{toml_path}.log").read_text(encoding="utf-8")
                if gateway_start_failed(text) or process.poll() is not None:
                    break
                found = re.search(r"(?im)token[^\n]*?[:\s]([A-Za-z0-9_\-\.]{20,})", text)
                if not found:
                    break
                return process, found.group(1)
            if process.poll() is not None:
                break
    except BaseException:  # Also an interrupt: the caller has no process to stop yet.
        stop_gateway(process)
        raise
    stop_gateway(process)
    raise RuntimeError(f"the gateway did not start; see {toml_path}.log")


def read_jsonl(path):
    """The rows of a JSON-lines file, or none if it is missing. The file is closed at once."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize(runs):
    """Per arm. A run whose last model call failed shows nothing about selection, so it is counted
    apart and left out of every other number."""
    summary = {}
    for name in sorted({r["arm"] for r in runs}):
        every = [r for r in runs if r["arm"] == name]
        group = [r for r in every if not r.get("upstream_failed")]
        firsts = [r["first_input"] for r in group if r["first_input"]]
        # A run of unknown size is counted apart; it is never a smaller measurement.
        totals = [r["total_input"] for r in group if r["total_input"] is not None]
        lanes = {}
        for r in group:
            lane = lanes.setdefault(r["lane"], dict(right=0, runs=0))
            lane["runs"] += 1
            lane["right"] += bool(r["correct"])
        summary[name] = dict(
            runs=len(group),
            failed_runs=sum(r["exit"] != 0 for r in group),
            right_skill=sum(bool(r["correct"]) for r in group),
            any_skill=sum(bool(r["any_skill"]) for r in group),
            searched=sum(r["searched"] > 0 for r in group),
            median_first_input=statistics.median(firsts) if firsts else None,
            median_total_input=statistics.median(totals) if totals else None,
            sum_total_input=sum(totals),
            unknown_input=len(group) - len(totals),
            # The gateway's own model calls go to the provider directly; the picker's tokens
            # are not in these sizes.
            gateway_model_tokens="not included",
            median_wall_s=statistics.median(r["wall_s"] for r in group) if group else None,
            hint_seen=sum(bool(r["hint_seen"]) for r in group),
            hint_right=sum(bool(r.get("hint_right")) for r in group),
            refused=sum(r.get("refused", 0) for r in group),
            by_lane=lanes,
            upstream_failed=len(every) - len(group),
        )
    return summary


def host_env(home, state, token):
    """The host's environment: its own home and state, and never the model key."""
    env = {
        **os.environ,
        # Node reads USERPROFILE on Windows and HOME elsewhere; both point at the sandbox.
        "HOME": str(home),
        "USERPROFILE": str(home),
        "OPENCLAW_STATE_DIR": str(state),
        "RELAYMIND_APP_TOKEN": token or "",
    }
    env.pop("RELAYMIND_EXTERNAL_API_KEY", None)
    return env


def default_lanes(*, similar):
    return (
        "similar_en,similar_zh"
        if similar
        else "en_to_en,zh_hans_to_en,zh_hans_to_zh_hans,en_to_zh_hans"
    )


def setup_problem(args) -> str | None:
    """A missing input, found before any arm spends on a model."""
    if args.similar and (args.catalog_dir or args.cases):
        return "--similar brings its own skills and cases: drop --catalog-dir and --cases"
    if not os.environ.get("RELAYMIND_EXTERNAL_API_KEY"):
        return "set RELAYMIND_EXTERNAL_API_KEY: the host's model is called with it"
    if args.tools and not args.catalog_dir:
        return "--tools needs --catalog-dir"
    if not args.similar and not (args.catalog_dir and args.cases):
        return "give --similar, or --catalog-dir with --cases"
    for name in args.arms.split(","):
        if name not in ARMS:
            return f"no arm named {name}"
        arm = ARMS[name]
        if arm.third_party and not args.third_party_url:
            return f"arm {name} needs --third-party-url"
        if arm.keywords == "local" and not (args.local_manifest and args.local_artifact):
            return f"arm {name} needs --local-manifest and --local-artifact"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog-dir", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument(
        "--similar", choices=("development", "holdout"), help="Test the similar skills of a split."
    )
    parser.add_argument(
        "--tools", action="store_true", help="Test the prompts for MCP tools, over a fake server."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-lane", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261105)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--model-base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--local-manifest")
    parser.add_argument("--local-artifact")
    parser.add_argument("--local-url", default="http://127.0.0.1:8033")
    parser.add_argument("--third-party-url")
    parser.add_argument("--keep-captures", action="store_true", help="Keep raw exchanges for debug")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument(
        "--lanes", help="Default: the similar lanes with --similar, else the four matrix lanes."
    )
    args = parser.parse_args()
    args.lanes = args.lanes or default_lanes(similar=bool(args.similar))
    problem = setup_problem(args)
    if problem:
        parser.error(problem)
    key = os.environ["RELAYMIND_EXTERNAL_API_KEY"]
    output = args.output.resolve()
    (output / "caps").mkdir(parents=True, exist_ok=True)
    workspace = output / "workspace"
    skills = {}
    if args.catalog_dir:
        skills = install_skills(args.catalog_dir, workspace / "skills")
    cases, tools = [], None
    if args.cases:
        cases = json.loads(args.cases.read_text("utf-8"))["cases"]
    wanted = skills
    if args.similar:
        names, cases = install_similar(args.similar, workspace / "skills")
        skills = {**skills, **names}
        wanted = names
    if args.tools:
        catalog = json.loads((args.catalog_dir / "catalog.json").read_text("utf-8"))
        names, tools = install_tools(catalog, output / "mcp")
        skills = {**skills, **names}
        wanted = names
    sample = sample_cases(
        cases, wanted, per_lane=args.per_lane, seed=args.seed, lanes=args.lanes.split(",")
    )
    (output / "sample.json").write_text(json.dumps([c["id"] for c in sample]), "utf-8")
    print(f"installed {len(skills)} skills, {len(sample)} prompts", flush=True)
    recorder = Recorder(args.model_base_url, key, output / "caps")
    results = output / "runs.jsonl"
    results.write_text("", "utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    local = (args.local_manifest, args.local_artifact, args.local_url)

    def one(arm, case, *, gateway_url, token):
        run = run_name(arm.name, case["id"])
        directory = output / "state" / run
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        capture = output / "caps" / f"{run}.jsonl"
        capture.unlink(missing_ok=True)
        config = openclaw_config(
            arm, f"http://127.0.0.1:{recorder.port}/run/{run}/v1", args.model, gateway_url, tools
        )
        (directory / "c.json").write_text(json.dumps(config), "utf-8")
        # The run's state and capture go even when grading fails: a capture holds the whole
        # prompt and every model answer, not only the metrics.
        with disposable(directory, capture, keep=args.keep_captures):
            child = host_env(output / "home", directory, token)
            started = time.time()
            exit_code = run_host(
                [
                    str(OPENCLAW),
                    "agent",
                    "exec",
                    "--config",
                    str(directory / "c.json"),
                    "--state-dir",
                    str(directory),
                    "--cwd",
                    str(workspace),
                    "--model",
                    f"glm/{args.model}",
                    "--json",
                    "--timeout",
                    "150",
                    case["task_text"],
                ],
                child,
                timeout=200,
            )
            recorder.wait_idle(run)
            rows = read_jsonl(capture)
            acceptable = {skills[a] for a in case["acceptable_asset_ids"] if a in skills}
            outcome = dict(
                arm=arm.name,
                case=case["id"],
                lane=case["lane"],
                exit=exit_code,
                wall_s=round(time.time() - started, 1),
                **grade(rows, acceptable),
            )
            return outcome

    (output / "home").mkdir(exist_ok=True)
    for index, name in enumerate(args.arms.split(",")):
        arm = ARMS[name]
        gateway, gateway_url, token = None, None, None
        if arm.guided:
            arm_dir = output / "gateway" / name
            shutil.rmtree(arm_dir, ignore_errors=True)
            arm_dir.mkdir(parents=True)
            port = 18970 + index
            toml = arm_dir / "gateway.toml"
            toml.write_text(
                gateway_toml(
                    arm,
                    arm_dir,
                    port=port,
                    local=local,
                    third_party_url=args.third_party_url,
                    external=(args.model_base_url, args.model),
                ),
                "utf-8",
            )
            gateway, token = start_gateway(toml, env)
            gateway_url = f"http://127.0.0.1:{port}/v1"
        try:
            checked = preflight_cases(sample)
            any_hint = False
            for case in checked:
                first = one(arm, case, gateway_url=gateway_url, token=token)
                check_applied(arm, first)
                any_hint = any_hint or bool(first["hint_seen"])
                with results.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(first, ensure_ascii=False) + "\n")
                print(
                    f"{name}: applied on {case['id']}, listed {first['listed']}, "
                    f"first input {first['first_input']}",
                    flush=True,
                )
            rest = [c for c in sample if c not in checked]
            # Better selection can rightly give a prompt no hint, but not several in a row.
            tried = 0
            while arm.guided and not any_hint and rest and tried < PREFLIGHT_EXTRA:
                case = rest.pop(0)
                tried += 1
                first = one(arm, case, gateway_url=gateway_url, token=token)
                check_applied(arm, first)
                any_hint = bool(first["hint_seen"])
                with results.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(first, ensure_ascii=False) + "\n")
            if arm.guided and not any_hint:
                raise RuntimeError(f"{name}: no prompt got a hint, so guidance did not apply")
            with ThreadPoolExecutor(args.workers) as pool:
                try:
                    for outcome in pool.map(
                        partial(one, arm, gateway_url=gateway_url, token=token), rest
                    ):
                        with results.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(outcome, ensure_ascii=False) + "\n")
                except BaseException:  # Ctrl-C: stop queued cases and the hosts that run.
                    pool.shutdown(wait=False, cancel_futures=True)
                    stop_all_hosts()
                    raise
        finally:
            if gateway is not None:
                stop_gateway(gateway)
    runs = read_jsonl(results)
    summary = json.dumps(summarize(runs), indent=1)
    (output / "summary.json").write_text(summary, "utf-8")
    print(summary)


if __name__ == "__main__":
    main()
