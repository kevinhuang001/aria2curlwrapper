"""The ``aria2curl`` command line.

Two responsibilities:

* decide, for every invocation, whether aria2 can faithfully do the job;
* provide the non-curl surface: ``config``, ``alias``, ``doctor``, ``version``.

When aria2 cannot do the job the original argv is handed to ``curl`` with
``execv``, so signals, credentials and the exit code behave exactly as if curl
had been invoked directly.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Sequence, TextIO

from . import __version__
from . import alias as alias_mod
from . import configcmd
from . import curlparse
from .aria2args import Aria2Plan, build_plan
from .config import SOURCE_CLI, Config
from .errors import Aria2CurlError, ConfigError, FallbackNeeded, UsageError
from .exitcodes import CURL_FAILED_INIT, CURL_OK
from .runner import Runner, free_port

PROG = "aria2curl"
DEPTH_ENV = "ARIA2CURL_DEPTH"
SUBCOMMANDS = ("config", "alias", "doctor", "version", "help")


@dataclass
class WrapperOptions:
    assignments: list[str] = dataclass_field(default_factory=list)
    config_path: Any = ...
    mode: str | None = None
    aria2_path: str | None = None
    curl_path: str | None = None
    engine: str | None = None
    explain: bool = False
    dry_run: bool = False
    debug: bool = False
    quiet: bool = False
    no_fallback: bool = False
    help: bool = False


_VALUE_FLAGS = (
    "--acurl-set",
    "--acurl-config",
    "--acurl-mode",
    "--acurl-aria2-path",
    "--acurl-curl-path",
    "--acurl-engine",
)
_BOOL_FLAGS = (
    "--acurl-explain",
    "--acurl-dry-run",
    "--acurl-debug",
    "--acurl-quiet",
    "--acurl-no-fallback",
    "--acurl-help",
)


def extract_wrapper_options(argv: Sequence[str]) -> tuple[WrapperOptions, list[str]]:
    """Split our own ``--acurl-*`` flags out of the curl command line."""
    options = WrapperOptions()
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        name, sep, inline = token.partition("=")
        if name not in _VALUE_FLAGS and name not in _BOOL_FLAGS:
            if name.startswith("--acurl-"):
                raise UsageError(f"unknown aria2curl option {name!r} (see `{PROG} --acurl-help`)")
            rest.append(token)
            index += 1
            continue
        if name in _BOOL_FLAGS:
            if sep:
                raise UsageError(f"option {name} does not take a value")
            setattr(options, _attr_for(name), True)
            index += 1
            continue
        if sep:
            value = inline
        else:
            index += 1
            if index >= len(argv):
                raise UsageError(f"option {name} is missing its argument")
            value = argv[index]
        _apply_value_option(options, name, value)
        index += 1
    return options, rest


def _attr_for(name: str) -> str:
    return {
        "--acurl-explain": "explain",
        "--acurl-dry-run": "dry_run",
        "--acurl-debug": "debug",
        "--acurl-quiet": "quiet",
        "--acurl-no-fallback": "no_fallback",
        "--acurl-help": "help",
    }[name]


def _apply_value_option(options: WrapperOptions, name: str, value: str) -> None:
    if name == "--acurl-set":
        if "=" not in value:
            raise UsageError("--acurl-set expects KEY=VALUE")
        options.assignments.append(value)
    elif name == "--acurl-config":
        options.config_path = Path(value).expanduser() if value else None
    elif name == "--acurl-mode":
        if value not in ("auto", "aria2", "curl"):
            raise UsageError("--acurl-mode must be auto, aria2 or curl")
        options.mode = value
    elif name == "--acurl-aria2-path":
        options.aria2_path = value
    elif name == "--acurl-curl-path":
        options.curl_path = value
    elif name == "--acurl-engine":
        options.engine = value


# --------------------------------------------------------------------- helpers


def _is_tty(stream: TextIO | None = None) -> bool:
    stream = stream if stream is not None else sys.stderr
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _notice(cfg: Config, message: str, *, stream: TextIO, force: bool = False) -> None:
    if cfg["quiet"]:
        return
    if not force and not cfg["debug"] and not cfg["show_fallback_reason"]:
        return
    if not force and not cfg["debug"] and not _is_tty(stream):
        return
    prefix = f"{PROG}: "
    if _is_tty(stream) and "NO_COLOR" not in os.environ:
        print(f"\x1b[2m{prefix}{message}\x1b[0m", file=stream)
    else:
        print(f"{prefix}{message}", file=stream)


def _looks_like_shim(path: str) -> bool:
    """True when ``path`` is a wrapper script that would re-enter aria2curl.

    The system wide installer drops a ``curl`` shim in ``/usr/local/bin``.  If a
    fallback ever resolved back to it the two would call each other forever, so
    the first block of the file is checked for our own name.  That catches the
    installed shim and hand written wrappers alike, without trusting PATH order.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192)
    except OSError:
        return False
    return b"aria2curl" in head


def _looks_like_self(path: str) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:  # pragma: no cover
        return False
    candidates = set()
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if argv0:
        candidates.add(os.path.realpath(argv0))
    for name in ("aria2curl", "acurl"):
        found = shutil.which(name)
        if found:
            candidates.add(os.path.realpath(found))
    return real in candidates


def resolve_curl(cfg: Config) -> str:
    """Locate the real curl.

    Never resolves to aria2curl itself and never to a shim that mentions
    aria2curl -- the shim exists to *call* us, so handing control back to it
    would loop.  ``ARIA2CURL_REAL_CURL`` is exported by the system wide shim and
    is tried first, which keeps things working when ``/usr/local/bin`` precedes
    ``/usr/bin`` in ``PATH``.
    """
    candidates: list[str] = []
    for candidate in (
        os.environ.get("ARIA2CURL_REAL_CURL", "").strip(),
        cfg["curl_path"],
        "/usr/bin/curl",
        "/bin/curl",
        "/usr/local/bin/curl",
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    rejected: list[str] = []
    for candidate in candidates:
        if os.sep in candidate:
            path = candidate if os.path.exists(candidate) else None
        else:
            path = shutil.which(candidate)
        if not path:
            continue
        if _looks_like_self(path) or _looks_like_shim(path):
            rejected.append(path)
            continue
        return path
    detail = f"{', '.join(sorted(set(rejected)))} would re-enter aria2curl" if rejected else "curl was not found"
    raise UsageError(f"refusing to exec curl: {detail}")


def exec_curl(
    cfg: Config,
    args: Sequence[str],
    *,
    reason: str | None = None,
    stream: TextIO | None = None,
) -> int:
    """Replace this process with curl.  Returns only when exec failed."""
    stream = stream if stream is not None else sys.stderr
    if reason:
        _notice(cfg, f"using curl instead of aria2 — {reason}", stream=stream)
    path = resolve_curl(cfg)
    depth = int(os.environ.get(DEPTH_ENV, "0") or 0)
    os.environ[DEPTH_ENV] = str(depth + 1)
    for handle in (sys.stdout, sys.stderr):
        try:
            handle.flush()
        except (OSError, ValueError):  # pragma: no cover
            pass
    try:
        os.execv(path, [path, *args])
    except OSError as exc:  # pragma: no cover - exec failure
        print(f"{PROG}: cannot exec {path}: {exc}", file=stream)
        return CURL_FAILED_INIT
    return CURL_FAILED_INIT  # pragma: no cover


def load_config(options: WrapperOptions) -> Config:
    overrides: dict[str, str] = {}
    for assignment in options.assignments:
        key, _, value = assignment.partition("=")
        overrides[key] = value
    cfg = Config.load(options.config_path, overrides=overrides)
    if options.mode:
        cfg.set("mode", options.mode, source=SOURCE_CLI)
    if options.aria2_path:
        cfg.set("aria2_path", options.aria2_path, source=SOURCE_CLI)
    if options.curl_path:
        cfg.set("curl_path", options.curl_path, source=SOURCE_CLI)
    if options.engine:
        cfg.set("engine", options.engine, source=SOURCE_CLI)
    if options.debug:
        cfg.set("debug", True, source=SOURCE_CLI)
    if options.quiet:
        cfg.set("quiet", True, source=SOURCE_CLI)
    return cfg


# ------------------------------------------------------------------- downloads


@dataclass
class Decision:
    use_aria2: bool
    reason: str = ""
    plan: Aria2Plan | None = None


def decide(cfg: Config, args: Sequence[str], options: WrapperOptions, *, stream: TextIO) -> Decision:
    """Work out whether this invocation goes to aria2 or to curl."""
    if options.no_fallback:
        cfg.set("fallback_to_curl", False, source=SOURCE_CLI)
    if cfg["mode"] == "curl":
        return Decision(False, "mode is set to curl")

    if not shutil.which(cfg["aria2_path"]):
        reason = f"aria2c was not found ({cfg['aria2_path']!r})"
        if cfg["fallback_to_curl"]:
            return Decision(False, reason)
        raise Aria2CurlError(reason + "; install aria2 or set aria2_path")

    rc_reason = _curlrc_reason(cfg)
    if rc_reason:
        return Decision(False, rc_reason)

    try:
        command = curlparse.parse(
            args,
            stdout_isatty=_is_tty(sys.stdout),
            implicit_output=cfg["implicit_output"],
        )
    except FallbackNeeded as exc:
        reason = exc.reason
        if cfg["mode"] == "aria2":
            raise Aria2CurlError(f"cannot translate this curl command: {reason}") from None
        if cfg["fallback_to_curl"]:
            return Decision(False, reason)
        raise Aria2CurlError(f"cannot translate this curl command: {reason}") from None

    try:
        plan = build_plan(command, cfg)
    except FallbackNeeded as exc:
        if cfg["fallback_to_curl"] and cfg["mode"] != "aria2":
            return Decision(False, exc.reason)
        raise Aria2CurlError(f"cannot translate this curl command: {exc.reason}") from None
    return Decision(True, plan=plan)


def _curlrc_reason(cfg: Config) -> str | None:
    policy = cfg["curlrc_policy"]
    if policy == "ignore":
        return None
    for candidate in _curlrc_candidates():
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                if policy == "fallback":
                    return f"{candidate} exists and aria2 would ignore it"
                _notice(
                    cfg,
                    f"note: {candidate} is not read on the aria2 path (set curlrc_policy=fallback to always use curl)",
                    stream=sys.stderr,
                    force=True,
                )
                return None
        except OSError:  # pragma: no cover
            continue
    return None


def _curlrc_candidates() -> list[Path]:
    candidates: list[Path] = []
    curl_home = os.environ.get("CURL_HOME")
    if curl_home:
        candidates.append(Path(curl_home) / ".curlrc")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "curlrc")
    candidates.append(Path.home() / ".curlrc")
    return candidates


def _describe(cfg: Config, decision: Decision, args: Sequence[str], *, stream: TextIO) -> None:
    print(f"{PROG} {__version__}", file=stream)
    print(f"config:     {cfg.path or '(none)'}", file=stream)
    print(f"mode:       {cfg['mode']}", file=stream)
    if not decision.use_aria2:
        print(f"decision:   curl  ({decision.reason})", file=stream)
        print(f"exec:       curl {' '.join(shlex.quote(a) for a in args)}", file=stream)
        return
    plan = decision.plan
    assert plan is not None
    print(f"decision:   aria2  ({cfg['aria2_path']})", file=stream)
    print(
        f"resume:     {'yes' if plan.resume else 'no'}"
        f"{' (explicit -C -)' if plan.resume_requested else ' (auto)' if plan.resume else ''}",
        file=stream,
    )
    if plan.max_time:
        print(f"max-time:   {plan.max_time:g}s (enforced by the supervisor)", file=stream)
    for target in plan.targets:
        location = target.expected_path or os.path.join(target.directory, target.display)
        print(f"target:     {location}", file=stream)
        print(f"            <- {target.url}", file=stream)
    if plan.mkdirs:
        print(f"mkdir:      {', '.join(plan.mkdirs)}", file=stream)
    print(f"command:    {plan.command_preview(cfg['aria2_path'])}", file=stream)
    if plan.uses_input_file:
        print("input-file:", file=stream)
        for line in (plan.input_file_text or "").splitlines():
            print(f"            {line}", file=stream)
    for note in plan.notes:
        print(f"note:       {note}", file=stream)
    for warning in plan.warnings:
        print(f"warning:    {warning}", file=stream)
    changed = cfg.non_default()
    if changed:
        print("config (non-default):", file=stream)
        for key, value in changed.items():
            print(f"            {key} = {value!r}  [{cfg.sources[key]}]", file=stream)


def run_download(cfg: Config, args: Sequence[str], options: WrapperOptions, *, stream: TextIO) -> int:
    if options.explain or options.dry_run:
        decision = decide(cfg, args, options, stream=stream)
        _describe(cfg, decision, args, stream=stream)
        if not decision.use_aria2:
            print("(dry run: not executing)", file=stream)
        else:
            print("(dry run: not executing)", file=stream)
        return CURL_OK

    try:
        decision = decide(cfg, args, options, stream=stream)
    except Aria2CurlError as exc:
        print(f"{PROG}: {exc}", file=stream)
        return 2
    if not decision.use_aria2:
        return exec_curl(cfg, args, reason=decision.reason, stream=stream)

    plan = decision.plan
    assert plan is not None
    runner = Runner(cfg, plan, stream=stream, quiet=options.quiet)
    try:
        result = runner.run()
    except Aria2CurlError as exc:
        if cfg["fallback_to_curl"] and cfg["mode"] != "aria2":
            _notice(cfg, f"aria2 failed to start ({exc}); falling back to curl", stream=stream, force=True)
            return exec_curl(cfg, args, reason=None, stream=stream)
        print(f"{PROG}: {exc}", file=stream)
        return CURL_FAILED_INIT
    if result.exit_code != CURL_OK and cfg["debug"] and result.message:
        print(f"{PROG}: {result.message}", file=stream)
    return result.exit_code


# ---------------------------------------------------------------- sub-commands


def cmd_version(cfg: Config, *, stream: TextIO) -> int:
    print(f"{PROG} {__version__}", file=stream)
    print(f"python      {sys.version.split()[0]} ({sys.executable})", file=stream)
    curl_path = None
    try:
        curl_path = resolve_curl(cfg)
    except UsageError:
        pass
    print(f"curl        {_tool_version([curl_path or 'curl', '--version'])}", file=stream)
    aria2_path = shutil.which(cfg["aria2_path"])
    print(f"aria2c      {_tool_version([aria2_path or cfg['aria2_path'], '--version'])}", file=stream)
    print(f"config      {cfg.path or '(no configuration file)'}", file=stream)
    return CURL_OK


def _tool_version(argv: Sequence[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not available ({exc})"
    if proc.returncode != 0:
        return f"not available ({proc.stderr.strip() or proc.returncode})"
    first = (proc.stdout or "").splitlines()
    return first[0].strip() if first else "unknown"


def _check(label: str, ok: bool, detail: str, *, stream: TextIO, hint: str = "") -> bool:
    mark = "✔" if ok else "✘"
    if _is_tty(stream) and "NO_COLOR" not in os.environ:
        mark = f"\x1b[32m{mark}\x1b[0m" if ok else f"\x1b[31m{mark}\x1b[0m"
    print(f"  {mark} {label:<22} {detail}", file=stream)
    if not ok and hint:
        print(f"      hint: {hint}", file=stream)
    return ok


def cmd_doctor(cfg: Config, *, stream: TextIO, probe: bool = True) -> int:
    print(f"{PROG} doctor", file=stream)
    failures = 0

    py_ok = sys.version_info >= (3, 11)
    failures += not _check("python", py_ok, sys.version.split()[0], stream=stream, hint="Python 3.11+ is required")

    curl_path = None
    try:
        curl_path = resolve_curl(cfg)
        failures += not _check("curl", True, _tool_version([curl_path, "--version"]), stream=stream)
    except UsageError as exc:
        failures += not _check("curl", False, str(exc), stream=stream, hint="install curl")

    aria2_path = shutil.which(cfg["aria2_path"])
    if aria2_path:
        failures += not _check("aria2c", True, _tool_version([aria2_path, "--version"]), stream=stream)
    else:
        failures += not _check(
            "aria2c",
            False,
            "not found",
            stream=stream,
            hint="apt install aria2   (aria2curl will fall back to plain curl without it)",
        )

    try:
        import importlib.metadata as md

        rich_version = md.version("rich")
        rich_ok = True
    except Exception:
        rich_version, rich_ok = "not installed (plain renderer will be used)", False
    _check("rich", rich_ok, rich_version, stream=stream, hint="pip install rich")

    path = cfg.path
    if path is None:
        _check("config file", True, "disabled via ARIA2CURL_CONFIG", stream=stream)
    else:
        exists = Path(path).exists()
        writable = os.access(Path(path).parent if exists else Path(path).parent.parent, os.W_OK)
        failures += not _check(
            "config file",
            writable,
            f"{path} ({'exists' if exists else 'not created yet'})",
            stream=stream,
            hint="set --acurl-config to a writable path",
        )

    failures += not _check(
        "terminal",
        _is_tty(sys.stderr),
        f"stderr {'is' if _is_tty(sys.stderr) else 'is not'} a tty, TERM={os.environ.get('TERM', '?')}",
        stream=stream,
    )

    if probe and aria2_path:
        ok, detail = probe_rpc(cfg, aria2_path)
        failures += not _check("aria2 JSON-RPC", ok, detail, stream=stream, hint="aria2 must be built with RPC support")
    print("", file=stream)
    print("all good" if not failures else f"{failures} problem(s) found", file=stream)
    return 0 if not failures else 1


def probe_rpc(cfg: Config, aria2_path: str) -> tuple[bool, str]:
    """Start aria2c briefly to prove the live-progress path works."""
    import secrets
    import tempfile

    from .rpc import Aria2Rpc, Aria2RpcError

    port = free_port()
    secret = secrets.token_hex(8)
    argv = [
        aria2_path,
        "--no-conf=true",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}",
        "--quiet=true",
        "--summary-interval=0",
        "--show-console-readout=false",
    ]
    with tempfile.TemporaryFile() as log:
        try:
            proc = subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            return False, f"cannot start aria2c: {exc}"
        rpc = Aria2Rpc(port, secret, timeout=2.0)
        try:
            if not rpc.wait_until_ready(8.0, is_alive=lambda: proc.poll() is None):
                return False, "aria2c did not answer on its RPC port"
            version = rpc.version()
            detail = f"ok (aria2 {version.get('version', '?')} on port {port})"
            try:
                rpc.shutdown()
            except Aria2RpcError:
                pass
            return True, detail
        finally:
            rpc.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), 15)
                except OSError:
                    proc.kill()
                proc.wait(timeout=5)


def cmd_alias(argv: Sequence[str], *, stream: TextIO, errors: TextIO | None = None) -> int:
    errors = errors if errors is not None else sys.stderr
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(alias_usage(), file=stream)
        return 0
    verb, *rest = argv
    shell = alias_mod.detect_shell()
    names_spec = "curl"
    command = None
    dry_run = False
    index = 0
    while index < len(rest):
        token = rest[index]
        if token.startswith("--shell="):
            shell = token.split("=", 1)[1]
        elif token == "--shell" and index + 1 < len(rest):
            index += 1
            shell = rest[index]
        elif token.startswith("--name="):
            names_spec = token.split("=", 1)[1]
        elif token == "--name" and index + 1 < len(rest):
            index += 1
            names_spec = rest[index]
        elif token.startswith("--command="):
            command = token.split("=", 1)[1]
        elif token == "--command" and index + 1 < len(rest):
            index += 1
            command = rest[index]
        elif token == "--dry-run":
            dry_run = True
        else:
            print(f"{PROG}: unknown alias option {token!r}", file=errors)
            return 2
        index += 1

    try:
        names = alias_mod.parse_names(names_spec)
    except ValueError as exc:
        print(f"{PROG}: {exc}", file=errors)
        return 2
    resolved = alias_mod.resolve_command(command)

    if verb == "status":
        info = alias_mod.status(shell)
        state = "installed" if info.installed else "not installed"
        print(f"{info.path}: {state}" + (f" ({', '.join(info.names)})" if info.names else ""), file=stream)
        return 0

    if verb in ("print", "show"):
        print(alias_mod.render_block(shell, resolved, names or alias_mod.DEFAULT_NAMES), end="", file=stream)
        return 0

    if verb == "install":
        if not names:
            print(f"{PROG}: nothing to install (--name none)", file=stream)
            return 0
        info = alias_mod.install(shell, resolved, names, dry_run=dry_run)
        verb_word = "would add" if dry_run else "added"
        print(f"{verb_word} alias {', '.join(names)} -> {resolved} in {info.path}", file=stream)
        if not dry_run:
            print(f"restart your shell or run: . {info.path}", file=stream)
        return 0

    if verb in ("uninstall", "remove"):
        info = alias_mod.uninstall(shell)
        print(
            f"removed the aria2curl block from {info.path}"
            if not info.installed
            else f"nothing to remove in {info.path}",
            file=stream,
        )
        return 0

    print(f"{PROG}: unknown alias command {verb!r}", file=errors)
    print(alias_usage(), file=errors)
    return 2


def alias_usage() -> str:
    return "\n".join(
        [
            "usage: aria2curl alias <command> [options]",
            "",
            "commands:",
            "  print [--shell=NAME]        print the alias block",
            "  install [--shell=NAME]      add it to your shell rc file (idempotent)",
            "  uninstall [--shell=NAME]    remove it again",
            "  status [--shell=NAME]       report whether it is installed",
            "",
            "options:",
            "  --shell=NAME   bash (default), zsh, fish, sh, powershell (detected from $SHELL)",
            "  --name=NAMES   curl (default), acurl, both, all, none, or a list",
            "  --command=CMD  command the alias expands to (default: the aria2curl in PATH)",
            "  --dry-run      show what would change",
            "",
            "aliases are used instead of a PATH symlink because shell aliases are not",
            "inherited by execvp: aria2curl's own fallback to the real curl can never",
            "recurse back into aria2curl.",
        ]
    )


# ------------------------------------------------------------------------ usage


def usage() -> str:
    return f"""{PROG} {__version__} — a curl-compatible front end for aria2

usage:
  {PROG} [curl options] <url>...        download (accelerated with aria2 when possible)
  {PROG} config <command>               view or change the global configuration
  {PROG} alias <command>                install the `curl` alias in your shell
  {PROG} doctor                         check the environment end to end
  {PROG} version                        print component versions

wrapper options (everything else is passed to curl unchanged):
  --acurl-set KEY=VALUE     override one config value for this run only
  --acurl-config PATH       use PATH instead of the default config file
  --acurl-mode MODE         auto (default), aria2 (never fall back), curl (always)
  --acurl-engine ENGINE     auto, rich, plain, json or none
  --acurl-aria2-path PATH   aria2c binary to use
  --acurl-curl-path PATH    curl binary used for fallback
  --acurl-no-fallback       fail instead of falling back to curl
  --acurl-explain           explain the decision and exit without downloading
  --acurl-dry-run           print the aria2c command and exit
  --acurl-debug             verbose parser diagnostics on stderr
  --acurl-quiet             suppress aria2curl's own messages
  --acurl-help              this text

how it behaves:
  * arguments are parsed; anything aria2 cannot reproduce faithfully (POST data,
    byte ranges, uploads, -o -, HTTP/2, netrc, custom request methods, ...) makes
    {PROG} exec the real curl with your original argv, so scripts keep working;
  * aria2-specific knobs (chunk count, resume, connections, allocation, ...) come
    from the global configuration, not from the command line;
  * exit codes are translated from aria2 back to curl's numbering.

examples:
  {PROG} -L -o archive.tar.gz https://example.com/archive.tar.gz
  {PROG} -O https://example.com/big.iso
  {PROG} config set split 16
  {PROG} config list
"""


# ------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stream = sys.stderr

    try:
        options, rest = extract_wrapper_options(argv)
    except UsageError as exc:
        print(f"{PROG}: {exc}", file=stream)
        return 2

    if options.help:
        print(usage(), file=sys.stdout)
        return CURL_OK

    depth = int(os.environ.get(DEPTH_ENV, "0") or 0)
    if depth > 0:
        # We were started by our own curl fallback: run the real curl directly.
        cfg = Config.load(...)
        return exec_curl(cfg, rest, reason=None, stream=stream)

    if not rest:
        print(usage(), file=sys.stderr)
        return 2

    try:
        return _dispatch(options, rest, stream=stream)
    except UsageError as exc:
        print(f"{PROG}: {exc}", file=stream)
        return 2
    except ConfigError as exc:
        print(f"{PROG}: {exc}", file=stream)
        return 2
    except Aria2CurlError as exc:
        print(f"{PROG}: {exc}", file=stream)
        return CURL_FAILED_INIT
    except BrokenPipeError:  # pragma: no cover
        return CURL_OK
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def _dispatch(options: WrapperOptions, rest: list[str], *, stream: TextIO) -> int:
    head = rest[0]
    if head == "config":
        if len(rest) == 1:
            print(configcmd.config_usage(), file=stream)
            return 1
        return configcmd.run(rest[1:], load_config(options), stream=sys.stdout)
    if head == "alias":
        return cmd_alias(rest[1:], stream=sys.stdout)
    if head == "doctor":
        return cmd_doctor(load_config(options), stream=sys.stdout)
    if head == "version":
        return cmd_version(load_config(options), stream=sys.stdout)
    if head == "help":
        print(usage(), file=sys.stdout)
        return CURL_OK
    if head in ("-h", "--help") and len(rest) == 1:
        print(usage(), file=sys.stdout)
        return CURL_OK
    if head in ("-V", "--version") and len(rest) == 1:
        return cmd_version(load_config(options), stream=sys.stdout)

    cfg = load_config(options)
    if cfg["mode"] == "curl":
        return exec_curl(cfg, rest, reason="mode is set to curl", stream=stream)
    return run_download(cfg, rest, options, stream=stream)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
