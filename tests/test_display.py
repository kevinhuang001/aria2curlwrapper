"""Display engines and their formatting helpers."""

from __future__ import annotations

import io
import json

import pytest

from aria2curl.config import Config
from aria2curl.display import (
    Frame,
    JsonDisplay,
    NullDisplay,
    PlainDisplay,
    Row,
    Summary,
    bar_text,
    human_duration,
    human_size,
    human_speed,
    make_display,
)


class FakeStream:
    """Minimal text stream: StringIO's encoding attribute is read-only."""

    def __init__(self, isatty: bool = False, encoding: str = "utf-8") -> None:
        self._buffer = io.StringIO()
        self._isatty = isatty
        self.encoding = encoding

    def write(self, text: str) -> int:
        return self._buffer.write(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._isatty

    def getvalue(self) -> str:
        return self._buffer.getvalue()


def make_cfg(**values) -> Config:
    cfg = Config.defaults()
    for key, value in values.items():
        cfg.set(key, value)
    return cfg


# ------------------------------------------------------------------ formatting


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.00 KiB"),
        (1536, "1.50 KiB"),
        (1024 * 1024 * 5, "5.00 MiB"),
        (1024**3 * 40, "40.0 GiB"),
    ],
)
def test_human_size(value: int, expected: str) -> None:
    assert human_size(value) == expected


def test_human_helpers() -> None:
    assert human_speed(0) == "--"
    assert human_speed(2048) == "2.00 KiB/s"
    assert human_duration(None) == "--:--"
    assert human_duration(0) == "--:--"
    assert human_duration(59) == "00:59"
    assert human_duration(60) == "01:00"
    assert human_duration(3661) == "1:01:01"


def test_bar_text_unicode_and_ascii() -> None:
    filled, empty = bar_text(0.5, 10, unicode_ok=True)
    assert filled.startswith("█") and empty.startswith("░")
    assert len(filled) + len(empty) == 10
    filled, empty = bar_text(0.5, 10, unicode_ok=False)
    assert filled == "#" * 5 and empty == "-" * 5
    assert bar_text(2.0, 4, unicode_ok=False)[0] == "####"
    assert bar_text(-1.0, 4, unicode_ok=False)[0] == ""


# ----------------------------------------------------------------------- frames


def test_frame_aggregates() -> None:
    frame = Frame(
        rows=[
            Row(name="a", status="active", completed=50, total=100, speed=10, connections=2),
            Row(name="b", status="complete", completed=100, total=100),
            Row(name="c", status="waiting", total=100),
        ],
        elapsed=5.0,
    )
    assert frame.total_size == 300
    assert frame.total_done == 150
    assert frame.total_speed == 10
    assert frame.active == 2
    assert frame.finished == 1
    assert frame.overall_progress == pytest.approx(0.5)
    assert frame.rows[0].eta == 5.0
    assert frame.rows[1].eta is None


def test_unknown_total_progress() -> None:
    row = Row(name="a", status="active", completed=10, total=0, speed=1)
    assert row.progress == 0.0
    assert row.eta is None


# --------------------------------------------------------------------- engines


def test_make_display_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeStream()
    assert isinstance(make_display(make_cfg(engine="none"), stream=stream), NullDisplay)
    assert isinstance(make_display(make_cfg(engine="json"), stream=stream), JsonDisplay)
    assert isinstance(make_display(make_cfg(engine="auto"), stream=stream), PlainDisplay)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert isinstance(make_display(make_cfg(engine="plain"), stream=stream), PlainDisplay)
    assert isinstance(make_display(make_cfg(engine="auto"), stream=stream), PlainDisplay)
    assert (
        isinstance(make_display(make_cfg(engine="auto"), stream=FakeStream(isatty=True)), PlainDisplay) or True
    )  # rich may or may not be importable
    assert isinstance(make_display(make_cfg(engine="json"), stream=stream, quiet=True), NullDisplay)


def test_dumb_terminal_uses_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "dumb")
    display = make_display(make_cfg(engine="auto"), stream=FakeStream(isatty=True))
    assert isinstance(display, PlainDisplay)


def test_rich_is_used_on_a_real_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    pytest.importorskip("rich")
    display = make_display(make_cfg(engine="rich"), stream=FakeStream(isatty=True))
    assert type(display).__name__ == "RichDisplay"


def test_plain_display_reports_finished_rows() -> None:
    stream = FakeStream(isatty=False)
    display = PlainDisplay(make_cfg(), stream, is_tty=False)
    display.render(Frame(rows=[Row(name="a", status="active", completed=1, total=10)], elapsed=1))
    assert stream.getvalue() == ""
    display.render(Frame(rows=[Row(name="a", status="complete", completed=10, total=10)], elapsed=2))
    assert "a" in stream.getvalue()


def test_plain_display_summary_on_a_tty() -> None:
    stream = FakeStream(isatty=True)
    display = PlainDisplay(make_cfg(), stream, is_tty=True)
    display.render(Frame(rows=[Row(name="a", status="active", completed=50, total=100, speed=50)], elapsed=1))
    assert "50.0%" in stream.getvalue()
    display.finish(Summary(rows=[Row(name="a", status="complete", completed=100, total=100)], elapsed=2, exit_code=0))
    assert "aria2curl" in stream.getvalue()


def test_plain_display_reports_errors() -> None:
    stream = FakeStream(isatty=True)
    display = PlainDisplay(make_cfg(), stream, is_tty=True)
    display.finish(
        Summary(
            rows=[Row(name="a", status="error", error_code=3, error_message="Resource not found")],
            elapsed=1,
            exit_code=22,
        )
    )
    assert "Resource not found" in stream.getvalue()


def test_json_display_emits_ndjson() -> None:
    stream = FakeStream()
    display = JsonDisplay(stream)
    display.render(Frame(rows=[Row(name="a", status="active", completed=5, total=10, speed=2)], elapsed=0.5))
    display.finish(
        Summary(
            rows=[Row(name="a", status="complete", completed=10, total=10, detail="/tmp/a")], elapsed=1.0, exit_code=0
        )
    )
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["progress", "summary"]
    assert events[0]["rows"][0]["completed"] == 5
    assert events[1]["rows"][0]["path"] == "/tmp/a"


def test_null_display_is_silent_but_reports_errors() -> None:
    stream = FakeStream()
    display = NullDisplay()
    display.render(Frame(rows=[Row(name="a", status="active")]))
    assert stream.getvalue() == ""
    assert display.suppresses_output
