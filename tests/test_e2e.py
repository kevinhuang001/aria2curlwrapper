"""End-to-end downloads through aria2c (skipped when aria2c is unavailable)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import FILE_A, FILE_B, FILE_BIG, PIECE, clean_env, run_cli


@pytest.fixture()
def env(aria2_binary: str, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    return clean_env(
        ARIA2CURL_CONFIG=str(config_file),
        ARIA2CURL_ARIA2_PATH=aria2_binary,
        ARIA2CURL_ENGINE="none",
    )


def test_single_file_download(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    out = tmp_path / "out.bin"
    result = run_cli(["-o", str(out), f"{http_server.url}/files/a.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_A


def test_segmented_download_uses_multiple_connections(
    http_server, tmp_path: Path, env: dict[str, str], requests_log
) -> None:
    out = tmp_path / "big.bin"
    env = dict(env, ARIA2CURL_SPLIT="4", ARIA2CURL_MIN_SPLIT_SIZE="1M", ARIA2CURL_MAX_CONNECTION_PER_SERVER="4")
    result = run_cli(["-o", str(out), f"{http_server.url}/files/big.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_BIG
    ranges = [request.range_header for request in requests_log if request.range_header]
    assert len(ranges) >= 2, f"expected segmented requests, saw {ranges}"


def test_remote_name_download(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    result = run_cli(["-O", f"{http_server.url}/files/b.bin"], env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "b.bin").read_bytes() == FILE_B


def test_multiple_urls_with_remote_name_all(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    result = run_cli(
        ["--remote-name-all", f"{http_server.url}/files/a.bin", f"{http_server.url}/files/b.bin"],
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "a.bin").read_bytes() == FILE_A
    assert (tmp_path / "b.bin").read_bytes() == FILE_B


def test_multiple_outputs(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    one, two = tmp_path / "one.bin", tmp_path / "two.bin"
    result = run_cli(
        ["-o", str(one), f"{http_server.url}/files/a.bin", "-o", str(two), f"{http_server.url}/files/b.bin"],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert one.read_bytes() == FILE_A
    assert two.read_bytes() == FILE_B


def test_redirect_is_followed(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    out = tmp_path / "r.bin"
    result = run_cli(["-o", str(out), f"{http_server.url}/redirect?to=/files/b.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_B


def test_404_maps_to_curl_exit_22(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    result = run_cli(["-o", str(tmp_path / "x.bin"), f"{http_server.url}/nope"], env=env)
    assert result.returncode == 22


def test_existing_file_is_overwritten_like_curl(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    out = tmp_path / "out.bin"
    out.write_bytes(b"Z" * 500_000)
    result = run_cli(["-o", str(out), f"{http_server.url}/files/a.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_A, "curl semantics: the target is truncated and replaced"


def test_auto_resume_continues_a_partial_file(http_server, tmp_path: Path, env: dict[str, str], requests_log) -> None:
    out = tmp_path / "big.bin"
    # aria2 resumes on piece boundaries; give it one complete piece to skip.
    out.write_bytes(FILE_BIG[:PIECE])
    started = time.monotonic()
    result = run_cli(["-o", str(out), f"{http_server.url}/files/big.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_BIG
    assert any(request.range_header for request in requests_log), "aria2 should have asked for a byte range"
    assert time.monotonic() - started < 60


def test_explicit_continue_at_resumes(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    out = tmp_path / "big.bin"
    out.write_bytes(FILE_BIG[:PIECE])
    result = run_cli(["-C", "-", "-o", str(out), f"{http_server.url}/files/big.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_BIG


def test_resume_falls_back_to_a_fresh_download_when_unsupported(
    http_server, tmp_path: Path, env: dict[str, str]
) -> None:
    out = tmp_path / "a.bin"
    out.write_bytes(b"stale partial data")
    result = run_cli(["-o", str(out), f"{http_server.url}/norange"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_A


def test_create_dirs(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    out = tmp_path / "deep" / "nested" / "a.bin"
    result = run_cli(["--create-dirs", "-o", str(out), f"{http_server.url}/files/a.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_A


def test_content_disposition_name(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    result = run_cli(["-J", "-O", f"{http_server.url}/disposition"], env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "from-header.bin").read_bytes() == FILE_A


def test_basic_auth(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    out = tmp_path / "auth.bin"
    ok = run_cli(["--basic", "-u", "user:pass", "-o", str(out), f"{http_server.url}/auth"], env=env)
    assert ok.returncode == 0, ok.stderr
    assert out.read_bytes() == FILE_A
    denied = run_cli(["-o", str(tmp_path / "no.bin"), f"{http_server.url}/auth"], env=env)
    assert denied.returncode != 0


def test_max_time_is_enforced_by_the_supervisor(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    started = time.monotonic()
    result = run_cli(
        ["-m", "2", "-o", str(tmp_path / "slow.bin"), f"{http_server.url}/slow?rate=20000&size=400000"],
        env=env,
        timeout=90,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 28, result.stderr
    assert elapsed < 30, "the supervisor should stop the transfer long before the server finishes"


def test_json_progress_events(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    env = dict(env, ARIA2CURL_ENGINE="json")
    result = run_cli(["-o", str(tmp_path / "a.bin"), "--limit-rate", "2M", f"{http_server.url}/files/big.bin"], env=env)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert events, result.stderr
    assert events[-1]["event"] == "summary"
    assert events[-1]["exit_code"] == 0
    assert events[-1]["rows"][0]["completed"] == len(FILE_BIG)


def test_configured_directory_is_used_for_url_derived_names(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    target_dir = tmp_path / "downloads"
    env = dict(env, ARIA2CURL_DIR=str(target_dir))
    result = run_cli(["-O", f"{http_server.url}/files/a.bin"], env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (target_dir / "a.bin").read_bytes() == FILE_A


def test_fractional_sized_file_is_intact(http_server, tmp_path: Path, env: dict[str, str]) -> None:
    out = tmp_path / "b.bin"
    result = run_cli(["-o", str(out), f"{http_server.url}/files/b.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.stat().st_size == len(FILE_B)
    assert out.read_bytes() == FILE_B
