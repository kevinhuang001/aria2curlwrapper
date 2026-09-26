"""Translate a parsed curl command plus the global config into an aria2c plan.

The plan is deliberately data, not a process: :func:`build_plan` returns the
argv, the optional per-download input file and the resolved output targets, so
the whole translation can be inspected (``--acurl-dry-run``) and unit tested
without starting anything.
"""

from __future__ import annotations

import os
import posixpath
import urllib.parse
from dataclasses import dataclass, field as dataclass_field
from typing import Sequence

from .config import Config
from .curlparse import CurlCommand, Transfer, ceil_seconds, url_scheme
from .errors import FallbackNeeded

# aria2 options that only accept a single occurrence in an input file entry.
_INPUT_FILE_SAFE = frozenset({"out", "dir"})


@dataclass
class Target:
    """Where one download is expected to land."""

    url: str
    directory: str
    filename: str | None  # None = let aria2 derive it from the URL
    display: str

    @property
    def expected_path(self) -> str | None:
        if self.filename is None:
            return None
        return os.path.join(self.directory, self.filename)


@dataclass
class Aria2Plan:
    """Everything the runner needs in order to supervise one aria2c process."""

    args: list[str] = dataclass_field(default_factory=list)
    uris: list[str] = dataclass_field(default_factory=list)
    input_file_text: str | None = None
    targets: list[Target] = dataclass_field(default_factory=list)
    mkdirs: list[str] = dataclass_field(default_factory=list)
    resume: bool = False
    resume_requested: bool = False
    max_time: float | None = None
    notes: list[str] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)

    @property
    def uses_input_file(self) -> bool:
        return self.input_file_text is not None

    def command_preview(self, aria2_path: str) -> str:
        """A copy/pasteable representation of the aria2c call."""
        parts = [aria2_path, *self.args]
        if self.uses_input_file:
            parts.insert(1, "--input-file=<generated>")
        else:
            parts.extend(self.uris)
        return " ".join(_quote(part) for part in parts)


def _quote(text: str) -> str:
    if text and all(ch.isalnum() or ch in "-_./=:@,+" for ch in text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def guess_remote_name(url: str) -> str:
    """The file name curl's ``-O`` would pick for ``url``."""
    path = urllib.parse.urlsplit(url).path
    name = posixpath.basename(path.rstrip("/"))
    name = urllib.parse.unquote(name)
    return name or "index.html"


def _flag(args: list[str], name: str, value: bool) -> None:
    args.append(f"--{name}={'true' if value else 'false'}")


def _opt(args: list[str], name: str, value: object) -> None:
    args.append(f"--{name}={value}")


def _first_env(env: dict[str, str], names: Sequence[str]) -> str | None:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return None


def _proxy_args(cmd: CurlCommand, cfg: Config, env: dict[str, str]) -> list[str]:
    """Reproduce curl's proxy resolution (options, then environment)."""
    args: list[str] = []
    if cmd.noproxy_all:
        return args

    proxy_user = cmd.proxy_user
    if cmd.proxy is not None:
        _opt(args, "all-proxy", cmd.proxy)
    elif cmd.proxy_explicitly_empty:
        # curl semantics: -x "" disables proxying entirely, including env vars.
        return args
    elif cfg["proxy"]:
        _opt(args, "all-proxy", cfg["proxy"])
    elif cfg["honor_proxy_env"]:
        all_proxy = _first_env(env, ("all_proxy", "ALL_PROXY"))
        if all_proxy:
            _opt(args, "all-proxy", all_proxy)
        http_proxy = _first_env(env, ("http_proxy",))
        if http_proxy:
            _opt(args, "http-proxy", http_proxy)
        https_proxy = _first_env(env, ("https_proxy", "HTTPS_PROXY"))
        if https_proxy:
            _opt(args, "https-proxy", https_proxy)
        ftp_proxy = _first_env(env, ("ftp_proxy", "FTP_PROXY"))
        if ftp_proxy:
            _opt(args, "ftp-proxy", ftp_proxy)

    if args and proxy_user:
        user, _, password = proxy_user.partition(":")
        _opt(args, "all-proxy-user", user)
        _opt(args, "all-proxy-passwd", password)

    noproxy = cmd.noproxy
    if noproxy is None:
        noproxy = cfg["no_proxy"] or (_first_env(env, ("no_proxy", "NO_PROXY")) if cfg["honor_proxy_env"] else None)
    if noproxy:
        if noproxy.strip() == "*":
            # "never proxy anything" -- dropping the proxy options achieves it.
            return [
                a
                for a in args
                if not a.startswith("--all-proxy")
                and not a.startswith("--http-proxy")
                and not a.startswith("--https-proxy")
                and not a.startswith("--ftp-proxy")
            ]
        _opt(args, "no-proxy", noproxy)
    return args


def _auth_args(cmd: CurlCommand) -> list[str]:
    if not cmd.user:
        return []
    user, _, password = cmd.user.partition(":")
    schemes = {url_scheme(t.url) or "http" for t in cmd.transfers}
    args: list[str] = []
    if schemes & {"http", "https"}:
        _opt(args, "http-user", user)
        _opt(args, "http-passwd", password)
    if "ftp" in schemes:
        _opt(args, "ftp-user", user)
        _opt(args, "ftp-passwd", password)
    if "sftp" in schemes:
        _opt(args, "ftp-user", user)
        _opt(args, "ftp-passwd", password)
    return args


def _resolve_base(transfer: Transfer, cfg: Config, cwd: str) -> str:
    if transfer.output_dir is not None:
        base = transfer.output_dir
        return base if os.path.isabs(base) else os.path.abspath(os.path.join(cwd, base))
    if transfer.output_name is not None:
        # curl resolves -o relative to the current directory, always.
        return cwd
    configured = cfg["dir"]
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return cwd


def _target_for(transfer: Transfer, cfg: Config, cwd: str) -> Target:
    directory = _resolve_base(transfer, cfg, cwd)
    if transfer.output_name is not None:
        filename: str | None = transfer.output_name
    elif transfer.needs_remote_name:
        filename = None
    else:  # pragma: no cover - the parser rejects this combination
        raise FallbackNeeded("download target could not be determined")
    if filename is not None and ("\n" in filename or "\r" in filename or "\t" in filename):
        raise FallbackNeeded(f"output name {filename!r} contains control characters")
    display = filename or guess_remote_name(transfer.url)
    return Target(url=transfer.url, directory=directory, filename=filename, display=display)


def _resume_decision(targets: Sequence[Target], cfg: Config, requested: bool) -> bool:
    """Decide whether to pass aria2's ``-c``.

    ``-C -`` always resumes.  ``auto_resume`` (on by default) additionally
    resumes when a non-empty target file is already on disk, which is the
    headline difference from curl and the reason this wrapper exists.
    """
    if requested:
        return True
    if not cfg["auto_resume"]:
        return False
    for target in targets:
        if target.filename is None:
            # Name is derived by aria2; use the guess for the existence probe.
            candidate = os.path.join(target.directory, guess_remote_name(target.url))
        else:
            candidate = os.path.join(target.directory, target.filename)
        try:
            if os.path.getsize(candidate) > 0:
                return True
        except OSError:
            continue
    return False


def build_plan(
    cmd: CurlCommand,
    cfg: Config,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> Aria2Plan:
    """Build the aria2c invocation for ``cmd``."""
    cwd = os.path.abspath(cwd or os.getcwd())
    env = dict(os.environ if env is None else env)
    plan = Aria2Plan()
    args = plan.args

    targets = [_target_for(transfer, cfg, cwd) for transfer in cmd.transfers]
    plan.targets = targets

    # ---- tuning -----------------------------------------------------------
    _opt(args, "split", cfg["split"])
    _opt(args, "max-connection-per-server", cfg["max_connection_per_server"])
    _opt(args, "min-split-size", cfg["min_split_size"])
    _opt(args, "file-allocation", cfg["file_allocation"])
    _opt(args, "disk-cache", cfg["disk_cache"])
    _opt(args, "max-tries", cfg["max_tries"])
    _opt(args, "retry-wait", cfg["retry_wait"])
    _opt(args, "timeout", cfg["timeout"])
    if cmd.parallel_max is not None:
        _opt(args, "max-concurrent-downloads", cmd.parallel_max)
    else:
        _opt(args, "max-concurrent-downloads", cfg["max_concurrent_downloads"])
    if cfg["lowest_speed_limit"] not in ("", "0"):
        _opt(args, "lowest-speed-limit", cfg["lowest_speed_limit"])
    if cmd.connect_timeout is not None:
        _opt(args, "connect-timeout", ceil_seconds(cmd.connect_timeout))
    else:
        _opt(args, "connect-timeout", cfg["connect_timeout"])

    # curl --retry N means N *retries* (N+1 attempts); aria2 --max-tries is
    # the total number of attempts.  0 means "never retry" in curl but
    # "unlimited" in aria2, so it maps to a single attempt.
    if cmd.retry is not None:
        # replace the config-derived value
        _replace(args, "max-tries", cmd.retry + 1)
    if cmd.retry_delay is not None:
        _replace(args, "retry-wait", cmd.retry_delay)

    # ---- overwrite / resume semantics (curl compatible by default) --------
    _flag(args, "allow-overwrite", cfg["allow_overwrite"])
    _flag(args, "auto-file-renaming", cfg["auto_file_renaming"])
    plan.resume_requested = cmd.resume
    plan.resume = _resume_decision(targets, cfg, cmd.resume)
    if plan.resume:
        args.append("--continue=true")
        plan.notes.append(
            "resuming from the existing file size (-C -)" if cmd.resume else "auto-resuming from the existing file size"
        )
    if cmd.max_time is not None:
        plan.max_time = cmd.max_time
        plan.notes.append(f"enforcing --max-time {cmd.max_time:g}s in the supervisor")

    # ---- identification / TLS --------------------------------------------
    _flag(args, "check-certificate", False if cmd.insecure else cfg["check_certificate"])
    ca = cmd.cacert or cfg["ca_certificate"]
    if ca:
        _opt(args, "ca-certificate", os.path.abspath(os.path.expanduser(ca)))
    if cmd.cert:
        _opt(args, "certificate", cmd.cert)
    if cmd.key:
        _opt(args, "private-key", cmd.key)
    min_tls = cmd.min_tls_version or cfg["min_tls_version"]
    if min_tls:
        _opt(args, "min-tls-version", min_tls)
    if cmd.ipv4:
        _flag(args, "disable-ipv6", True)
    elif cmd.ipv6:
        _flag(args, "disable-ipv6", False)
    else:
        _flag(args, "disable-ipv6", cfg["disable_ipv6"])

    # ---- request decoration ----------------------------------------------
    user_agent = cmd.user_agent if cmd.user_agent is not None else cfg["user_agent"]
    if user_agent:
        _opt(args, "user-agent", user_agent)
    referer = cmd.referer if cmd.referer is not None else cfg["referer"]
    if referer:
        _opt(args, "referer", referer)
    for header in [*cfg["headers"], *cmd.headers]:
        _opt(args, "header", header)
    if cmd.cookies_inline:
        _opt(args, "header", "Cookie: " + "; ".join(cmd.cookies_inline))
    cookie_file = cmd.cookie_file or cfg["cookie_file"]
    if cookie_file:
        _opt(args, "load-cookies", os.path.abspath(os.path.expanduser(cookie_file)))
    if cmd.cookie_jar:
        _opt(args, "save-cookies", os.path.abspath(os.path.expanduser(cmd.cookie_jar)))
    interface = cmd.interface or cfg["interface"]
    if interface:
        _opt(args, "interface", interface)
    if cmd.limit_rate:
        _opt(args, "max-download-limit", cmd.limit_rate)
    speed_limit = cmd.speed_limit or cfg["lowest_speed_limit"]
    if speed_limit and speed_limit not in ("", "0"):
        _replace(args, "lowest-speed-limit", speed_limit)

    args.extend(_proxy_args(cmd, cfg, env))
    args.extend(_auth_args(cmd))

    # ---- misc toggles -----------------------------------------------------
    if cmd.remote_time or cfg["remote_time"]:
        _flag(args, "remote-time", True)
    if cfg["content_disposition"] or cmd.remote_header_name:
        _flag(args, "content-disposition", True)
    if cfg["conditional_get"]:
        _flag(args, "conditional-get", True)
    if cmd.compressed or cfg["http_accept_gzip"]:
        _flag(args, "http-accept-gzip", True)

    # ---- output placement -------------------------------------------------
    plan.mkdirs = _mkdir_list(targets, cmd.create_dirs, cfg)
    if len(targets) == 1:
        target = targets[0]
        _opt(args, "dir", target.directory)
        if target.filename is not None:
            _opt(args, "out", target.filename)
        plan.uris = [target.url]
    else:
        plan.input_file_text = _render_input_file(targets)

    if cfg["extra_args"]:
        args.extend(cfg["extra_args"])
        plan.notes.append("extra_args from config appended verbatim")

    plan.notes.extend(cmd.notes)
    return plan


def _replace(args: list[str], name: str, value: object) -> None:
    prefix = f"--{name}="
    for index, existing in enumerate(args):
        if existing.startswith(prefix):
            args[index] = f"{prefix}{value}"
            return
    args.append(f"{prefix}{value}")


def _mkdir_list(targets: Sequence[Target], create_dirs: bool, cfg: Config) -> list[str]:
    wanted: list[str] = []
    for target in targets:
        if target.directory and target.directory not in wanted:
            wanted.append(target.directory)
    missing = [directory for directory in wanted if not os.path.isdir(directory)]
    if create_dirs:
        return missing
    # The globally configured download directory is ours to create; an explicit
    # -o path is not (the parser already refused non-existent ones).
    configured = cfg["dir"]
    if configured:
        resolved = os.path.abspath(os.path.expanduser(configured))
        if resolved in missing:
            return [resolved]
    return []


def _render_input_file(targets: Sequence[Target]) -> str:
    lines: list[str] = []
    for index, target in enumerate(targets):
        if index:
            lines.append("")
        lines.append(target.url)
        if target.filename is not None:
            lines.append(f"  out={target.filename}")
        lines.append(f"  dir={target.directory}")
    return "\n".join(lines) + "\n"


def ensure_directories(plan: Aria2Plan) -> list[str]:
    """Create directories the user asked for with --create-dirs."""
    created: list[str] = []
    for directory in plan.mkdirs:
        os.makedirs(directory, exist_ok=True)
        created.append(directory)
    return created
