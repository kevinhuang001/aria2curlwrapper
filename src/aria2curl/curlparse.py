"""A curl command-line parser that knows when aria2 can take over.

The parser is deliberately an **allowlist**.  Every curl option is either

* translated to its aria2 equivalent, or
* provably harmless for the aria2 path (``-L``, ``-N``, ``-#`` ...), or
* a reason to hand the original argv to curl untouched.

Anything the table does not mention raises :class:`~aria2curl.errors.FallbackNeeded`,
so an option added to curl tomorrow degrades to plain curl instead of silently
changing behaviour.  That property is the whole point of the wrapper.
"""

from __future__ import annotations

import base64
import math
import os
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Callable, Sequence

from .errors import FallbackNeeded

# --------------------------------------------------------------------------- data


@dataclass
class Transfer:
    """One URL plus the output target curl would have used for it."""

    url: str
    output_dir: str | None = None
    output_name: str | None = None
    remote_name: bool = False
    remote_header_name: bool = False
    to_stdout: bool = False

    @property
    def needs_remote_name(self) -> bool:
        return self.remote_name or self.remote_header_name


@dataclass
class CurlCommand:
    """The parsed subset of a curl invocation that aria2 can express."""

    argv: list[str] = dataclass_field(default_factory=list)
    transfers: list[Transfer] = dataclass_field(default_factory=list)

    headers: list[str] = dataclass_field(default_factory=list)
    cookies_inline: list[str] = dataclass_field(default_factory=list)
    cookie_file: str | None = None
    cookie_jar: str | None = None
    user: str | None = None
    basic_auth: bool = False

    proxy: str | None = None
    proxy_explicitly_empty: bool = False
    proxy_user: str | None = None
    noproxy: str | None = None
    noproxy_all: bool = False

    user_agent: str | None = None
    referer: str | None = None
    insecure: bool = False
    cacert: str | None = None
    cert: str | None = None
    key: str | None = None

    limit_rate: str | None = None
    max_time: float | None = None
    connect_timeout: float | None = None
    speed_limit: str | None = None
    retry: int | None = None
    retry_delay: int | None = None

    resume: bool = False
    ipv4: bool = False
    ipv6: bool = False
    remote_time: bool = False
    create_dirs: bool = False
    compressed: bool = False
    interface: str | None = None
    min_tls_version: str | None = None
    output_dir_option: str | None = None

    silent: bool = False
    show_error: bool = False
    progress_bar: bool = False
    no_progress: bool = False
    disable_config: bool = False
    globbing: bool = True
    parallel: bool = False
    parallel_max: int | None = None
    remote_name_all: bool = False
    remote_header_name: bool = False

    notes: list[str] = dataclass_field(default_factory=list)


# ------------------------------------------------------------------------ helpers

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
_BARE_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_ARIA2_SCHEMES = frozenset({"http", "https", "ftp", "sftp"})
_GLOB_RE = re.compile(r"\{|\]|[^:]\[[^\]]*[-:,][^\]]*\]")
_OUTPUT_GLOB_RE = re.compile(r"#\d")
_TIME_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def url_scheme(url: str) -> str | None:
    """Return the lower-cased URL scheme, or ``None`` when there is none."""
    match = _SCHEME_RE.match(url)
    return match.group(1).lower() if match else None


def looks_globby(url: str) -> bool:
    """True when curl would expand this URL as a glob."""
    if "{" in url:
        return True
    for match in re.finditer(r"\[([^\]]*)\]", url):
        body = match.group(1)
        if ":" in body:  # IPv6 literal, not a range glob
            continue
        if "-" in body or "," in body:
            return True
    return False


def _seconds(value: str, option: str) -> float:
    text = value.strip()
    if not _TIME_RE.match(text):
        raise FallbackNeeded(f"{option} value {value!r} is not a plain number of seconds")
    return float(text)


def _positive_int(value: str, option: str) -> int:
    try:
        return int(value.strip(), 10)
    except (TypeError, ValueError):
        raise FallbackNeeded(f"{option} value {value!r} is not an integer") from None


def ceil_seconds(value: float) -> int:
    return max(1, int(math.ceil(value)))


def split_userinfo(value: str, option: str) -> tuple[str, str]:
    if ":" not in value:
        raise FallbackNeeded(f"{option} without a password would need an interactive prompt")
    user, _, password = value.partition(":")
    return user, password


# ------------------------------------------------------------------ option table


@dataclass(frozen=True)
class Opt:
    name: str
    takes_value: bool
    apply: Callable[["_Parser", str | None], None]


_LONG: dict[str, Opt] = {}
_SHORT: dict[str, Opt] = {}


def _define(
    name: str,
    takes_value: bool,
    apply: Callable[["_Parser", str | None], None],
    *,
    longs: Sequence[str] = (),
    shorts: Sequence[str] = (),
) -> None:
    spec = Opt(name, takes_value, apply)
    for long_name in longs:
        _LONG[f"--{long_name}"] = spec
    for short_name in shorts:
        _SHORT[short_name] = spec


def _store(attr: str, transform: Callable[[str], object] | None = None, note: str | None = None):
    def apply(parser: "_Parser", value: str | None) -> None:
        assert value is not None
        setattr(parser.cmd, attr, transform(value) if transform else value)
        if note:
            parser.note(note)

    return apply


def _flag(attr: str, note: str | None = None):
    def apply(parser: "_Parser", value: str | None) -> None:
        setattr(parser.cmd, attr, True)
        if note:
            parser.note(note)

    return apply


def _ignore(note: str | None = None):
    def apply(parser: "_Parser", value: str | None) -> None:
        if note:
            parser.note(note)

    return apply


def _unsupported(reason: str):
    def apply(parser: "_Parser", value: str | None) -> None:
        raise FallbackNeeded(reason)

    return apply


# ---- output targeting


def _h_output(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    if not value:
        raise FallbackNeeded("-o/--output with an empty name")
    parser.add_target(("output", value))


def _h_remote_name(parser: "_Parser", value: str | None) -> None:
    parser.add_target(("remote", None))


def _h_remote_name_all(parser: "_Parser", value: str | None) -> None:
    parser.cmd.remote_name_all = True


def _h_remote_header_name(parser: "_Parser", value: str | None) -> None:
    parser.cmd.remote_header_name = True


def _h_output_dir(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    parser.cmd.output_dir_option = value


# ---- headers, auth, cookies


def _h_header(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    if ":" not in value and not value.endswith(";"):
        raise FallbackNeeded(f"malformed header {value!r}")
    if value.startswith("@"):
        raise FallbackNeeded("headers read from a file (-H @file)")
    parser.cmd.headers.append(value)


def _h_user(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    user, password = split_userinfo(value, "-u/--user")
    parser.cmd.user = f"{user}:{password}"


def _h_proxy_user(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    user, password = split_userinfo(value, "-U/--proxy-user")
    parser.cmd.proxy_user = f"{user}:{password}"


def _h_basic(parser: "_Parser", value: str | None) -> None:
    parser.cmd.basic_auth = True


def _h_cookie(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    if "=" in value:
        parser.cmd.cookies_inline.append(value)
        return
    path = os.path.expanduser(value)
    if os.path.isfile(path):
        if parser.cmd.cookie_file is not None and parser.cmd.cookie_file != path:
            parser.note(f"-b/--cookie: only {path!r} is used; aria2 accepts one cookie file")
        parser.cmd.cookie_file = path
    else:
        parser.note(f"-b/--cookie: cookie file {value!r} does not exist and was ignored")


def _h_cookie_jar(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    parser.cmd.cookie_jar = value


# ---- proxy / tls / network


def _h_proxy(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    text = value.strip()
    if not text:
        parser.cmd.proxy = None
        parser.cmd.proxy_explicitly_empty = True
        return
    scheme = url_scheme(text) or "http"
    if scheme in ("socks4a", "socks5h"):
        raise FallbackNeeded(f"proxy scheme {scheme}:// (remote DNS) is not supported by aria2")
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise FallbackNeeded(f"proxy scheme {scheme}:// is not supported by aria2")
    parser.cmd.proxy = text if "://" in text else f"{scheme}://{text}"


def _h_noproxy(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    parser.cmd.noproxy = value
    parser.cmd.noproxy_all = value.strip() == "*"


def _h_limit_rate(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    parser.cmd.limit_rate = value


def _h_max_time(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    parser.cmd.max_time = _seconds(value, "-m/--max-time")


def _h_connect_timeout(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    parser.cmd.connect_timeout = _seconds(value, "--connect-timeout")


def _h_retry(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    parser.cmd.retry = _positive_int(value, "--retry")


def _h_cert_type(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    if not value.strip().upper().startswith("PEM"):
        raise FallbackNeeded(f"certificate type {value!r} (aria2 only reads PEM)")


def _h_referer(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    if value.endswith(";auto"):
        raise FallbackNeeded("automatic referer (-e 'URL;auto')")
    parser.cmd.referer = value


def _h_tls_min(version: str):
    def apply(parser: "_Parser", value: str | None) -> None:
        parser.cmd.min_tls_version = version

    return apply


def _h_url(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    parser.add_url(value)


def _h_output_glob_guard(parser: "_Parser", value: str | None) -> None:
    assert value is not None
    if parser.cmd.globbing and _OUTPUT_GLOB_RE.search(value):
        raise FallbackNeeded(f"output name globbing {value!r}")
    _h_output(parser, value)


# ------------------------------------------------------------------- registrations

# Output
_define("output", True, _h_output_glob_guard, longs=("output",), shorts=("o",))
_define("remote-name", False, _h_remote_name, longs=("remote-name",), shorts=("O",))
_define("remote-name-all", False, _h_remote_name_all, longs=("remote-name-all",))
_define("remote-header-name", False, _h_remote_header_name, longs=("remote-header-name",), shorts=("J",))
_define("output-dir", True, _h_output_dir, longs=("output-dir",))
_define("create-dirs", False, _flag("create_dirs"), longs=("create-dirs",))
_define("continue-at", True, lambda p, v: p.set_resume(v), longs=("continue-at",), shorts=("C",))

# Headers / identity
_define("header", True, _h_header, longs=("header",), shorts=("H",))
_define("user", True, _h_user, longs=("user",), shorts=("u",))
_define("basic", False, _h_basic, longs=("basic",))
_define("digest", False, _ignore("--digest: aria2 answers 401 challenges itself"), longs=("digest",))
_define("anyauth", False, _ignore("--anyauth: aria2 answers 401 challenges itself"), longs=("anyauth",))
_define("user-agent", True, _store("user_agent"), longs=("user-agent",), shorts=("A",))
_define("referer", True, _h_referer, longs=("referer",), shorts=("e",))
_define("cookie", True, _h_cookie, longs=("cookie",), shorts=("b",))
_define("cookie-jar", True, _h_cookie_jar, longs=("cookie-jar",), shorts=("c",))

# Transport / proxy
_define("proxy", True, _h_proxy, longs=("proxy",), shorts=("x",))
_define("proxy-user", True, _h_proxy_user, longs=("proxy-user",), shorts=("U",))
_define("noproxy", True, _h_noproxy, longs=("noproxy",))
_define(
    "proxytunnel",
    False,
    _ignore("--proxytunnel: aria2 always tunnels HTTPS over a proxy"),
    longs=("proxytunnel",),
    shorts=("p",),
)
_define("preproxy", True, _unsupported("pre-proxy (--preproxy)"), longs=("preproxy",))
_define("proxy-basic", False, _ignore(), longs=("proxy-basic",))
_define("socks5-basic", False, _ignore(), longs=("socks5-basic",))
_define("socks4", True, lambda p, v: p.set_proxy_scheme(v, "socks4"), longs=("socks4",))
_define("socks5", True, lambda p, v: p.set_proxy_scheme(v, "socks5"), longs=("socks5",))
_define("socks4a", True, _unsupported("--socks4a (remote DNS) is not supported by aria2"), longs=("socks4a",))
_define(
    "socks5-hostname",
    True,
    _unsupported("--socks5-hostname (remote DNS) is not supported by aria2"),
    longs=("socks5-hostname",),
)
_define("interface", True, _store("interface"), longs=("interface",))
_define("ipv4", False, _flag("ipv4"), longs=("ipv4",), shorts=("4",))
_define("ipv6", False, _flag("ipv6"), longs=("ipv6",), shorts=("6",))

# Limits and retries
_define("limit-rate", True, _h_limit_rate, longs=("limit-rate",))
_define("max-time", True, _h_max_time, longs=("max-time",), shorts=("m",))
_define("connect-timeout", True, _h_connect_timeout, longs=("connect-timeout",))
_define("speed-limit", True, _store("speed_limit"), longs=("speed-limit",), shorts=("Y",))
_define(
    "speed-time",
    True,
    _ignore("--speed-time: aria2 uses --lowest-speed-limit with its own sampling window"),
    longs=("speed-time",),
    shorts=("y",),
)
_define("retry", True, _h_retry, longs=("retry",))
_define(
    "retry-delay",
    True,
    lambda p, v: setattr(p.cmd, "retry_delay", _positive_int(v, "--retry-delay")),
    longs=("retry-delay",),
)
_define("retry-connrefused", False, _ignore(), longs=("retry-connrefused",))
_define("retry-all-errors", False, _ignore(), longs=("retry-all-errors",))
_define(
    "max-redirs", True, _unsupported("curl's redirect limit (aria2 always follows redirects)"), longs=("max-redirs",)
)

# TLS
_define("insecure", False, _flag("insecure"), longs=("insecure",), shorts=("k",))
_define("cacert", True, _store("cacert"), longs=("cacert",))
_define("cert", True, _store("cert"), longs=("cert",), shorts=("E",))
_define("key", True, _store("key"), longs=("key",))
_define("cert-type", True, _h_cert_type, longs=("cert-type",))
_define("key-type", True, _h_cert_type, longs=("key-type",))
_define("tlsv1", False, _unsupported("--tlsv1 (TLS 1.0) is below aria2's minimum"), longs=("tlsv1",))
_define("tlsv1.0", False, _unsupported("--tlsv1.0 is below aria2's minimum"), longs=("tlsv1.0",))
_define("tlsv1.1", False, _h_tls_min("TLSv1.1"), longs=("tlsv1.1",))
_define("tlsv1.2", False, _h_tls_min("TLSv1.2"), longs=("tlsv1.2",))
_define("tlsv1.3", False, _h_tls_min("TLSv1.3"), longs=("tlsv1.3",))
_define("tls-max", True, _unsupported("--tls-max (aria2 only has --min-tls-version)"), longs=("tls-max",))

# Protocol version
_define(
    "http1.0", False, _unsupported("pinning HTTP/1.0 (aria2 has no --http-version)"), longs=("http1.0",), shorts=("0",)
)
_define("http1.1", False, _unsupported("pinning HTTP/1.1 (aria2 has no --http-version)"), longs=("http1.1",))
_define("http2", False, _unsupported("HTTP/2 (aria2 speaks HTTP/1.1)"), longs=("http2",))
_define("http2-prior-knowledge", False, _unsupported("--http2-prior-knowledge"), longs=("http2-prior-knowledge",))
_define("http3", False, _unsupported("HTTP/3"), longs=("http3",))
_define("http3-only", False, _unsupported("HTTP/3"), longs=("http3-only",))

# Behaviour
_define("location", False, _ignore(), longs=("location",), shorts=("L",))
_define(
    "location-trusted",
    False,
    _unsupported("--location-trusted sends credentials across hosts"),
    longs=("location-trusted",),
)
_define("compressed", False, _flag("compressed"), longs=("compressed",))
_define("tr-encoding", False, _flag("compressed"), longs=("tr-encoding",))
_define("remote-time", False, _flag("remote_time"), longs=("remote-time",), shorts=("R",))
_define("fail", False, _ignore("--fail: aria2 already fails on HTTP 4xx/5xx"), longs=("fail",), shorts=("f",))
_define("parallel", False, _flag("parallel"), longs=("parallel",), shorts=("Z",))
_define(
    "parallel-max",
    True,
    lambda p, v: setattr(p.cmd, "parallel_max", _positive_int(v, "--parallel-max")),
    longs=("parallel-max",),
)
_define("globoff", False, lambda p, v: setattr(p.cmd, "globbing", False), longs=("globoff",), shorts=("g",))
_define("url", True, _h_url, longs=("url",))

# Display-ish
_define("silent", False, _flag("silent"), longs=("silent",), shorts=("s",))
_define("show-error", False, _flag("show_error"), longs=("show-error",), shorts=("S",))
_define("progress-bar", False, _flag("progress_bar"), longs=("progress-bar",), shorts=("#",))
_define("no-progress-meter", False, _flag("no_progress"), longs=("no-progress-meter",))
_define("disable", False, _flag("disable_config"), longs=("disable",), shorts=("q",))
_define("no-buffer", False, _ignore(), longs=("no-buffer",), shorts=("N",))
_define("no-keepalive", False, _ignore(), longs=("no-keepalive",))
_define("keepalive-time", True, _ignore(), longs=("keepalive-time",))
_define("tcp-nodelay", False, _ignore(), longs=("tcp-nodelay",))
_define("no-tcp-nodelay", False, _ignore(), longs=("no-tcp-nodelay",))
_define("happy-eyeballs-timeout-ms", True, _ignore(), longs=("happy-eyeballs-timeout-ms",))
_define("styled-output", False, _ignore(), longs=("styled-output",))
_define("no-styled-output", False, _ignore(), longs=("no-styled-output",))
_define("path-as-is", False, _ignore(), longs=("path-as-is",))
_define("alt-svc", True, _ignore(), longs=("alt-svc",))
_define("expect100-timeout", True, _ignore(), longs=("expect100-timeout",))
_define("ftp-pasv", False, _ignore(), longs=("ftp-pasv",))
_define("ftp-create-dirs", False, _ignore(), longs=("ftp-create-dirs",))
_define("disable-eprt", False, _ignore(), longs=("disable-eprt",))
_define("disable-epsv", False, _ignore(), longs=("disable-epsv",))
_define("ur", False, _ignore(), longs=("ur",))

# Explicitly refused: these change what curl *does*, not just how fast it goes.
_KNOWN_UNSUPPORTED: dict[str, str] = {
    "--request": "custom request method (-X/--request)",
    "--head": "HEAD request (-I/--head)",
    "--get": "query mode (-G/--get)",
    "--data": "request body (-d/--data)",
    "--data-raw": "request body (--data-raw)",
    "--data-binary": "request body (--data-binary)",
    "--data-urlencode": "request body (--data-urlencode)",
    "--data-ascii": "request body (--data-ascii)",
    "--json": "JSON request body (--json)",
    "--form": "multipart upload (-F/--form)",
    "--form-string": "multipart upload (--form-string)",
    "--upload-file": "upload (-T/--upload-file)",
    "--append": "upload append (-a/--append)",
    "--range": "byte ranges (-r/--range)",
    "--time-cond": "conditional transfer (-z/--time-cond)",
    "--etag-compare": "conditional transfer (--etag-compare)",
    "--etag-save": "conditional transfer (--etag-save)",
    "--max-filesize": "size limit (--max-filesize)",
    "--include": "response headers in output (-i/--include)",
    "--dump-header": "response headers in output (-D/--dump-header)",
    "--write-out": "custom output format (-w/--write-out)",
    "--verbose": "verbose request trace (-v/--verbose)",
    "--trace": "request trace (--trace)",
    "--trace-ascii": "request trace (--trace-ascii)",
    "--config": "curl config file (-K/--config)",
    "--next": "multiple transfer groups (-:/--next)",
    "--url-query": "URL query helpers (--url-query)",
    "--netrc": "netrc credentials (-n/--netrc)",
    "--netrc-file": "netrc credentials (--netrc-file)",
    "--netrc-optional": "netrc credentials (--netrc-optional)",
    "--ntlm": "NTLM authentication",
    "--negotiate": "SPNEGO authentication",
    "--proxy-ntlm": "NTLM proxy authentication",
    "--proxy-negotiate": "SPNEGO proxy authentication",
    "--junk-session-cookies": "cookie session filtering (-j/--junk-session-cookies)",
    "--capath": "CA directory (aria2 needs a single PEM bundle)",
    "--raw": "raw transfer (--raw)",
    "--resolve": "host remapping (--resolve)",
    "--connect-to": "host remapping (--connect-to)",
    "--unix-socket": "unix domain sockets",
    "--abstract-unix-socket": "unix domain sockets",
    "--haproxy-protocol": "PROXY protocol",
    "--rate": "request rate limiting (--rate)",
    "--limit-rate-upload": "upload rate limiting",
    "--stderr": "stderr redirection",
    "--interface-name": "interface selection",
    "--local-port": "local port range",
    "--ciphers": "cipher selection",
    "--tls13-ciphers": "cipher selection",
    "--pinnedpubkey": "public key pinning",
    "--cert-status": "OCSP stapling checks",
    "--pass": "private key passphrase",
    "--proxy-insecure": "proxy certificate verification",
    "--proxy-cacert": "proxy CA configuration",
    "--proxy-capath": "proxy CA configuration",
    "--preproxy": "pre-proxy",
    "--proxy1.0": "HTTP/1.0 proxy",
    "--proxy-digest": "proxy digest authentication",
    "--proxy-anyauth": "proxy auth negotiation",
    "--dns-servers": "custom DNS servers",
    "--dns-interface": "custom DNS interface",
    "--dns-ipv4-addr": "custom DNS address",
    "--dns-ipv6-addr": "custom DNS address",
    "--ftp-port": "active FTP",
    "--ftp-method": "FTP method selection",
    "--ftp-account": "FTP account",
    "--ftp-ssl-ccc": "FTP CCC",
    "--ftp-pret": "FTP PRET",
    "--ssl-reqd": "mandatory TLS upgrade",
    "--ssl-no-revoke": "certificate revocation settings",
    "--ssl-allow-beast": "TLS BEAST workaround",
    "--aws-sigv4": "AWS signature v4",
    "--oauth2-bearer": "OAuth2 bearer token",
    "--sasl-ir": "SASL initial response",
    "--delegation": "GSSAPI delegation",
    "--krb": "Kerberos",
    "--mail-from": "mail protocols",
    "--mail-rcpt": "mail protocols",
    "--list-only": "FTP listing",
    "--quote": "FTP commands",
    "--telnet-option": "telnet options",
    "--manual": "manual page",
    "--libcurl": "libcurl source output",
}

# Short options whose long name carries the human explanation above.
_KNOWN_UNSUPPORTED_SHORT: dict[str, str] = {
    "-X": "custom request method (-X/--request)",
    "-d": "request body (-d/--data)",
    "-F": "multipart upload (-F/--form)",
    "-T": "upload (-T/--upload-file)",
    "-I": "HEAD request (-I/--head)",
    "-G": "query mode (-G/--get)",
    "-i": "response headers in output (-i/--include)",
    "-D": "response headers in output (-D/--dump-header)",
    "-w": "custom output format (-w/--write-out)",
    "-v": "verbose request trace (-v/--verbose)",
    "-r": "byte ranges (-r/--range)",
    "-z": "conditional transfer (-z/--time-cond)",
    "-n": "netrc credentials (-n/--netrc)",
    "-j": "cookie session filtering (-j/--junk-session-cookies)",
    "-K": "curl config file (-K/--config)",
    "-M": "manual page (-M/--manual)",
    "-a": "upload append (-a/--append)",
    "-B": "ASCII transfer mode (-B/--use-ascii)",
    "-l": "FTP listing (-l/--list-only)",
    "-Q": "FTP commands (-Q/--quote)",
    "-P": "active FTP (-P/--ftp-port)",
    "-t": "telnet options (-t/--telnet-option)",
    "-:": "multiple transfer groups (-:/--next)",
}


# ------------------------------------------------------------------------- parser


class _Parser:
    """Single-use curl command-line parser."""

    def __init__(self, *, stdout_isatty: bool, implicit_output: str = "auto") -> None:
        self.cmd = CurlCommand()
        self.stdout_isatty = stdout_isatty
        self.implicit_output = implicit_output
        self._urls: list[str] = []
        self._targets: list[tuple[str, str | None]] = []

    # -------------------------------------------------------------- small helpers
    def note(self, text: str) -> None:
        if text not in self.cmd.notes:
            self.cmd.notes.append(text)

    def add_url(self, url: str) -> None:
        if not url or any(ch.isspace() for ch in url):
            raise FallbackNeeded(f"cannot handle URL {url!r}")
        self._urls.append(url)

    def add_target(self, target: tuple[str, str | None]) -> None:
        self._targets.append(target)

    def set_resume(self, value: str | None) -> None:
        assert value is not None
        text = value.strip()
        if text == "-":
            self.cmd.resume = True
            return
        offset = _positive_int(text, "-C/--continue-at")
        if offset == 0:
            self.cmd.resume = False
            return
        raise FallbackNeeded(f"resuming at a byte offset (-C {offset}); aria2 only resumes at the current file size")

    def set_proxy_scheme(self, value: str | None, scheme: str) -> None:
        assert value is not None
        text = value.strip()
        if not text:
            raise FallbackNeeded(f"--{scheme} with an empty host")
        self.cmd.proxy = text if "://" in text else f"{scheme}://{text}"

    # -------------------------------------------------------------------- parsing
    def parse(self, argv: Sequence[str]) -> CurlCommand:
        self.cmd.argv = list(argv)
        index = 0
        while index < len(argv):
            arg = argv[index]
            if arg == "--":
                for rest in argv[index + 1 :]:
                    self.add_url(rest)
                break
            if arg.startswith("--"):
                index = self._parse_long(argv, index)
            elif arg.startswith("-") and arg != "-":
                index = self._parse_short(argv, index)
            else:
                self.add_url(arg)
            index += 1
        self._finalize()
        return self.cmd

    def _parse_long(self, argv: Sequence[str], index: int) -> int:
        name, sep, inline = argv[index].partition("=")
        spec = _LONG.get(name)
        if spec is None:
            reason = _KNOWN_UNSUPPORTED.get(name)
            raise FallbackNeeded(reason or f"curl option {name} has no aria2 equivalent")
        if spec.takes_value:
            if sep:
                value = inline
            else:
                index += 1
                if index >= len(argv):
                    raise FallbackNeeded(f"option {name} is missing its argument")
                value = argv[index]
            spec.apply(self, value)
        else:
            if sep:
                raise FallbackNeeded(f"option {name} does not take a value")
            spec.apply(self, None)
        return index

    def _parse_short(self, argv: Sequence[str], index: int) -> int:
        cluster = argv[index][1:]
        position = 0
        while position < len(cluster):
            char = cluster[position]
            spec = _SHORT.get(char)
            if spec is None:
                reason = _KNOWN_UNSUPPORTED_SHORT.get(f"-{char}")
                raise FallbackNeeded(reason or f"curl option -{char} has no aria2 equivalent")
            if spec.takes_value:
                remainder = cluster[position + 1 :]
                if remainder:
                    value = remainder
                else:
                    index += 1
                    if index >= len(argv):
                        raise FallbackNeeded(f"option -{char} is missing its argument")
                    value = argv[index]
                spec.apply(self, value)
                return index
            spec.apply(self, None)
            position += 1
        return index

    # ------------------------------------------------------------------- finalize
    def _finalize(self) -> None:
        if not self._urls:
            raise FallbackNeeded("no URL on the command line")
        if self.cmd.output_dir_option == "":
            raise FallbackNeeded("--output-dir with an empty path")

        self._check_urls()
        self._pair_targets()
        self._check_local_conditions()
        if self.cmd.remote_header_name:
            for transfer in self.cmd.transfers:
                if transfer.needs_remote_name:
                    transfer.remote_header_name = True
        if self.cmd.basic_auth and self.cmd.user:
            user, _, password = self.cmd.user.partition(":")
            token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            self.cmd.headers.append(f"Authorization: Basic {token}")
            self.cmd.user = None
            self.note("--basic: credentials sent as a preemptive Authorization header")

    def _check_urls(self) -> None:
        for url in self._urls:
            scheme = url_scheme(url)
            if scheme is None:
                if _BARE_SCHEME_RE.match(url) or "://" in url:
                    raise FallbackNeeded(f"unsupported URL scheme in {url!r}")
                if os.path.exists(url):
                    raise FallbackNeeded(f"{url!r} looks like a local file, not a URL")
                continue
            if scheme not in _ARIA2_SCHEMES:
                raise FallbackNeeded(f"URL scheme {scheme}:// is not handled by aria2")
            if self.cmd.globbing and looks_globby(url):
                raise FallbackNeeded("curl URL globbing ({} / [])")

    def _pair_targets(self) -> None:
        urls = self._urls
        targets = list(self._targets)
        if self.cmd.remote_name_all:
            if len(targets) > len(urls):
                raise FallbackNeeded("more output targets than URLs")
        elif len(targets) == 0:
            targets = [("implicit", None)] * len(urls)
        elif len(targets) != len(urls):
            raise FallbackNeeded(
                f"{len(targets)} output option(s) for {len(urls)} URL(s); curl would mix file output with stdout"
            )

        transfers: list[Transfer] = []
        for position, url in enumerate(urls):
            target = targets[position] if position < len(targets) else ("remote", None)
            transfers.append(self._build_transfer(url, target))
        self.cmd.transfers = transfers

    def _build_transfer(self, url: str, target: tuple[str, str | None]) -> Transfer:
        kind, value = target
        transfer = Transfer(url=url)
        if kind == "remote":
            transfer.remote_name = True
        elif kind == "output":
            assert value is not None
            if value == "-":
                transfer.to_stdout = True
            else:
                path = value
                if path.endswith(("/", os.sep)) or os.path.isdir(path):
                    transfer.output_dir = path
                    transfer.remote_name = True
                else:
                    directory, name = os.path.split(path)
                    transfer.output_dir = directory or None
                    transfer.output_name = name
        else:  # implicit
            if self.implicit_output == "fallback":
                raise FallbackNeeded("no -o/-O given and implicit_output is 'fallback'")
            if self.implicit_output == "remote-name" or self.stdout_isatty:
                transfer.remote_name = True
                self.note("no -o/-O: saving under the remote file name")
            else:
                raise FallbackNeeded("no -o/-O given and stdout is not a terminal")

        if transfer.to_stdout:
            raise FallbackNeeded("writing the download to stdout (-o -)")

        if self.cmd.output_dir_option:
            base = self.cmd.output_dir_option
            if transfer.output_dir:
                transfer.output_dir = os.path.normpath(os.path.join(base, transfer.output_dir))
            elif transfer.output_name:
                transfer.output_dir = base
            else:
                transfer.output_dir = base
        return transfer

    def _check_local_conditions(self) -> None:
        for transfer in self.cmd.transfers:
            if transfer.output_name is None:
                continue
            directory = transfer.output_dir or "."
            if not os.path.isdir(directory) and not self.cmd.create_dirs:
                raise FallbackNeeded(f"target directory {directory!r} does not exist (curl would report a write error)")


def parse(
    argv: Sequence[str],
    *,
    stdout_isatty: bool,
    implicit_output: str = "auto",
) -> CurlCommand:
    """Parse curl arguments, raising :class:`FallbackNeeded` when aria2 cannot help."""
    return _Parser(stdout_isatty=stdout_isatty, implicit_output=implicit_output).parse(argv)
