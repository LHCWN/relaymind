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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROUTER_DIR_ENV = "RELAYMIND_ASSET_ROUTER_DIR"
ROUTER_REV_ENV = "RELAYMIND_ASSET_ROUTER_REV"
ROUTER_NAME = "asset-router"
ROUTER_VERSION = "1"
DIMENSIONS = 64
_WORD = re.compile(r"[a-z0-9]+")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def router_revision(checkout: Path) -> str:
    """The exact commit of the router checkout, refused when its tracked files changed."""
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError(f"router checkout {checkout} has local changes; use a clean revision")
    return head


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[_Server, str]:
    server = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@dataclass
class EmbeddingDouble:
    """Deterministic bag-of-words vectors. Inference is the only part that is not production code.

    `fail_marker`: an input containing it gets HTTP 500, as a failing embedding endpoint would.
    `seed`: changing it changes every vector, as a new embedding model would.
    """

    fail_marker: str | None = None
    seed: str = "v1"
    calls: int = 0
    url: str = ""
    _server: _Server | None = None

    def vector(self, text: str) -> list[float]:
        vec = [0.0] * DIMENSIONS
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(f"{self.seed}:{word}".encode()).digest()
            vec[digest[0] % DIMENSIONS] += 1.0 if digest[1] % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec] if any(vec) else [1.0] + [0.0] * (DIMENSIONS - 1)

    def start(self) -> EmbeddingDouble:
        double = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                double.calls += 1
                text = body.get("input", "")
                if double.fail_marker and double.fail_marker in text:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = json.dumps(
                    {"data": [{"embedding": double.vector(text)}], "model": body.get("model")}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server, self.url = _serve(Handler)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


@dataclass
class Exchange:
    path: str
    request: dict
    status: int
    response: object


@dataclass
class RecordingProxy:
    """Forwards POST /select and /sync to the router unchanged and records both sides."""

    target: str
    exchanges: list[Exchange] = field(default_factory=list)
    url: str = ""
    _server: _Server | None = None

    def start(self) -> RecordingProxy:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                answer = httpx.post(
                    proxy.target + self.path,
                    content=raw,
                    headers={"Content-Type": self.headers.get("Content-Type", "")},
                    timeout=30,
                )
                try:
                    decoded: object = answer.json()
                except ValueError:
                    decoded = answer.text
                proxy.exchanges.append(
                    Exchange(self.path, json.loads(raw), answer.status_code, decoded)
                )
                self.send_response(answer.status_code)
                self.send_header("Content-Type", answer.headers.get("content-type", ""))
                self.send_header("Content-Length", str(len(answer.content)))
                self.end_headers()
                self.wfile.write(answer.content)

        self._server, self.url = _serve(Handler)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

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
        log = open(self.workdir / "router.log", "ab")  # noqa: SIM115 - closed with the process
        tsx = self.checkout / "node_modules" / ".bin" / "tsx"
        self.process = subprocess.Popen(
            [str(tsx), str(self.checkout / "src" / "apps" / "server" / "main.ts")],
            cwd=self.workdir,
            stdout=log,
            stderr=log,
            env={**os.environ, "NO_COLOR": "1"},
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"router exited early; see {self.workdir / 'router.log'}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"router did not listen within {timeout} s")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)
        self.process = None

    def log(self) -> str:
        path = self.workdir / "router.log"
        return path.read_text(errors="replace") if path.exists() else ""


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
