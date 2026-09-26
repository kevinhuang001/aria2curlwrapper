"""Modern terminal progress rendering for aria2curl.

Four engines are available (``display.engine`` in the config):

``rich``
    A live, resizable dashboard: aggregate bar, per-download bars, speed, ETA
    and connection counts, followed by a summary panel.
``plain``
    No dependencies beyond the stdlib: a single live status line on a TTY and
    one line per finished file when stderr is redirected.
``json``
    Newline delimited JSON on stderr, for tooling.
``none``
    Silence (what ``-s`` selects).
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Sequence, TextIO

from .config import Config

FILLED = "█"
PARTIAL = "▉▊▋▌▍▎▏"
EMPTY = "░"
ASCII_FILLED = "#"
ASCII_EMPTY = "-"

STATUS_STYLE = {
    "active": "cyan",
    "waiting": "yellow",
    "paused": "yellow",
    "complete": "green",
    "error": "red",
    "removed": "magenta",
}


# ------------------------------------------------------------------- formatting


def human_size(size: float | int | None) -> str:
    if size is None or size < 0:
        return "?"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}" if value < 10 else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def human_speed(speed: float | int | None) -> str:
    if not speed:
        return "--"
    return f"{human_size(speed)}/s"


def human_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return "--:--"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def bar_text(progress: float, width: int, *, unicode_ok: bool) -> tuple[str, str]:
    """Return ``(filled_part, empty_part)`` for a progress bar."""
    width = max(1, width)
    clamped = min(1.0, max(0.0, progress))
    exact = clamped * width
    full = int(exact)
    if unicode_ok:
        remainder = exact - full
        partial = ""
        if full < width and remainder > 0.02:
            index = min(len(PARTIAL) - 1, int(remainder * len(PARTIAL)))
            partial = PARTIAL[index]
        filled = FILLED * full + partial
        empty = EMPTY * max(0, width - full - len(partial))
    else:
        filled = ASCII_FILLED * full
        empty = ASCII_EMPTY * max(0, width - full)
    return filled, empty


def _unicode_ok(cfg: Config, stream: TextIO) -> bool:
    setting = cfg["unicode"]
    if setting == "always":
        return True
    if setting == "never":
        return False
    encoding = (getattr(stream, "encoding", None) or "utf-8").lower()
    return "utf" in encoding


def _want_color(cfg: Config, stream: TextIO) -> bool:
    setting = cfg["color"]
    if setting == "always":
        return True
    if setting == "never":
        return False
    if not getattr(stream, "isatty", lambda: False)():
        return False
    return "NO_COLOR" not in __import__("os").environ


# ------------------------------------------------------------------------ frames


@dataclass
class Row:
    name: str
    status: str = "waiting"
    completed: int = 0
    total: int = 0
    speed: int = 0
    connections: int = 0
    error_code: int = 0
    error_message: str = ""
    detail: str = ""

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 0.0 if self.status not in ("complete",) else 1.0
        return min(1.0, self.completed / self.total)

    @property
    def eta(self) -> float | None:
        if self.status != "active" or self.total <= 0 or self.speed <= 0:
            return None
        remaining = self.total - self.completed
        if remaining <= 0:
            return None
        return remaining / self.speed


@dataclass
class Frame:
    rows: list[Row] = dataclass_field(default_factory=list)
    elapsed: float = 0.0

    @property
    def total_speed(self) -> int:
        return sum(row.speed for row in self.rows if row.status == "active")

    @property
    def total_size(self) -> int:
        return sum(row.total for row in self.rows)

    @property
    def total_done(self) -> int:
        return sum(min(row.completed, row.total or row.completed) for row in self.rows)

    @property
    def active(self) -> int:
        return sum(1 for row in self.rows if row.status in ("active", "waiting", "paused"))

    @property
    def finished(self) -> int:
        return sum(1 for row in self.rows if row.status in ("complete", "error", "removed"))

    @property
    def overall_progress(self) -> float:
        total = self.total_size
        if total > 0:
            return min(1.0, self.total_done / total)
        if not self.rows:
            return 0.0
        return sum(row.progress for row in self.rows) / len(self.rows)


@dataclass
class Summary:
    rows: list[Row]
    elapsed: float
    exit_code: int
    message: str = ""


# ------------------------------------------------------------------------ engines


class Display:
    """Base class: a display engine receives frames and a final summary."""

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def render(self, frame: Frame) -> None:  # pragma: no cover - trivial
        pass

    def finish(self, summary: Summary) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    @property
    def suppresses_output(self) -> bool:
        return False


class NullDisplay(Display):
    """Silent engine (``-s``)."""

    @property
    def suppresses_output(self) -> bool:
        return True

    def finish(self, summary: Summary) -> None:
        if summary.exit_code != 0 and summary.message:
            print(f"aria2curl: {summary.message}", file=sys.stderr)


class JsonDisplay(Display):
    """Newline delimited JSON events on stderr."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def _emit(self, payload: dict[str, Any]) -> None:
        self.stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.stream.flush()

    def render(self, frame: Frame) -> None:
        self._emit(
            {
                "event": "progress",
                "elapsed": round(frame.elapsed, 3),
                "overall": round(frame.overall_progress, 6),
                "speed": frame.total_speed,
                "active": frame.active,
                "rows": [
                    {
                        "name": row.name,
                        "status": row.status,
                        "completed": row.completed,
                        "total": row.total,
                        "speed": row.speed,
                        "connections": row.connections,
                        "error_code": row.error_code,
                    }
                    for row in frame.rows
                ],
            }
        )

    def finish(self, summary: Summary) -> None:
        self._emit(
            {
                "event": "summary",
                "elapsed": round(summary.elapsed, 3),
                "exit_code": summary.exit_code,
                "message": summary.message,
                "rows": [
                    {
                        "name": row.name,
                        "status": row.status,
                        "completed": row.completed,
                        "total": row.total,
                        "error_code": row.error_code,
                        "error_message": row.error_message,
                        "path": row.detail,
                    }
                    for row in summary.rows
                ],
            }
        )


class PlainDisplay(Display):
    """Zero-dependency renderer.

    On a TTY it keeps one self-overwriting aggregate line plus a line per
    finished file; when stderr is redirected it only reports finished files, so
    logs stay readable.
    """

    def __init__(self, cfg: Config, stream: TextIO, *, is_tty: bool) -> None:
        self.cfg = cfg
        self.stream = stream
        self.is_tty = is_tty
        self.color = _want_color(cfg, stream)
        self.unicode_ok = _unicode_ok(cfg, stream)
        self._last_draw = 0.0
        self._reported: set[str] = set()
        self._line_open = False
        self._interval = max(0.05, 1.0 / max(1.0, float(cfg["refresh_per_second"])))

    # -- helpers
    def _style(self, text: str, code: str) -> str:
        if not self.color:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def _clear_line(self) -> None:
        if self.is_tty and self._line_open:
            self.stream.write("\r\x1b[2K")
            self._line_open = False

    def _write_line(self, text: str) -> None:
        self._clear_line()
        self.stream.write(text + "\n")
        self.stream.flush()

    def _finish_report(self, row: Row) -> None:
        if row.status == "complete":
            mark = self._style("✔", "32")
            extra = f"{human_size(row.total)} in {human_speed(row.speed)}"
        elif row.status == "error":
            mark = self._style("✘", "31")
            extra = f"error {row.error_code}: {row.error_message or 'failed'}"
        elif row.status == "removed":
            mark = self._style("•", "35")
            extra = "removed"
        else:
            return
        self._write_line(f"{mark} {row.name}  {extra}")

    def render(self, frame: Frame) -> None:
        now = time.monotonic()
        for row in frame.rows:
            if row.status in ("complete", "error", "removed") and row.name not in self._reported:
                self._reported.add(row.name)
                self._finish_report(row)

        if not self.is_tty or self.cfg["style"] != "bar":
            if not self.is_tty:
                return
        if now - self._last_draw < self._interval and frame.overall_progress < 1.0:
            return
        self._last_draw = now

        width = max(10, shutil.get_terminal_size((80, 24)).columns - 46)
        filled, empty = bar_text(frame.overall_progress, min(28, width), unicode_ok=self.unicode_ok)
        line = (
            f"[{self._style(filled, '36')}{empty}] "
            f"{frame.overall_progress * 100:5.1f}%  "
            f"{human_speed(frame.total_speed):>11}  "
            f"ETA {human_duration(_frame_eta(frame)):>7}  "
            f"{frame.finished}/{len(frame.rows)} files"
        )
        self.stream.write("\r\x1b[2K" + line)
        self.stream.flush()
        self._line_open = True

    def finish(self, summary: Summary) -> None:
        self._clear_line()
        self._reported.clear()
        if summary.message and summary.exit_code != 0:
            self._write_line(self._style(f"aria2curl: {summary.message}", "31"))
        if not self.cfg["show_summary"] or not self.is_tty:
            if not self.is_tty and summary.exit_code == 0:
                done = [row for row in summary.rows if row.status == "complete"]
                self._write_line(
                    f"aria2curl: {len(done)} file(s), {human_size(sum(row.total for row in done))}, "
                    f"{human_speed(_average_speed(summary))} in {human_duration(summary.elapsed)}"
                )
            return
        ok = sum(1 for row in summary.rows if row.status == "complete")
        failed = [row for row in summary.rows if row.status == "error"]
        parts = [
            self._style("aria2curl", "1"),
            f"{ok}/{len(summary.rows)} ok",
            human_size(sum(row.total for row in summary.rows)),
            human_speed(_average_speed(summary)),
            human_duration(summary.elapsed),
        ]
        self._write_line("  ".join(parts))
        for row in failed:
            self._write_line("  " + self._style(f"✘ {row.name}: {row.error_message or 'failed'}", "31"))


def _frame_eta(frame: Frame) -> float | None:
    if frame.total_size > 0 and frame.total_speed > 0:
        remaining = frame.total_size - frame.total_done
        if remaining > 0:
            return remaining / frame.total_speed
    etas = [row.eta for row in frame.rows if row.eta is not None]
    return max(etas) if etas else None


def _average_speed(summary: Summary) -> float:
    done = sum(row.completed for row in summary.rows)
    if summary.elapsed <= 0 or done <= 0:
        return 0.0
    return done / summary.elapsed


class RichDisplay(Display):
    """The fancy engine: a live dashboard built on ``rich``."""

    def __init__(self, cfg: Config, stream: TextIO, *, is_tty: bool) -> None:
        from rich.console import Console

        color = _want_color(cfg, stream)
        self.cfg = cfg
        self.stream = stream
        self.console = Console(
            file=stream,
            force_terminal=is_tty or color,
            no_color=not color,
            highlight=False,
            soft_wrap=False,
            legacy_windows=False,
        )
        self.unicode_ok = _unicode_ok(cfg, stream)
        self._live = None
        self._started = False

    # -- renderable construction
    def _bar(self, progress: float, width: int, style: str) -> Any:
        from rich.text import Text

        filled, empty = bar_text(progress, width, unicode_ok=self.unicode_ok)
        text = Text()
        text.append(filled, style=style)
        text.append(empty, style="grey37")
        return text

    def _header(self, frame: Frame) -> Any:
        from rich.text import Text

        text = Text()
        text.append("aria2curl ", style="bold white")
        if frame.active:
            text.append(f"{frame.active} downloading", style="cyan")
        else:
            text.append("finishing", style="cyan")
        text.append("  ·  ", style="grey50")
        text.append(f"{human_speed(frame.total_speed)}", style="bold green")
        text.append("  ·  ", style="grey50")
        text.append(
            f"{human_size(frame.total_done)} / {human_size(frame.total_size)}",
            style="white",
        )
        text.append("  ·  ", style="grey50")
        text.append(f"elapsed {human_duration(frame.elapsed)}", style="grey70")
        eta = _frame_eta(frame)
        if eta:
            text.append("  ·  ", style="grey50")
            text.append(f"ETA {human_duration(eta)}", style="grey70")
        return text

    def _table(self, frame: Frame) -> Any:
        from rich.table import Table
        from rich.text import Text

        width = max(40, self.console.width)
        bar_width = 20 if self.cfg["style"] == "bar" else 0
        show = {
            "speed": bool(self.cfg["show_speed"]),
            "eta": bool(self.cfg["show_eta"]),
            "conn": bool(self.cfg["show_connections"]),
        }
        name_width = max(
            12,
            width
            - bar_width
            - 8
            - (11 if show["speed"] else 0)
            - (8 if show["eta"] else 0)
            - (4 if show["conn"] else 0)
            - 26,
        )

        table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False, expand=False)
        table.add_column("name", width=name_width, no_wrap=True, overflow="ellipsis")
        table.add_column("progress", style="grey70", no_wrap=True)
        if show["speed"]:
            table.add_column("speed", justify="right", no_wrap=True)
        if show["eta"]:
            table.add_column("eta", justify="right", style="grey70", no_wrap=True)
        if show["conn"]:
            table.add_column("cn", justify="right", style="grey70", no_wrap=True)

        for row in frame.rows:
            style = STATUS_STYLE.get(row.status, "white")
            name = Text(row.name, style="white" if row.status == "active" else "grey70")
            if row.status == "waiting":
                name.stylize("yellow")
            cells: list[Any] = [name]
            if bar_width:
                cells.append(self._bar(row.progress, bar_width, style))
            if row.status == "complete":
                detail = Text(f"{human_size(row.total):>11}", style="green")
            elif row.status == "error":
                detail = Text(f"{'error':>11}", style="red")
            elif row.status == "waiting":
                detail = Text(f"{'queued':>11}", style="yellow")
            else:
                detail = Text(f"{row.progress * 100:>10.1f}%", style=style)
            cells.append(detail)
            if show["speed"]:
                cells.append(Text(human_speed(row.speed) if row.status == "active" else "", style="green"))
            if show["eta"]:
                cells.append(Text(human_duration(row.eta) if row.eta else "", style="grey70"))
            if show["conn"]:
                cells.append(Text(str(row.connections) if row.status == "active" else "", style="grey70"))
            table.add_row(*cells)
        return table

    def _renderable(self, frame: Frame) -> Any:
        from rich.console import Group

        parts: list[Any] = []
        if self.cfg["show_header"]:
            parts.append(self._header(frame))
            width = max(20, self.console.width - 4)
            parts.append(self._bar(frame.overall_progress, min(width, 72), "magenta"))
        parts.append(self._table(frame))
        return Group(*parts)

    # -- lifecycle
    def render(self, frame: Frame) -> None:
        from rich.live import Live

        if self._live is None:
            self._live = Live(
                self._renderable(frame),
                console=self.console,
                refresh_per_second=max(2.0, min(30.0, float(self.cfg["refresh_per_second"]))),
                transient=True,
                auto_refresh=False,
                vertical_overflow="visible",
            )
            self._live.start(refresh=True)
            self._started = True
            return
        self._live.update(self._renderable(frame), refresh=True)

    def finish(self, summary: Summary) -> None:
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        if self._live is not None:
            self._live.stop()
            self._live = None
        if summary.message and summary.exit_code != 0:
            self.console.print(f"[red]{summary.message}[/red]")
        if not self.cfg["show_summary"]:
            return

        table = Table(box=None, padding=(0, 1), show_header=False, expand=False)
        table.add_column("status", width=2, no_wrap=True)
        table.add_column("file", no_wrap=True, overflow="ellipsis")
        table.add_column("size", justify="right", no_wrap=True)
        table.add_column("detail", no_wrap=False)

        for row in summary.rows:
            if row.status == "complete":
                mark = Text("✔", style="green")
                detail = Text(row.detail or "", style="grey62")
            elif row.status == "error":
                mark = Text("✘", style="red")
                detail = Text(f"error {row.error_code}: {row.error_message or 'failed'}", style="red")
            else:
                mark = Text("•", style="magenta")
                detail = Text(row.status, style="magenta")
            table.add_row(mark, Text(row.name, style="white"), Text(human_size(row.total), style="grey70"), detail)

        ok = sum(1 for row in summary.rows if row.status == "complete")
        failed = [row for row in summary.rows if row.status == "error"]
        title = Text.assemble(
            ("aria2curl ", "bold white"),
            (f"{ok}/{len(summary.rows)} ok", "green" if not failed else "yellow"),
            ("  ·  ", "grey50"),
            (human_size(sum(row.total for row in summary.rows)), "white"),
            ("  ·  ", "grey50"),
            (human_speed(_average_speed(summary)), "bold green"),
            ("  ·  ", "grey50"),
            (human_duration(summary.elapsed), "grey70"),
        )
        border = "green" if not failed else "red"
        self.console.print(Panel(table, title=title, border_style=border, expand=False))

    def close(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None


# ----------------------------------------------------------------------- factory


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401
    except Exception:  # pragma: no cover - rich is a declared dependency
        return False
    return True


def _is_dumb_terminal() -> bool:
    import os

    return os.environ.get("TERM", "").lower() in ("dumb", "unknown")


def make_display(
    cfg: Config,
    *,
    stream: TextIO | None = None,
    is_tty: bool | None = None,
    quiet: bool = False,
) -> Display:
    """Pick a display engine from the configuration and the environment."""
    stream = stream if stream is not None else sys.stderr
    if is_tty is None:
        is_tty = bool(getattr(stream, "isatty", lambda: False)())

    engine = cfg["engine"]
    if quiet or cfg["quiet"]:
        return NullDisplay()
    if engine in ("none", "json"):
        return NullDisplay() if engine == "none" else JsonDisplay(stream)
    if engine == "auto":
        if not is_tty or _is_dumb_terminal():
            engine = "plain"
        else:
            engine = "rich" if _rich_available() else "plain"
    if engine == "rich":
        if _rich_available() and (is_tty or cfg["color"] == "always"):
            return RichDisplay(cfg, stream, is_tty=is_tty)
        return PlainDisplay(cfg, stream, is_tty=is_tty)
    return PlainDisplay(cfg, stream, is_tty=is_tty)


def frame_from_rows(rows: Sequence[Row], elapsed: float) -> Frame:
    return Frame(rows=list(rows), elapsed=elapsed)
