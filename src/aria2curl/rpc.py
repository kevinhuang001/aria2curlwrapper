"""A tiny, dependency-free aria2 JSON-RPC client.

Only the handful of methods the supervisor needs are implemented.  The client
keeps one HTTP/1.1 connection alive (aria2 answers up to ~3 requests per poll
tick, so reconnecting every time would be wasteful) and reconnects lazily.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

RPC_TIMEOUT = 4.0


class Aria2RpcError(Exception):
    """Transport or protocol level failure while talking to aria2."""


@dataclass
class DownloadStatus:
    """A flattened view of one aria2 download."""

    gid: str
    status: str
    total: int
    completed: int
    speed: int
    connections: int
    error_code: int
    error_message: str
    path: str | None
    uris: tuple[str, ...]
    dir: str

    @property
    def finished(self) -> bool:
        return self.status in ("complete", "error", "removed")

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.completed / self.total)

    @property
    def eta(self) -> float | None:
        if self.total <= 0 or self.speed <= 0 or self.completed >= self.total:
            return None
        return (self.total - self.completed) / self.speed

    @property
    def name(self) -> str:
        if self.path:
            return self.path.rsplit("/", 1)[-1]
        if self.uris:
            tail = self.uris[0].rstrip("/").rsplit("/", 1)[-1]
            return tail or self.uris[0]
        return self.gid


def _int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def parse_status(entry: dict[str, Any]) -> DownloadStatus:
    files = entry.get("files") or []
    path = None
    if files and isinstance(files[0], dict):
        path = files[0].get("path") or None
    uris: list[str] = []
    for file_entry in files:
        for uri in file_entry.get("uris") or []:
            value = uri.get("uri")
            if value and value not in uris:
                uris.append(value)
    return DownloadStatus(
        gid=str(entry.get("gid", "")),
        status=str(entry.get("status", "unknown")),
        total=_int(entry.get("totalLength")),
        completed=_int(entry.get("completedLength")),
        speed=_int(entry.get("downloadSpeed")),
        connections=_int(entry.get("connections")),
        error_code=_int(entry.get("errorCode")),
        error_message=str(entry.get("errorMessage") or ""),
        path=path,
        uris=tuple(uris),
        dir=str(entry.get("dir") or ""),
    )


class Aria2Rpc:
    """Minimal JSON-RPC client bound to one aria2c instance."""

    def __init__(
        self,
        port: int,
        secret: str,
        host: str = "127.0.0.1",
        timeout: float = RPC_TIMEOUT,
    ) -> None:
        self.port = port
        self.secret = secret
        self.host = host
        self.timeout = timeout
        self._conn: http.client.HTTPConnection | None = None
        self._counter = 0

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        conn.connect()
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def call(self, method: str, params: Sequence[Any] = ()) -> Any:
        self._counter += 1
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": f"aria2curl-{self._counter}",
                "method": method,
                "params": [f"token:{self.secret}", *params],
            }
        ).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if self._conn is None:
                    self._conn = self._connect()
                self._conn.request(
                    "POST",
                    "/jsonrpc",
                    body=payload,
                    headers={"Content-Type": "application/json"},
                )
                response = self._conn.getresponse()
                raw = response.read()
                if response.status != 200:
                    raise Aria2RpcError(f"aria2 RPC returned HTTP {response.status}")
                decoded = json.loads(raw.decode("utf-8"))
                if "error" in decoded:
                    error = decoded["error"]
                    raise Aria2RpcError(f"{method} failed: {error.get('message', error)}")
                return decoded.get("result")
            except Aria2RpcError:
                raise
            except (OSError, http.client.HTTPException, json.JSONDecodeError, socket.timeout) as exc:
                last_error = exc
                self.close()
                if attempt == 1:
                    break
                time.sleep(0.05)
        raise Aria2RpcError(f"{method} failed: {last_error}")

    # -------------------------------------------------------------------- queries
    def version(self) -> dict[str, Any]:
        return self.call("aria2.getVersion") or {}

    def is_up(self) -> bool:
        try:
            self.version()
        except Aria2RpcError:
            return False
        return True

    def wait_until_ready(self, timeout: float, *, is_alive=None, interval: float = 0.1) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.is_up():
                return True
            if is_alive is not None and not is_alive():
                return False
            time.sleep(interval)
        return self.is_up()

    def tell_active(self) -> list[dict[str, Any]]:
        return list(self.call("aria2.tellActive") or [])

    def tell_waiting(self) -> list[dict[str, Any]]:
        return list(self.call("aria2.tellWaiting", (0, 1000)) or [])

    def tell_stopped(self) -> list[dict[str, Any]]:
        return list(self.call("aria2.tellStopped", (0, 1000)) or [])

    def snapshot(self) -> list[DownloadStatus]:
        """Active + waiting + stopped downloads, as flat statuses."""
        statuses: list[DownloadStatus] = []
        for entry in self.tell_active():
            statuses.append(parse_status(entry))
        for entry in self.tell_waiting():
            statuses.append(parse_status(entry))
        for entry in self.tell_stopped():
            statuses.append(parse_status(entry))
        return statuses

    def global_stat(self) -> dict[str, Any]:
        return self.call("aria2.getGlobalStat") or {}

    # ------------------------------------------------------------------- commands
    def remove(self, gid: str) -> str:
        return str(self.call("aria2.remove", (gid,)))

    def shutdown(self) -> None:
        self.call("aria2.shutdown")

    def force_shutdown(self) -> None:
        self.call("aria2.forceShutdown")


def dedupe(statuses: Iterable[DownloadStatus]) -> list[DownloadStatus]:
    """Keep the most advanced record per gid (active > waiting > stopped)."""
    order = {"active": 0, "waiting": 1, "paused": 2, "complete": 3, "error": 3, "removed": 3}
    best: dict[str, DownloadStatus] = {}
    for status in statuses:
        current = best.get(status.gid)
        if current is None or order.get(status.status, 9) < order.get(current.status, 9):
            best[status.gid] = status
    return list(best.values())
