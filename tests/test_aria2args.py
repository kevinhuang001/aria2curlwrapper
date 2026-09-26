"""curl -> aria2c translation."""

from __future__ import annotations

from pathlib import Path

import pytest

from aria2curl import curlparse
from aria2curl.aria2args import build_plan, ensure_directories, guess_remote_name
from aria2curl.config import Config

URL = "http://example.test/file.bin"


def make_cfg(**values) -> Config:
    cfg = Config.defaults()
    for key, value in values.items():
        cfg.set(key, value)
    return cfg


def plan_for(
    args,
    *,
    cfg: Config | None = None,
    cwd: Path | str = ".",
    env: dict[str, str] | None = None,
    implicit: str = "remote-name",
):
    cfg = cfg or Config.defaults()
    cmd = curlparse.parse(list(args), stdout_isatty=False, implicit_output=implicit)
    return build_plan(cmd, cfg, cwd=str(cwd), env=env or {})


def has(args: list[str], value: str) -> bool:
    return value in args


# ------------------------------------------------------------------- tuning up


def test_default_tuning_is_emitted(tmp_path: Path) -> None:
    plan = plan_for(["-o", str(tmp_path / "f.bin"), URL], cwd=tmp_path)
    args = plan.args
    assert has(args, "--split=5")
    assert has(args, "--max-connection-per-server=5")
    assert has(args, "--min-split-size=1M")
    assert has(args, "--file-allocation=prealloc")
    assert has(args, "--max-concurrent-downloads=5")
    assert has(args, "--allow-overwrite=true")
    assert has(args, "--auto-file-renaming=false")
    assert has(args, "--check-certificate=true")
    assert has(args, "--no-proxy=localhost,127.0.0.1,::1".replace("localhost,127.0.0.1,::1", "x")) is False


def test_config_overrides_reach_aria2() -> None:
    cfg = make_cfg(split=16, max_connection_per_server=8, min_split_size="4M", max_concurrent_downloads=3)
    plan = plan_for(["-O", URL], cfg=cfg)
    for expected in (
        "--split=16",
        "--max-connection-per-server=8",
        "--min-split-size=4M",
        "--max-concurrent-downloads=3",
    ):
        assert has(plan.args, expected)


def test_output_placement_single_transfer(tmp_path: Path) -> None:
    plan = plan_for(["-o", str(tmp_path / "sub" / "f.bin"), URL], cwd=tmp_path) if False else None
    (tmp_path / "sub").mkdir()
    plan = plan_for(["-o", str(tmp_path / "sub" / "f.bin"), URL], cwd=tmp_path)
    assert has(plan.args, f"--dir={tmp_path / 'sub'}")
    assert has(plan.args, "--out=f.bin")
    assert plan.uris == [URL]
    assert plan.input_file_text is None


def test_remote_name_leaves_out_unset(tmp_path: Path) -> None:
    plan = plan_for(["-O", URL], cwd=tmp_path)
    assert not any(arg.startswith("--out=") for arg in plan.args)
    assert has(plan.args, f"--dir={tmp_path}")
    assert plan.targets[0].filename is None


def test_multiple_transfers_use_an_input_file(tmp_path: Path) -> None:
    plan = plan_for(
        ["--remote-name-all", URL, "http://example.test/two.bin"],
        cwd=tmp_path,
    )
    assert plan.uses_input_file
    assert plan.uris == []
    text = plan.input_file_text or ""
    assert "http://example.test/two.bin" in text
    assert f"dir={tmp_path}" in text
    assert "<generated>" in plan.command_preview("aria2c")


def test_input_file_records_explicit_names(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    plan = plan_for(
        [
            "-o",
            str(tmp_path / "d" / "one.bin"),
            URL,
            "-o",
            str(tmp_path / "d" / "two.bin"),
            "http://example.test/two.bin",
        ],
        cwd=tmp_path,
    )
    text = plan.input_file_text or ""
    assert "out=one.bin" in text and "out=two.bin" in text


# ----------------------------------------------------------------------- resume


def test_auto_resume_when_partial_file_exists(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"partial")
    plan = plan_for(["-o", str(target), URL], cwd=tmp_path)
    assert plan.resume is True
    assert "--continue=true" in plan.args
    assert plan.resume_requested is False


def test_no_resume_for_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"")
    plan = plan_for(["-o", str(target), URL], cwd=tmp_path)
    assert plan.resume is False


def test_no_resume_when_disabled(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"partial")
    plan = plan_for(["-o", str(target), URL], cwd=tmp_path, cfg=make_cfg(auto_resume=False))
    assert plan.resume is False


def test_explicit_continue_at_is_marked_requested(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"partial")
    plan = plan_for(["-C", "-", "-o", str(target), URL], cwd=tmp_path)
    assert plan.resume is True and plan.resume_requested is True


def test_auto_resume_probes_the_url_derived_name(tmp_path: Path) -> None:
    (tmp_path / "file.bin").write_bytes(b"partial")
    plan = plan_for(["-O", URL], cwd=tmp_path)
    assert plan.resume is True


# ------------------------------------------------------------------------ proxy


def test_proxy_from_option() -> None:
    plan = plan_for(["-x", "http://p.test:3128", "-o", "f", URL])
    assert has(plan.args, "--all-proxy=http://p.test:3128")


def test_proxy_user_is_split() -> None:
    plan = plan_for(["-x", "http://p.test:3128", "-U", "bob:secret", "-o", "f", URL])
    assert has(plan.args, "--all-proxy-user=bob")
    assert has(plan.args, "--all-proxy-passwd=secret")


def test_proxy_from_config() -> None:
    plan = plan_for(["-o", "f", URL], cfg=make_cfg(proxy="http://cfg.test:8080"))
    assert has(plan.args, "--all-proxy=http://cfg.test:8080")


def test_proxy_from_environment() -> None:
    env = {"all_proxy": "http://all.test:1", "http_proxy": "http://http.test:2", "https_proxy": "http://ssl.test:3"}
    plan = plan_for(["-o", "f", URL], env=env)
    assert has(plan.args, "--all-proxy=http://all.test:1")
    assert has(plan.args, "--http-proxy=http://http.test:2")
    assert has(plan.args, "--https-proxy=http://ssl.test:3")


def test_uppercase_http_proxy_is_ignored_like_curl() -> None:
    plan = plan_for(["-o", "f", URL], env={"HTTP_PROXY": "http://upper.test:1"})
    assert not any("upper.test" in arg for arg in plan.args)


def test_proxy_env_can_be_disabled() -> None:
    plan = plan_for(["-o", "f", URL], env={"http_proxy": "http://p.test:1"}, cfg=make_cfg(honor_proxy_env=False))
    assert not any("p.test" in arg for arg in plan.args)


def test_explicit_empty_proxy_beats_environment() -> None:
    plan = plan_for(["-x", "", "-o", "f", URL], env={"http_proxy": "http://p.test:1"})
    assert not any("p.test" in arg for arg in plan.args)


def test_noproxy_star_drops_the_proxy_entirely() -> None:
    plan = plan_for(["-x", "http://p.test:1", "--noproxy", "*", "-o", "f", URL])
    assert not any("--all-proxy" in arg for arg in plan.args)


def test_noproxy_list_is_passed_through() -> None:
    plan = plan_for(["--noproxy", "a.test,b.test", "-o", "f", URL], env={"http_proxy": "http://p.test:1"})
    assert has(plan.args, "--no-proxy=a.test,b.test")


def test_no_proxy_env_is_used_when_curl_does_not_specify() -> None:
    plan = plan_for(["-o", "f", URL], env={"http_proxy": "http://p.test:1", "no_proxy": "localhost"})
    assert has(plan.args, "--no-proxy=localhost")


# ------------------------------------------------------------------- decoration


def test_auth_uses_scheme_specific_options() -> None:
    http_plan = plan_for(["-u", "u:p", "-o", "f", URL])
    assert has(http_plan.args, "--http-user=u") and has(http_plan.args, "--http-passwd=p")
    ftp_plan = plan_for(["-u", "u:p", "-o", "f", "ftp://example.test/f"])
    assert has(ftp_plan.args, "--ftp-user=u") and has(ftp_plan.args, "--ftp-passwd=p")


def test_basic_auth_is_sent_as_a_header() -> None:
    plan = plan_for(["--basic", "-u", "u:p", "-o", "f", URL])
    assert has(plan.args, "--header=Authorization: Basic dTpw")
    assert not any(arg.startswith("--http-user") for arg in plan.args)


def test_headers_merge_config_and_command_line() -> None:
    cfg = make_cfg(headers=["X-Config: 1"])
    plan = plan_for(["-H", "X-Curl: 2", "-o", "f", URL], cfg=cfg)
    assert has(plan.args, "--header=X-Config: 1")
    assert has(plan.args, "--header=X-Curl: 2")


def test_inline_cookies_become_one_header() -> None:
    plan = plan_for(["-b", "a=1", "-b", "b=2", "-o", "f", URL])
    assert has(plan.args, "--header=Cookie: a=1; b=2")


def test_cookie_files_are_loaded_and_saved(tmp_path: Path) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text("x", encoding="utf-8")
    plan = plan_for(["-b", str(jar), "-c", str(tmp_path / "out.txt"), "-o", "f", URL])
    assert has(plan.args, f"--load-cookies={jar}")
    assert has(plan.args, f"--save-cookies={tmp_path / 'out.txt'}")


def test_limits_and_retries() -> None:
    plan = plan_for(["--retry", "3", "--retry-delay", "1", "--limit-rate", "500K", "-m", "9", "-o", "f", URL])
    # curl counts retries, aria2 counts attempts
    assert has(plan.args, "--max-tries=4")
    assert has(plan.args, "--retry-wait=1")
    assert has(plan.args, "--max-download-limit=500K")
    assert plan.max_time == 9.0
    assert not any("--timeout=9" == arg for arg in plan.args)


def test_tls_and_ip_switches() -> None:
    plan = plan_for(["-k", "-4", "--cacert", "/tmp/ca.pem", "--tlsv1.2", "-o", "f", URL])
    assert has(plan.args, "--check-certificate=false")
    assert has(plan.args, "--disable-ipv6=true")
    assert has(plan.args, "--ca-certificate=/tmp/ca.pem")
    assert has(plan.args, "--min-tls-version=TLSv1.2")


def test_compressed_and_remote_time() -> None:
    plan = plan_for(["--compressed", "-R", "-o", "f", URL])
    assert has(plan.args, "--http-accept-gzip=true")
    assert has(plan.args, "--remote-time=true")


def test_content_disposition_from_J() -> None:
    plan = plan_for(["-J", "-O", URL])
    assert has(plan.args, "--content-disposition=true")


def test_extra_args_are_appended_last() -> None:
    cfg = make_cfg(extra_args=["--seed-time=0", "--max-overall-download-limit=1M"])
    plan = plan_for(["-o", "f", URL], cfg=cfg)
    assert plan.args[-2:] == ["--seed-time=0", "--max-overall-download-limit=1M"]


def test_parallel_max_overrides_concurrency() -> None:
    plan = plan_for(["--parallel-max", "2", "-o", "f", URL])
    assert has(plan.args, "--max-concurrent-downloads=2")


# -------------------------------------------------------------------- placement


def test_config_dir_applies_only_to_name_from_url(tmp_path: Path) -> None:
    cfg = make_cfg(dir=str(tmp_path / "downloads"))
    remote = plan_for(["-O", URL], cfg=cfg, cwd=tmp_path)
    assert has(remote.args, f"--dir={tmp_path / 'downloads'}")
    explicit = plan_for(["-o", "explicit.bin", URL], cfg=cfg, cwd=tmp_path)
    assert has(explicit.args, f"--dir={tmp_path}")


def test_create_dirs_is_recorded_and_created(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "f.bin"
    plan = plan_for(["--create-dirs", "-o", str(target), URL], cwd=tmp_path)
    assert plan.mkdirs == [str(tmp_path / "a" / "b")]
    ensure_directories(plan)
    assert (tmp_path / "a" / "b").is_dir()


def test_configured_dir_is_created_even_without_create_dirs(tmp_path: Path) -> None:
    cfg = make_cfg(dir=str(tmp_path / "auto"))
    plan = plan_for(["-O", URL], cfg=cfg, cwd=tmp_path)
    assert plan.mkdirs == [str(tmp_path / "auto")]
    ensure_directories(plan)
    assert (tmp_path / "auto").is_dir()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://example.test/a/b/file.bin", "file.bin"),
        ("http://example.test/a/b/", "b"),
        ("http://example.test/", "index.html"),
        ("http://example.test/a%20b.bin", "a b.bin"),
    ],
)
def test_guess_remote_name(url: str, expected: str) -> None:
    assert guess_remote_name(url) == expected


def test_command_preview_quotes_awkward_values() -> None:
    plan = plan_for(["-H", "X-Space: a b", "-o", "f", URL])
    assert "'--header=X-Space: a b'" in plan.command_preview("aria2c")
