"""CLI surface: subcommands, dry runs, alias management and curl fallback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FILE_A, FILE_B, clean_env, run_cli

from aria2curl import alias as alias_mod


@pytest.fixture()
def env(aria2_binary: str, config_file: Path) -> dict[str, str]:
    return clean_env(
        ARIA2CURL_CONFIG=str(config_file),
        ARIA2CURL_ARIA2_PATH=aria2_binary,
        ARIA2CURL_ENGINE="none",
    )


# ------------------------------------------------------------------- meta usage


def test_help_and_version() -> None:
    helped = run_cli(["--acurl-help"])
    assert helped.returncode == 0
    assert "curl-compatible front end" in helped.stdout
    version = run_cli(["version"])
    assert version.returncode == 0
    assert "aria2curl" in version.stdout
    assert "python" in version.stdout


def test_no_arguments_prints_usage() -> None:
    result = run_cli([])
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_unknown_wrapper_option_is_rejected() -> None:
    result = run_cli(["--acurl-nonsense", "http://example.test/f"])
    assert result.returncode == 2
    assert "unknown aria2curl option" in result.stderr


def test_doctor_runs() -> None:
    result = run_cli(["doctor"])
    assert "python" in result.stdout
    assert "aria2c" in result.stdout
    assert result.returncode in (0, 1)


# ------------------------------------------------------------------ config CLI


def test_config_round_trip(env: dict[str, str], config_file: Path) -> None:
    assert run_cli(["config", "path"], env=env).stdout.strip() == str(config_file)
    set_result = run_cli(["config", "set", "split", "16"], env=env)
    assert set_result.returncode == 0, set_result.stderr
    assert config_file.is_file()
    assert run_cli(["config", "get", "split"], env=env).stdout.strip() == "16"
    assert run_cli(["config", "unset", "split"], env=env).returncode == 0
    assert run_cli(["config", "get", "split"], env=env).stdout.strip() == "5"


def test_config_set_rejects_bad_values(env: dict[str, str]) -> None:
    result = run_cli(["config", "set", "split", "999"], env=env)
    assert result.returncode == 2
    assert "must be <=" in result.stderr


def test_config_list_json(env: dict[str, str]) -> None:
    result = run_cli(["config", "list", "--json"], env=env)
    payload = json.loads(result.stdout)
    assert payload["values"]["split"]["value"] == 5
    assert "engine" in payload["values"]


def test_config_list_plain_mentions_keys(env: dict[str, str]) -> None:
    result = run_cli(["config", "list"], env=env)
    assert result.returncode == 0
    assert "split" in result.stdout
    assert "aria2" in result.stdout


def test_config_help(env: dict[str, str]) -> None:
    assert "usage: aria2curl config" in run_cli(["config"], env=env).stderr + run_cli(["config"], env=env).stdout


def test_config_changed_only(env: dict[str, str]) -> None:
    run_cli(["config", "set", "split", "9"], env=env)
    result = run_cli(["config", "list", "--changed"], env=env)
    assert "split" in result.stdout
    assert "min_split_size" not in result.stdout


# ------------------------------------------------------------------- alias CLI


def test_alias_print(tmp_path: Path) -> None:
    result = run_cli(["alias", "print", "--shell", "bash"])
    assert result.returncode == 0
    assert "alias curl=" in result.stdout
    assert alias_mod.BEGIN_MARK in result.stdout


def test_alias_print_fish_uses_a_function(tmp_path: Path) -> None:
    result = run_cli(["alias", "print", "--shell", "fish"])
    assert "function curl" in result.stdout


def test_alias_install_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = clean_env(HOME=str(home))
    rc = home / ".bashrc"
    rc.write_text("# existing config\n", encoding="utf-8")

    first = run_cli(["alias", "install", "--shell", "bash", "--name", "both"], env=env)
    assert first.returncode == 0, first.stderr
    text = rc.read_text(encoding="utf-8")
    assert "# existing config" in text
    assert text.count(alias_mod.BEGIN_MARK) == 1
    assert "alias curl=" in text and "alias acurl=" in text

    second = run_cli(["alias", "install", "--shell", "bash", "--name", "both"], env=env)
    assert second.returncode == 0
    text = rc.read_text(encoding="utf-8")
    assert text.count(alias_mod.BEGIN_MARK) == 1, "install must not duplicate the block"

    status = run_cli(["alias", "status", "--shell", "bash"], env=env)
    assert "installed" in status.stdout

    removed = run_cli(["alias", "uninstall", "--shell", "bash"], env=env)
    assert removed.returncode == 0
    text = rc.read_text(encoding="utf-8")
    assert alias_mod.BEGIN_MARK not in text
    assert "# existing config" in text


def test_alias_install_accepts_an_explicit_command(tmp_path: Path) -> None:
    """`alias install --command PATH` (space form) must work: the installer uses it."""
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "bin" / "aria2curl"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    env = clean_env(HOME=str(home))
    result = run_cli(["alias", "install", "--shell", "bash", "--name", "curl", "--command", str(fake)], env=env)
    assert result.returncode == 0, result.stderr
    text = (home / ".bashrc").read_text(encoding="utf-8")
    assert f"alias curl='{fake}'" in text


def test_alias_rejects_unknown_name() -> None:
    result = run_cli(["alias", "install", "--name", "wget"])
    assert result.returncode == 2
    assert "unknown alias name" in result.stderr


# -------------------------------------------------------------------- dry runs


def test_dry_run_shows_the_aria2_command(env: dict[str, str], tmp_path: Path) -> None:
    out = tmp_path / "out.bin"
    result = run_cli(["--acurl-dry-run", "-o", str(out), "http://example.test/a.bin"], env=env)
    assert result.returncode == 0
    assert "decision:   aria2" in result.stderr
    assert "--split=5" in result.stderr
    assert "(dry run: not executing)" in result.stderr
    assert not out.exists()


def test_explain_reports_a_fallback(env: dict[str, str]) -> None:
    result = run_cli(["--acurl-explain", "-X", "POST", "http://example.test/a.bin"], env=env)
    assert result.returncode == 0
    assert "decision:   curl" in result.stderr
    assert "request method" in result.stderr


# -------------------------------------------------------------------- fallback


def test_fallback_execs_real_curl(http_server, tmp_path: Path, env: dict[str, str], curl_binary: str) -> None:
    result = run_cli(
        ["-X", "POST", "-d", "payload=1", f"{http_server.url}/echo"],
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["method"] == "POST"
    assert payload["body"] == "payload=1"


def test_fallback_keeps_curl_exit_codes(http_server, tmp_path: Path, env: dict[str, str], curl_binary: str) -> None:
    result = run_cli(["--max-filesize", "10", f"{http_server.url}/files/b.bin"], env=env)
    assert result.returncode == 63  # curl: maximum file size exceeded


def test_fallback_still_downloads_files(http_server, tmp_path: Path, env: dict[str, str], curl_binary: str) -> None:
    out = tmp_path / "c.bin"
    result = run_cli(["--max-filesize", "1G", "-o", str(out), f"{http_server.url}/files/a.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_A


def test_mode_curl_always_uses_curl(http_server, tmp_path: Path, env: dict[str, str], curl_binary: str) -> None:
    out = tmp_path / "curl.bin"
    env = dict(env, ARIA2CURL_MODE="curl")
    result = run_cli(["-o", str(out), f"{http_server.url}/files/b.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_B


def test_mode_aria2_refuses_to_fall_back(env: dict[str, str]) -> None:
    result = run_cli(["--acurl-mode=aria2", "-X", "POST", "http://example.test/a.bin"], env=env)
    assert result.returncode == 2
    assert "cannot translate" in result.stderr


def test_no_fallback_flag_reports_failure(env: dict[str, str]) -> None:
    result = run_cli(["--acurl-no-fallback", "-X", "POST", "http://example.test/a.bin"], env=env)
    assert result.returncode == 2


def test_curlrc_policy_fallback(tmp_path: Path, env: dict[str, str], http_server, curl_binary: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".curlrc").write_text("silent\n", encoding="utf-8")
    out = tmp_path / "rc.bin"
    env = dict(env, HOME=str(home), ARIA2CURL_CURLRC_POLICY="fallback", ARIA2CURL_ENGINE="none")
    result = run_cli(["-o", str(out), f"{http_server.url}/files/a.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_A


def test_depth_guard_prevents_recursion(http_server, tmp_path: Path, env: dict[str, str], curl_binary: str) -> None:
    """ARIA2CURL_DEPTH=1 means "a wrapper invoked me": run plain curl."""
    out = tmp_path / "nested.bin"
    env = dict(env, ARIA2CURL_DEPTH="1")
    result = run_cli(["-o", str(out), f"{http_server.url}/files/a.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_A


def test_missing_aria2_falls_back_to_curl(http_server, tmp_path: Path, env: dict[str, str], curl_binary: str) -> None:
    env = dict(env, ARIA2CURL_ARIA2_PATH="/nonexistent/aria2c")
    out = tmp_path / "noaria2.bin"
    result = run_cli(["-o", str(out), f"{http_server.url}/files/a.bin"], env=env)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == FILE_A


# ------------------------------------------------------- system wide install


@pytest.fixture()
def system_env(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    system = tmp_path / "etc" / "aria2curl" / "config.toml"
    return dict(env, ARIA2CURL_SYSTEM_CONFIG=str(system))


def test_config_system_layer_round_trip(system_env: dict[str, str], tmp_path: Path) -> None:
    system = Path(system_env["ARIA2CURL_SYSTEM_CONFIG"])
    written = run_cli(["config", "set", "--system", "split", "9", "curl_path", "/usr/bin/curl"], env=system_env)
    assert written.returncode == 0, written.stderr
    assert system.is_file()
    assert run_cli(["config", "path", "--system"], env=system_env).stdout.strip() == str(system)

    explained = run_cli(["config", "get", "split", "--explain"], env=system_env)
    assert "source:      system" in explained.stdout

    # The user layer still wins.
    assert run_cli(["config", "set", "split", "3"], env=system_env).returncode == 0
    assert run_cli(["config", "get", "split"], env=system_env).stdout.strip() == "3"
    assert run_cli(["config", "get", "curl_path"], env=system_env).stdout.strip() == "/usr/bin/curl"


def test_config_list_reports_the_system_layer(system_env: dict[str, str], tmp_path: Path) -> None:
    run_cli(["config", "set", "--system", "split", "9"], env=system_env)
    result = run_cli(["config", "list"], env=system_env)
    assert "system layer" in result.stdout
    payload = json.loads(run_cli(["config", "list", "--json"], env=system_env).stdout)
    assert payload["values"]["split"]["value"] == 9
    assert payload["values"]["split"]["source"] == "system"


def test_config_unset_system_layer(system_env: dict[str, str]) -> None:
    run_cli(["config", "set", "--system", "split", "9"], env=system_env)
    assert run_cli(["config", "unset", "--system", "split"], env=system_env).returncode == 0
    assert run_cli(["config", "get", "split"], env=system_env).stdout.strip() == "5"


# ------------------------------------------------------------- shim behaviour


def _write_curl_shim(directory: Path, real_curl: str, *, export_real: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "curl"
    export = f'export ARIA2CURL_REAL_CURL="{real_curl}"\n' if export_real else ""
    shim.write_text(
        "#!/bin/sh\n"
        "# aria2curl system shim (installed by install-system.sh)\n"
        f"{export}"
        f'exec "{sys.executable}" -m aria2curl "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


@pytest.mark.parametrize("export_real", [True, False])
def test_curl_shim_never_recurses(
    http_server, tmp_path: Path, env: dict[str, str], curl_binary: str, export_real: bool
) -> None:
    """A system-wide `curl` shim must hand unsupported commands to the real curl.

    Run without ARIA2CURL_REAL_CURL too: then aria2curl has to notice that the
    `curl` on PATH is a shim that mentions aria2curl and skip it, otherwise the
    two would call each other until the process limit.
    """
    if not export_real and not any(
        os.path.exists(path) for path in ("/usr/bin/curl", "/bin/curl", "/usr/local/bin/curl")
    ):
        pytest.skip("no absolute curl path for the shim guard to fall back to")

    shim = _write_curl_shim(tmp_path / "bin", curl_binary, export_real=export_real)
    shim_env = dict(env, PATH=f"{shim.parent}{os.pathsep}{env['PATH']}")
    proc = subprocess.run(
        [str(shim), "-sS", "-X", "POST", "-d", "x=1", f"{http_server.url}/echo"],
        capture_output=True,
        text=True,
        env=shim_env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["method"] == "POST"


def test_curl_shim_accelerates_a_script_style_download(
    http_server, tmp_path: Path, env: dict[str, str], curl_binary: str
) -> None:
    shim = _write_curl_shim(tmp_path / "bin", curl_binary, export_real=True)
    shim_env = dict(env, PATH=f"{shim.parent}{os.pathsep}{env['PATH']}")
    out = tmp_path / "shim.bin"
    proc = subprocess.run(
        [str(shim), "-sS", "-o", str(out), f"{http_server.url}/files/b.bin"],
        capture_output=True,
        text=True,
        env=shim_env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_bytes() == FILE_B


def test_resolve_curl_skips_a_shim(tmp_path: Path) -> None:
    from aria2curl.cli import resolve_curl
    from aria2curl.config import Config
    from aria2curl.errors import UsageError

    real = shutil.which("curl")
    if not real or not os.path.exists("/usr/bin/curl"):
        pytest.skip("needs a real curl at /usr/bin/curl")

    shim = _write_curl_shim(tmp_path / "bin", "/usr/bin/curl", export_real=False)
    cfg = Config.defaults()
    cfg.set("curl_path", str(shim))
    assert resolve_curl(cfg) == "/usr/bin/curl", "a shim must never be exec'd as curl"

    # When every candidate would re-enter aria2curl we refuse to exec anything
    # rather than loop forever.
    every_candidate_is_self = Config.defaults()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("aria2curl.cli._looks_like_self", lambda path: True)
        patch.setattr("aria2curl.cli._looks_like_shim", lambda path: False)
        with pytest.raises(UsageError) as excinfo:
            resolve_curl(every_candidate_is_self)
    assert "re-enter aria2curl" in str(excinfo.value)
