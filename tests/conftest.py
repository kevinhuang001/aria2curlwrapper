"""Shared fixtures: an isolated environment and a Range-capable HTTP server.

Python's ``http.server`` ignores ``Range`` requests, which would make every
resume test meaningless, so the test-suite ships a small server that implements
byte ranges, throttling, redirects, Content-Disposition, gzip, basic auth and a
request log the tests can assert on.
"""

from __future__ import annotations

import gzip
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


# --------------------------------------------------------------------- payloads


def _blob(size: int, seed: int) -> bytes:
    """Deterministic, cheap, position sensitive test payload.

    Every 64 KiB block carries its own index in its first bytes, so a payload
    shifted by an aria2 piece (1 MiB) cannot compare equal by accident.
    """
    base = bytes(((i * seed + 7) % 251) for i in range(1 << 16))
    out = bytearray()
    index = 0
    while len(out) < size:
        chunk = bytearray(base)
        chunk[0] = index & 0xFF
        chunk[1] = (index >> 8) & 0xFF
        chunk[2] = (index * 31) & 0xFF
        out += chunk
        index += 1
    return bytes(out[:size])


FILE_A = _blob(16 * 1024, 3)
FILE_B = _blob(64 * 1024, 7)
# Larger than several aria2 pieces: aria2 resumes on 1 MiB piece boundaries, so
# resume tests need a payload bigger than one piece to be meaningful.
FILE_BIG = _blob(4 * 1024 * 1024, 13)
PIECE = 1024 * 1024


@dataclass
class Request:
    method: str
    path: str
    range_header: str | None
    headers: dict[str, str]
    body: bytes = b""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "aria2curl-test"

    # ------------------------------------------------------------- infra
    def log_message(self, *_args: Any) -> None:  # keep pytest output clean
        pass

    def _record(self, body: bytes = b"") -> None:
        self.server.requests.append(
            Request(
                method=self.command,
                path=self.path,
                range_header=self.headers.get("Range"),
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
            )
        )

    def _send(self, code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        try:
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            if self.command == "HEAD":
                body = b""
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # aria2 aborts its initial probe request on purpose; not an error.
            self.close_connection = True

    # ------------------------------------------------------------ routing
    def do_HEAD(self) -> None:
        self._serve()

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)
        if urlsplit(self.path).path == "/echo":
            payload = json.dumps(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": body.decode("utf-8", "replace"),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            ).encode()
            self._send(200, payload, {"Content-Type": "application/json"})
        else:
            self._send(405)

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length) if length else None
        self._record()
        self._send(405)

    # ------------------------------------------------------------ handlers
    def _serve(self) -> None:
        self._record()
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)

        if path == "/nope" or path.startswith("/nope"):
            self._send(404, b"not found")
            return
        if path == "/redirect":
            target = query.get("to", ["/files/a.bin"])[0]
            self._send(302, b"", {"Location": target})
            return
        if path == "/loop":
            self._send(302, b"", {"Location": "/loop"})
            return
        if path == "/norange":
            # Deliberately ignores Range: exercises "server cannot resume".
            self._send(200, FILE_A, {"Accept-Ranges": "none", "Content-Type": "application/octet-stream"})
            return
        if path == "/disposition":
            self._send(
                200,
                FILE_A,
                {
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="from-header.bin"',
                },
            )
            return
        if path == "/gzip":
            payload = gzip.compress(FILE_A)
            self._send(200, payload, {"Content-Type": "application/octet-stream", "Content-Encoding": "gzip"})
            return
        if path == "/auth":
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                self._send(401, b"auth required", {"WWW-Authenticate": 'Basic realm="test"'})
                return
            self._send(200, FILE_A)
            return
        if path == "/echo":
            payload = json.dumps(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            ).encode()
            self._send(200, payload, {"Content-Type": "application/json"})
            return
        if path == "/slow":
            rate = int(query.get("rate", ["65536"])[0])
            total = int(query.get("size", [str(len(FILE_BIG))])[0])
            chunk = max(1024, rate // 20)
            self.send_response(200)
            self.send_header("Content-Length", str(total))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            sent = 0
            try:
                while sent < total:
                    piece = FILE_BIG[sent % len(FILE_BIG) : (sent % len(FILE_BIG)) + chunk]
                    if not piece:
                        piece = FILE_BIG[:chunk]
                    piece = piece[: total - sent]
                    self.wfile.write(piece)
                    self.wfile.flush()
                    sent += len(piece)
                    time.sleep(len(piece) / rate)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        name = unquote(path.rsplit("/", 1)[-1])
        files = {"a.bin": FILE_A, "b.bin": FILE_B, "big.bin": FILE_BIG}
        if name not in files:
            self._send(404, b"not found")
            return
        payload = files[name]
        self._send_file(payload, name)

    def _send_file(self, payload: bytes, name: str) -> None:
        total = len(payload)
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if not match:
                self._send(416, b"", {"Content-Range": f"bytes */{total}"})
                return
            start_text, end_text = match.groups()
            if start_text == "" and end_text == "":
                self._send(416, b"", {"Content-Range": f"bytes */{total}"})
                return
            if start_text == "":
                length = int(end_text)
                start = max(0, total - length)
                end = total - 1
            else:
                start = int(start_text)
                end = int(end_text) if end_text else total - 1
                end = min(end, total - 1)
            if start >= total:
                self._send(416, b"", {"Content-Range": f"bytes */{total}"})
                return
            body = payload[start : end + 1]
            self._send(
                206,
                body,
                {
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Accept-Ranges": "bytes",
                    "Content-Type": "application/octet-stream",
                },
            )
            return
        self._send(
            200,
            payload,
            {
                "Accept-Ranges": "bytes",
                "Content-Type": "application/octet-stream",
                "ETag": f'"{name}-{total}"',
            },
        )


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.requests: list[Request] = []

    def handle_error(self, request, client_address) -> None:
        """Silence the noisy traceback for clients that hang up mid-response."""
        import sys as _sys

        exc = _sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}"


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def http_server() -> Iterable[Server]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = Server(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def requests_log(http_server: Server) -> list[Request]:
    http_server.requests.clear()
    return http_server.requests


@pytest.fixture(scope="session")
def aria2_binary() -> str:
    """Path to a usable aria2c, or skip the E2E tests."""
    candidates = []
    if os.environ.get("ARIA2CURL_TEST_ARIA2"):
        candidates.append(os.environ["ARIA2CURL_TEST_ARIA2"])
    candidates.append(str(REPO_ROOT / "tools" / "aria2c"))
    found = shutil.which("aria2c")
    if found:
        candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    pytest.skip("aria2c is not available; install aria2 or run tools/fetch-aria2.sh")


@pytest.fixture()
def curl_binary() -> str:
    found = shutil.which("curl")
    if not found:
        pytest.skip("curl is not available")
    return found


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throw-away HOME with an isolated config file."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.delenv("ARIA2CURL_CONFIG", raising=False)
    return fake_home


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config" / "config.toml"
    monkeypatch.setenv("ARIA2CURL_CONFIG", str(path))
    return path


def clean_env(**overrides: str) -> dict[str, str]:
    """A subprocess environment with no inherited proxy/no_proxy surprises."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy")
    }
    env.pop("ARIA2CURL_CONFIG", None)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_cli(
    args: Iterable[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 180,
    cwd: Path | str | None = None,
) -> CliResult:
    """Run the CLI in a subprocess (needed because fallback uses execv)."""
    argv = [sys.executable, "-m", "aria2curl", *[str(a) for a in args]]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env or clean_env(),
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
    )
    return CliResult(proc.returncode, proc.stdout, proc.stderr, argv)
