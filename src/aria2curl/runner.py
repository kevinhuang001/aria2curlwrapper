"""Supervise one aria2c process and report progress.

The runner owns the process lifecycle:

1. pick a free local port and a random RPC secret,
2. start ``aria2c`` in its own session with RPC enabled and its console output
   silenced (unless ``keep_aria2_output``),
3. poll the RPC endpoint to feed the display, enforcing ``--max-time`` itself
   because aria2 has no equivalent of curl's total-time budget,
4. shut aria2 down cleanly once every download reached a terminal state,
5. translate aria2's result into a curl-shaped exit code.

If RPC turns out to be unavailable the runner degrades to simply inheriting
aria2's console output rather than losing the download.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field as dataclass_field
from typing import Sequence, TextIO

from .aria2args import Aria2Plan, ensure_directories
from .config import Config
from .display import Display, Frame, Row, Summary, make_display
from .errors import Aria2StartupError
from .exitcodes import (
    CURL_INTERRUPTED,
    CURL_OK,
    CURL_OPERATION_TIMEDOUT,
    CURL_TERMINATED,
    aria2_to_curl,
    curl_status_name,
)
from .rpc import Aria2Rpc, Aria2RpcError, DownloadStatus, dedupe

# curl exit code for "bad download resume" (aria2 status 8).
CURL_BAD_DOWNLOAD_RESUME = 36


@dataclass
class RunResult:
    exit_code: int
    message: str
    rows: list[Row] = dataclass_field(default_factory=list)
    elapsed: float = 0.0
    used_rpc: bool = True
    report: bool = True


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _status_to_row(status: DownloadStatus) -> Row:
    return Row(
        name=status.name,
        status=status.status,
        completed=status.completed,
        total=status.total,
        speed=status.speed,
        connections=status.connections,
        error_code=status.error_code,
        error_message=status.error_message,
        detail=status.path or "",
    )


def _severity(code: int) -> int:
    """Rank curl exit codes so the most meaningful one wins."""
    if code == CURL_OK:
        return 0
    if code == CURL_OPERATION_TIMEDOUT:
        return 3
    return 2


class Runner:
    """Run an :class:`Aria2Plan`, rendering progress as it goes."""

    def __init__(
        self,
        cfg: Config,
        plan: Aria2Plan,
        *,
        display: Display | None = None,
        stream: TextIO | None = None,
        quiet: bool = False,
        is_tty: bool | None = None,
    ) -> None:
        self.cfg = cfg
        self.plan = plan
        self.stream = stream if stream is not None else sys.stderr
        self.display = display or make_display(cfg, stream=self.stream, is_tty=is_tty, quiet=quiet)
        self.aria2_path = shutil.which(cfg["aria2_path"]) or ""
        if not self.aria2_path:
            raise Aria2StartupError(f"aria2c not found (looked for {cfg['aria2_path']!r})")
        self._signal: int | None = None
        self._proc: subprocess.Popen | None = None
        self._input_file: str | None = None
        self._log = tempfile.TemporaryFile(mode="w+b")
        self._timed_out: set[str] = set()
        self._started_at: dict[str, float] = {}

    # ------------------------------------------------------------------ plumbing
    def _rpc_args(self, port: int, secret: str) -> list[str]:
        args = [
            "--no-conf=true",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={port}",
            f"--rpc-secret={secret}",
            "--rpc-allow-origin-all=false",
            "--quiet=true",
            f"--console-log-level={self.cfg['console_log_level']}",
            "--summary-interval=0",
            "--show-console-readout=false",
            "--download-result=hide",
            "--enable-color=false",
        ]
        if self.cfg["stop_with_process"]:
            args.append(f"--stop-with-process={os.getpid()}")
        return args

    def _write_input_file(self) -> str | None:
        if self.plan.input_file_text is None:
            return None
        handle = tempfile.NamedTemporaryFile("w", prefix="aria2curl-", suffix=".txt", delete=False, encoding="utf-8")
        with handle:
            handle.write(self.plan.input_file_text)
        self._input_file = handle.name
        return handle.name

    def _cleanup(self) -> None:
        if self._input_file:
            try:
                os.unlink(self._input_file)
            except OSError:
                pass
            self._input_file = None
        try:
            self._log.close()
        except OSError:
            pass

    def _aria2_tail(self, limit: int = 800) -> str:
        try:
            self._log.flush()
            self._log.seek(0)
            data = self._log.read()
        except (OSError, ValueError):
            return ""
        text = data.decode("utf-8", "replace").strip()
        return text[-limit:]

    def _install_signals(self) -> dict[int, object]:
        previous: dict[int, object] = {}

        def handler(signum, _frame):
            self._signal = signum
            self._kill(signum)

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass
        return previous

    @staticmethod
    def _restore_signals(previous: dict[int, object]) -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass

    def _kill(self, signum: int = signal.SIGTERM) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signum)
        except OSError:
            try:
                proc.send_signal(signum)
            except OSError:
                pass

    def _spawn(self, extra: Sequence[str], *, inherit: bool) -> subprocess.Popen:
        argv = [self.aria2_path, *extra, *self.plan.args]
        input_file = self._write_input_file()
        if input_file:
            argv.append(f"--input-file={input_file}")
        else:
            argv.extend(self.plan.uris)
        stdout = None if inherit else self._log
        stderr = None if inherit else self._log
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError as exc:
            raise Aria2StartupError(f"cannot start {self.aria2_path}: {exc}") from exc
        self._proc = proc
        return proc

    # --------------------------------------------------------------------- modes
    def run(self) -> RunResult:
        ensure_directories(self.plan)
        previous = self._install_signals()
        try:
            result = self._attempt()
            # aria2 status 8 means the server refused to resume.  When the resume
            # came from auto_resume (not from an explicit -C -) the user never
            # asked for incremental behaviour, so silently restart from scratch.
            if result.exit_code == CURL_BAD_DOWNLOAD_RESUME and self.plan.resume and not self.plan.resume_requested:
                self._report(result)
                retried = self._retry_without_resume()
                if retried is not None:
                    self._report(retried)
                    return retried
            self._report(result)
            return result
        finally:
            self._restore_signals(previous)
            self._cleanup()

    def _attempt(self) -> RunResult:
        result = self._run_rpc_mode()
        if result is None:
            result = self._run_passthrough_mode()
        return result

    def _report(self, result: RunResult) -> None:
        """Hand the outcome to the display (never twice for the same attempt)."""
        if not result.report:
            return
        self.display.finish(
            Summary(
                rows=result.rows,
                elapsed=result.elapsed,
                exit_code=result.exit_code,
                message=result.message,
            )
        )

    def _retry_without_resume(self) -> RunResult | None:
        if not self.cfg["auto_resume_retry"]:
            return None
        note = "server does not support resuming; restarting the download from scratch"
        print(f"aria2curl: {note}", file=self.stream)
        self.plan.resume = False
        self.plan.args = [a for a in self.plan.args if a != "--continue=true"]
        for target in self.plan.targets:
            path = target.expected_path
            if path:
                for candidate in (path + ".aria2", path):
                    try:
                        os.unlink(candidate)
                    except OSError:
                        pass
        self._timed_out.clear()
        self._started_at.clear()
        try:
            return self._run_rpc_mode() or self._run_passthrough_mode()
        except Aria2StartupError:
            return None

    # ------------------------------------------------------------------ RPC mode
    def _run_rpc_mode(self) -> RunResult | None:
        port = free_port()
        secret = secrets.token_hex(16)
        self._spawn(self._rpc_args(port, secret), inherit=False)
        rpc = Aria2Rpc(port, secret)
        proc = self._proc
        assert proc is not None

        ready = rpc.wait_until_ready(
            float(self.cfg["rpc_start_timeout"]),
            is_alive=lambda: proc.poll() is None,
        )
        if not ready:
            if proc.poll() is None:
                # aria2 is alive but not talking RPC: restart it with its console
                # output visible instead of losing the transfer.
                self._kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
                rpc.close()
                return None
            code = proc.returncode or 0
            rpc.close()
            tail = self._aria2_tail()
            message = tail or f"aria2c exited with status {code}"
            return RunResult(
                exit_code=aria2_to_curl(code),
                message=message,
                elapsed=0.0,
                used_rpc=False,
            )

        started = time.monotonic()
        statuses: list[DownloadStatus] = []
        try:
            statuses = self._poll(rpc, proc, started)
        finally:
            rpc.close()
        elapsed = time.monotonic() - started

        rows = [_status_to_row(status) for status in statuses]
        exit_code, message = self._verdict(rows, proc, elapsed)
        summary_rows = self._merge_expected(rows)
        return RunResult(exit_code=exit_code, message=message, rows=summary_rows, elapsed=elapsed)

    def _poll(self, rpc: Aria2Rpc, proc: subprocess.Popen, started: float) -> list[DownloadStatus]:
        interval = max(0.05, float(self.cfg["poll_interval"]))
        last: list[DownloadStatus] = []
        failures = 0
        while True:
            if self._signal is not None:
                break
            try:
                snapshot = dedupe(rpc.snapshot())
                failures = 0
            except Aria2RpcError:
                failures += 1
                if proc.poll() is not None or failures > 5:
                    break
                time.sleep(interval)
                continue
            last = snapshot
            self._enforce_max_time(rpc, snapshot)
            self.display.render(Frame(rows=[_status_to_row(s) for s in snapshot], elapsed=time.monotonic() - started))
            if snapshot and all(status.finished for status in snapshot):
                break
            if proc.poll() is not None and not snapshot:
                break
            time.sleep(interval)

        # Give aria2 a moment to flush the final state, then shut it down.
        try:
            last = dedupe(rpc.snapshot()) or last
        except Aria2RpcError:
            pass
        try:
            rpc.shutdown()
        except Aria2RpcError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
        return last

    def _enforce_max_time(self, rpc: Aria2Rpc, snapshot: Sequence[DownloadStatus]) -> None:
        budget = self.plan.max_time
        if not budget:
            return
        now = time.monotonic()
        for status in snapshot:
            if status.status == "active" and status.gid not in self._started_at:
                self._started_at[status.gid] = now
            started = self._started_at.get(status.gid)
            if started is None or status.status != "active":
                continue
            if now - started > budget:
                self._timed_out.add(status.gid)
                try:
                    rpc.remove(status.gid)
                except Aria2RpcError:
                    pass

    def _verdict(self, rows: Sequence[Row], proc: subprocess.Popen, elapsed: float) -> tuple[int, str]:
        if self._signal is not None:
            code = CURL_INTERRUPTED if self._signal == signal.SIGINT else CURL_TERMINATED
            return code, f"interrupted by signal {self._signal}"
        if self._timed_out:
            return (
                CURL_OPERATION_TIMEDOUT,
                f"operation timed out after {self.plan.max_time:g} seconds",
            )
        returncodes = [row.error_code for row in rows if row.error_code]
        if returncodes:
            code = aria2_to_curl(returncodes[0])
            failed = next(row for row in rows if row.error_code)
            message = failed.error_message or curl_status_name(code)
            return code, message
        if rows and all(row.status == "complete" for row in rows):
            return CURL_OK, ""
        if not rows:
            code = proc.returncode or 1
            tail = self._aria2_tail()
            return aria2_to_curl(code), tail or f"aria2c exited with status {code}"
        if proc.returncode not in (0, None):
            return aria2_to_curl(proc.returncode), self._aria2_tail() or "aria2c reported an error"
        unfinished = [row for row in rows if row.status != "complete"]
        if unfinished:
            return (
                aria2_to_curl(7),
                f"unfinished download: {unfinished[0].name}",
            )
        return CURL_OK, ""

    def _merge_expected(self, rows: Sequence[Row]) -> list[Row]:
        """Make sure every requested URL shows up in the summary."""
        if len(rows) >= len(self.plan.targets):
            return list(rows)
        seen = {row.name for row in rows}
        merged = list(rows)
        for target in self.plan.targets:
            if target.display not in seen:
                merged.append(Row(name=target.display, status="error", error_message="not started"))
        return merged

    # ----------------------------------------------------------- passthrough mode
    def _run_passthrough_mode(self) -> RunResult:
        started = time.monotonic()
        extra = ["--no-conf=true"]
        if self.cfg["stop_with_process"]:
            extra.append(f"--stop-with-process={os.getpid()}")
        self._spawn(extra, inherit=True)
        proc = self._proc
        assert proc is not None
        while proc.poll() is None:
            if self._signal is not None:
                self._kill()
                break
            time.sleep(0.1)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        elapsed = time.monotonic() - started
        if self._signal is not None:
            code = CURL_INTERRUPTED if self._signal == signal.SIGINT else CURL_TERMINATED
            return RunResult(code, f"interrupted by signal {self._signal}", [], elapsed, used_rpc=False, report=False)
        code = aria2_to_curl(proc.returncode or 0)
        message = "" if code == CURL_OK else curl_status_name(code)
        return RunResult(code, message, [], elapsed, used_rpc=False, report=False)


def run_plan(cfg: Config, plan: Aria2Plan, **kwargs) -> RunResult:
    """Convenience wrapper around :class:`Runner`."""
    return Runner(cfg, plan, **kwargs).run()
