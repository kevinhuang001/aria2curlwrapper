"""The curl parser: what is translated, what is tolerated, what forces fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from aria2curl import curlparse
from aria2curl.errors import FallbackNeeded

URL = "http://example.test/file.bin"


def parse(args, *, tty=False, implicit="remote-name", **kwargs):
    return curlparse.parse(list(args), stdout_isatty=tty, implicit_output=implicit, **kwargs)


def reason(args, *, tty=False, implicit="remote-name") -> str:
    with pytest.raises(FallbackNeeded) as excinfo:
        parse(args, tty=tty, implicit=implicit)
    return excinfo.value.reason


# ------------------------------------------------------------------ happy path


def test_short_cluster_and_attached_values() -> None:
    cmd = parse(["-sSL", "-oout.bin", URL])
    assert cmd.silent and cmd.show_error and cmd.silent
    assert cmd.transfers[0].output_name == "out.bin"


def test_long_option_forms() -> None:
    same = parse(["--output=out.bin", "--silent", URL])
    spaced = parse(["--output", "out.bin", "--silent", URL])
    assert same.transfers[0].output_name == spaced.transfers[0].output_name == "out.bin"


def test_remote_name_and_header_name() -> None:
    cmd = parse(["-O", URL])
    assert cmd.transfers[0].remote_name
    assert not cmd.transfers[0].remote_header_name
    cmd = parse(["-J", "-O", URL])
    assert cmd.transfers[0].remote_header_name
    assert cmd.remote_header_name


def test_output_to_directory_uses_remote_name() -> None:
    cmd = parse(["-o", "downloads/", URL])
    assert cmd.transfers[0].output_dir == "downloads/"
    assert cmd.transfers[0].remote_name
    assert cmd.transfers[0].output_name is None


def test_output_dir_option_is_joined(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    cmd = parse(["--output-dir", str(base), "-o", "file.bin", URL])
    assert cmd.transfers[0].output_dir == str(base)
    assert cmd.transfers[0].output_name == "file.bin"


def test_resume_forms() -> None:
    assert parse(["-C", "-", "-o", "f", URL]).resume is True
    assert parse(["-C", "0", "-o", "f", URL]).resume is False
    assert parse(["--continue-at", "-", "-o", "f", URL]).resume is True


def test_headers_cookies_and_auth() -> None:
    cmd = parse(["-H", "X-Test: 1", "-H", "Accept:", "-b", "a=1", "-b", "b=2", "-u", "user:pass", URL])
    assert cmd.headers == ["X-Test: 1", "Accept:"]
    assert cmd.cookies_inline == ["a=1", "b=2"]
    assert cmd.user == "user:pass"


def test_cookie_file_detected_by_content(tmp_path: Path) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cmd = parse(["-b", str(jar), URL])
    assert cmd.cookie_file == str(jar)
    assert cmd.cookies_inline == []


def test_missing_cookie_file_is_a_note_not_an_error(tmp_path: Path) -> None:
    cmd = parse(["-b", str(tmp_path / "missing.txt"), URL])
    assert cmd.cookie_file is None
    assert any("does not exist" in note for note in cmd.notes)


def test_basic_auth_becomes_a_preemptive_header() -> None:
    cmd = parse(["--basic", "-u", "user:pass", URL])
    assert cmd.user is None
    assert "Authorization: Basic dXNlcjpwYXNz" in cmd.headers


def test_proxy_forms() -> None:
    assert parse(["-x", "proxy.test:3128", URL]).proxy == "http://proxy.test:3128"
    assert parse(["-x", "socks5://proxy.test:1080", URL]).proxy == "socks5://proxy.test:1080"
    assert parse(["--socks4", "proxy.test:1080", URL]).proxy == "socks4://proxy.test:1080"
    empty = parse(["-x", "", URL])
    assert empty.proxy is None and empty.proxy_explicitly_empty
    assert parse(["--noproxy", "*", URL]).noproxy_all


def test_retry_and_limits() -> None:
    cmd = parse(
        ["--retry", "3", "--retry-delay", "1", "--limit-rate", "500K", "-m", "10", "--connect-timeout", "2.5", URL]
    )
    assert cmd.retry == 3
    assert cmd.retry_delay == 1
    assert cmd.limit_rate == "500K"
    assert cmd.max_time == 10.0
    assert cmd.connect_timeout == 2.5


def test_ip_and_tls_switches() -> None:
    assert parse(["-4", URL]).ipv4
    assert parse(["-6", URL]).ipv6
    assert parse(["-k", URL]).insecure
    assert parse(["--tlsv1.2", URL]).min_tls_version == "TLSv1.2"


def test_globoff_allows_braces() -> None:
    assert parse(["-g", "http://example.test/{a,b}.bin"]).transfers[0].url.endswith("{a,b}.bin")


def test_ipv6_literal_is_not_a_glob() -> None:
    cmd = parse(["http://[::1]:8080/file.bin"])
    assert cmd.transfers[0].url == "http://[::1]:8080/file.bin"


def test_double_dash_ends_options() -> None:
    cmd = parse(["-s", "--", "-weird-url"])
    assert cmd.silent
    assert cmd.transfers[0].url == "-weird-url"


def test_scheme_allowlist() -> None:
    assert parse(["ftp://example.test/f"]).transfers[0].url.startswith("ftp://")
    assert parse(["example.test/f.bin"]).transfers[0].url == "example.test/f.bin"
    for url in ("file:///etc/passwd", "scp://h/f", "imap://h/x", "magnet:?xt=urn:btih:x"):
        assert "scheme" in reason([url]) or "URL" in reason([url])


# ------------------------------------------------------------ implicit output


def test_implicit_output_falls_back_when_stdout_is_piped() -> None:
    assert "stdout" in reason([URL], tty=False, implicit="auto")


def test_implicit_output_uses_remote_name_on_a_tty() -> None:
    cmd = parse([URL], tty=True, implicit="auto")
    assert cmd.transfers[0].remote_name
    assert any("remote file name" in note for note in cmd.notes)


def test_implicit_output_policy_remote_name() -> None:
    assert parse([URL], tty=False, implicit="remote-name").transfers[0].remote_name


def test_implicit_output_policy_fallback() -> None:
    assert "implicit_output" in reason([URL], tty=False, implicit="fallback")


# ------------------------------------------------------------ output pairing


def test_multiple_urls_with_one_remote_name_falls_back() -> None:
    assert "output" in reason(["-O", URL, URL.replace("file", "other")])


def test_remote_name_all_applies_to_every_url() -> None:
    cmd = parse(["--remote-name-all", URL, "http://example.test/other.bin"])
    assert [t.remote_name for t in cmd.transfers] == [True, True]


def test_paired_outputs() -> None:
    cmd = parse(["-o", "one.bin", URL, "-o", "two.bin", "http://example.test/two.bin"])
    assert [t.output_name for t in cmd.transfers] == ["one.bin", "two.bin"]


def test_output_count_mismatch_falls_back() -> None:
    assert "output" in reason(["-o", "one.bin", URL, "http://example.test/two.bin"])


def test_output_to_stdout_falls_back() -> None:
    assert "stdout" in reason(["-o", "-", URL])


# --------------------------------------------------------------- fallback set


@pytest.mark.parametrize(
    "args,fragment",
    [
        (["-X", "POST", URL], "request method"),
        (["-d", "a=1", URL], "request body"),
        (["-F", "file=@x", URL], "multipart"),
        (["-T", "file", URL], "upload"),
        (["-I", URL], "HEAD"),
        (["-G", URL], "query mode"),
        (["-i", URL], "response headers"),
        (["-D", "h.txt", URL], "response headers"),
        (["-w", "%{http_code}", URL], "output format"),
        (["-v", URL], "trace"),
        (["--trace", "t.txt", URL], "trace"),
        (["-r", "0-100", URL], "range"),
        (["-z", "-1h", URL], "conditional"),
        (["-n", URL], "netrc"),
        (["--netrc-file", "f", URL], "netrc"),
        (["-j", URL], "cookie session"),
        (["-K", "cfg", URL], "config file"),
        (["-M"], "manual"),
        (["-a", "-T", "f", URL], "upload"),
        (["-B", URL], "ASCII"),
        (["-l", "ftp://h/"], "listing"),
        (["-Q", "cmd", URL], "FTP commands"),
        (["-P", "-", "ftp://h/f"], "FTP"),
        (["--http2", URL], "HTTP/2"),
        (["--http3", URL], "HTTP/3"),
        (["--http1.1", "-o", "f", URL], "HTTP/1.1"),
        (["-0", "-o", "f", URL], "HTTP/1.0"),
        (["--max-redirs", "3", URL], "redirect"),
        (["--tls-max", "1.2", URL], "tls-max"),
        (["--max-filesize", "10", URL], "size limit"),
        (["--capath", "/etc/ssl", URL], "CA directory"),
        (["--location-trusted", URL], "credentials"),
        (["--socks5-hostname", "p:1", "-o", "f", URL], "remote DNS"),
        (["--socks4a", "p:1", "-o", "f", URL], "remote DNS"),
        (["--preproxy", "p:1", URL], "pre-proxy"),
        (["--netrc-optional", URL], "netrc"),
        (["--resolve", "h:443:1.2.3.4", URL], "remapping"),
        (["--connect-to", "a::b:", URL], "remapping"),
        (["--unix-socket", "/tmp/s", URL], "unix"),
        (["--ciphers", "AES", URL], "cipher"),
        (["--pinnedpubkey", "sha256//x", URL], "pinning"),
        (["--aws-sigv4", "aws:amz", URL], "AWS"),
        (["--oauth2-bearer", "t", URL], "OAuth2"),
        (["--next", URL], "transfer groups"),
        (["--url-query", "a=1", URL], "query"),
        (["--stderr", "f", URL], "stderr"),
        (["--rate", "10/s", URL], "rate"),
        (["--cert-type", "P12", "--cert", "c.p12", URL], "certificate type"),
        (["-e", "http://x;auto", URL], "auto"),
        (["-H", "broken-header", URL], "malformed header"),
        (["-u", "useronly", URL], "interactive"),
        (["-U", "useronly", "-x", "p:1", "-o", "f", URL], "interactive"),
        (["-C", "500", "-o", "f", URL], "byte offset"),
        (["--output-dir", "", URL], "empty path"),
        (["--does-not-exist", URL], "no aria2 equivalent"),
    ],
)
def test_falls_back_for_unsupported_features(args: list[str], fragment: str) -> None:
    assert fragment in reason(args)


def test_unknown_option_mentions_itself() -> None:
    assert "--brand-new-curl-option" in reason(["--brand-new-curl-option", URL])


def test_url_globbing_falls_back() -> None:
    assert "globbing" in reason(["http://example.test/[1-10].bin"])


def test_local_file_path_falls_back(tmp_path: Path) -> None:
    local = tmp_path / "local.bin"
    local.write_bytes(b"x")
    assert "local file" in reason([str(local)])


def test_missing_target_directory_falls_back(tmp_path: Path) -> None:
    target = tmp_path / "definitely-missing" / "out.bin"
    assert "does not exist" in reason(["-o", str(target), URL])


def test_missing_target_directory_is_fine_with_create_dirs(tmp_path: Path) -> None:
    target = tmp_path / "new" / "out.bin"
    cmd = parse(["--create-dirs", "-o", str(target), URL])
    assert cmd.create_dirs
    assert cmd.transfers[0].output_name == "out.bin"


def test_no_url_falls_back() -> None:
    assert "no URL" in reason(["-s"])


def test_missing_option_argument_falls_back() -> None:
    assert "missing its argument" in reason(["-o"])


def test_tolerated_options_are_recorded_as_notes() -> None:
    cmd = parse(["-L", "-N", "--compressed", "-f", "--speed-time", "5", URL])
    assert cmd.compressed
    assert any("speed-time" in note for note in cmd.notes)
