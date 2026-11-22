"""Run the real asset-router next to the real guidance gateway (RM47, AC42; spec 0.72 section 7.7).

Used by tests/test_guidance_router_interop.py and scripts/asset_router_interop.py. The router is an
explicit local checkout (RELAYMIND_ASSET_ROUTER_DIR) started through its own production entry
point, src/apps/server/main.ts, in fed mode with its own SQLite store. Only inference is a double:
a deterministic OpenAI-shaped embeddings endpoint on loopback. A pass-through proxy between the
gateway and the router records every exchange, so a test can show what each side sent.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROUTER_DIR_ENV = "RELAYMIND_ASSET_ROUTER_DIR"
ROUTER_REV_ENV = "RELAYMIND_ASSET_ROUTER_REV"
ROUTER_NAME = "asset-router"
ROUTER_VERSION = "1"
DIMENSIONS = 64
_TSX_LOADER = Path("node_modules/tsx/dist/loader.mjs")
_WORD = re.compile(r"[a-z0-9]+")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def poll(check: Callable[[], object], timeout: float, message: Callable[[], str] | str):
    """Call `check` until it returns something truthy, and return that; fail after `timeout` s."""
    deadline = time.monotonic() + timeout
    while not (result := check()):
        if time.monotonic() > deadline:
            pytest.fail(message() if callable(message) else message)
        time.sleep(0.1)
    return result


def require_checkout() -> Path:
    """The router checkout named by RELAYMIND_ASSET_ROUTER_DIR, or a skip without one.

    scripts/asset_router_interop.py fails a run with any skip, so a skip never passes there."""
    value = os.environ.get(ROUTER_DIR_ENV)
    if not value:
        pytest.skip(f"set {ROUTER_DIR_ENV} to an asset-router checkout with node_modules")
    path = Path(value).resolve()
    if not (path / _TSX_LOADER).is_file():
        pytest.skip(f"{path} has no installed dependencies (run npm ci there)")
    return path


def router_revision(checkout: Path) -> str:
    """The exact commit of the router checkout, refused when its tracked files changed."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError(f"router checkout {checkout} has local changes; use a clean revision")
    return git("rev-parse", "HEAD")


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status: int, body: bytes = b"", content_type: str = "application/json"):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Loopback:
    """A threaded loopback HTTP server owned by a test double."""

    _server: _Server | None = None

    def _serve(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self._server = _Server(("127.0.0.1", 0), handler)
        # A short poll interval keeps shutdown() quick: the default waits up to 0.5 s.
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()

    @property
    def url(self) -> str:
        assert self._server is not None, "not started"
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


@dataclass
class EmbeddingDouble(_Loopback):
    """Deterministic bag-of-words vectors. Inference is the only part that is not production code.

    `fail_marker`: an input containing it gets HTTP 500, as a failing embedding endpoint would.
    `seed`: changing it changes every vector, as a new embedding model would.
    """

    fail_marker: str | None = None
    seed: str = "v1"
    calls: int = 0

    def vector(self, text: str) -> list[float]:
        vec = [0.0] * DIMENSIONS
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(f"{self.seed}:{word}".encode()).digest()
            vec[digest[0] % DIMENSIONS] += 1.0 if digest[1] % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec] if any(vec) else [1.0] + [0.0] * (DIMENSIONS - 1)

    def start(self) -> EmbeddingDouble:
        double = self

        class Handler(_QuietHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                double.calls += 1
                text = body.get("input", "")
                if double.fail_marker and double.fail_marker in text:
                    return self.reply(500)
                answer = {"data": [{"embedding": double.vector(text)}], "model": body.get("model")}
                self.reply(200, json.dumps(answer).encode())

        self._serve(Handler)
        return self


@dataclass
class Exchange:
    path: str
    request: dict
    status: int
    response: object


@dataclass
class RecordingProxy(_Loopback):
    """Forwards POST /select and /sync to the router unchanged and records both sides."""

    target: str = ""
    exchanges: list[Exchange] = field(default_factory=list)
    _client: httpx.Client | None = None

    def start(self) -> RecordingProxy:
        proxy = self
        # One client for every forwarded request; httpx clients are thread-safe.
        self._client = httpx.Client(timeout=30)

        class Handler(_QuietHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                answer = proxy._client.post(
                    proxy.target + self.path,
                    content=raw,
                    headers={"Content-Type": self.headers.get("Content-Type", "")},
                )
                try:
                    decoded: object = answer.json()
                except ValueError:
                    decoded = answer.text
                proxy.exchanges.append(
                    Exchange(self.path, json.loads(raw), answer.status_code, decoded)
                )
                self.reply(
                    answer.status_code, answer.content, answer.headers.get("content-type", "")
                )

        self._serve(Handler)
        return self

    def stop(self) -> None:
        super().stop()
        if self._client:
            self._client.close()
            self._client = None

    def of(self, path: str, operation: str | None = None) -> list[Exchange]:
        return [
            e
            for e in self.exchanges
            if e.path == path and (operation is None or e.request.get("operation") == operation)
        ]


@dataclass
class RouterProcess:
    """The router's production server, src/apps/server/main.ts, in fed mode."""

    checkout: Path
    workdir: Path
    embed_url: str
    roots: list[Path]
    # The router's embedding recipe; a change is a new feature recipe for the same sources.
    embedding_source: str = "full"
    port: int = 0
    process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def write_config(self) -> None:
        # main.ts reads config/default.json and config/local.json from its working directory.
        config = {
            "logger": {"level": "warn"},
            "skills": {"source": "relaymind"},
            "store": {"filename": str(self.workdir / "store" / "index.db")},
            "embeddings": {
                "baseUrl": self.embed_url,
                "model": "interop-double",
                "dimensions": DIMENSIONS,
                "timeoutMs": 5000,
            },
            "server": {
                "host": "127.0.0.1",
                "port": self.port,
                "name": ROUTER_NAME,
                "version": ROUTER_VERSION,
            },
            "router": {"embeddingSource": self.embedding_source, "recallUnmatched": True},
            "sync": {"allowedRoots": [str(r) for r in self.roots]},
        }
        (self.workdir / "config").mkdir(parents=True, exist_ok=True)
        (self.workdir / "config" / "local.json").write_text(json.dumps(config, indent=2))

    def start(self, timeout: float = 60.0) -> RouterProcess:
        self.port = self.port or free_port()
        self.write_config()
        log_path = self.workdir / "router.log"
        loader = (self.checkout / _TSX_LOADER).as_uri()
        entry = self.checkout / "src" / "apps" / "server" / "main.ts"
        # The child keeps its own copy of the log descriptor, so ours closes at once.
        with open(log_path, "ab") as log:
            self.process = subprocess.Popen(
                ["node", "--import", loader, str(entry)],
                cwd=self.workdir,
                stdout=log,
                stderr=log,
                env={**os.environ, "NO_COLOR": "1"},
            )

        def listening() -> bool:
            if self.process.poll() is not None:
                pytest.fail(f"router exited early; see {log_path}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return True
            except OSError:
                return False

        poll(listening, timeout, f"router did not listen within {timeout} s; see {log_path}")
        return self

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)
        self.process = None

    def restart(self, **changes) -> None:
        """Stop, change settings such as `embedding_source`, and start on the same store."""
        self.stop()
        for name, value in changes.items():
            setattr(self, name, value)
        self.start()


def skill_file(root: Path, name: str, description: str, body: str) -> dict:
    """A real SKILL.md, and the asset the guidance plugin sends for it (spec 0.72)."""
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", "utf-8")
    data = path.read_bytes()
    return dict(
        name=name,
        kind="skill",
        description=description,
        content_hash=hashlib.sha256(data).hexdigest(),
        path=str(path),
        size_bytes=len(data),
    )
