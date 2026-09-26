"""Configuration registry, validation and persistence for aria2curl.

Everything aria2-specific lives in one flat, typed registry (``FIELDS``).  The
registry doubles as the documentation source for ``aria2curl config list`` and
as the validator for ``aria2curl config set``, so the CLI can never persist a
value the rest of the program does not understand.

The file is TOML at ``$XDG_CONFIG_HOME/aria2curl/config.toml`` (override with
``$ARIA2CURL_CONFIG``).  Values can also come from the environment
(``ARIA2CURL_<KEY>``) or from ``--acurl-set key=value``; every value remembers
where it came from so ``config list`` can show provenance.
"""

from __future__ import annotations

import difflib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .errors import ConfigError

CATEGORY_ORDER: tuple[str, ...] = ("download", "engine", "display", "notices")

CATEGORY_TITLES: Mapping[str, str] = {
    "download": "aria2 transfer tuning (mapped to aria2c options)",
    "engine": "Engine selection, fallback and process control",
    "display": "Terminal display",
    "notices": "Messages and diagnostics",
}

ENV_PREFIX = "ARIA2CURL_"
CONFIG_ENV = "ARIA2CURL_CONFIG"
SYSTEM_CONFIG_ENV = "ARIA2CURL_SYSTEM_CONFIG"
DEFAULT_SYSTEM_CONFIG = "/etc/aria2curl/config.toml"

SOURCE_DEFAULT = "default"
SOURCE_SYSTEM = "system"
SOURCE_FILE = "file"
SOURCE_ENV = "env"
SOURCE_CLI = "cli"


@dataclass(frozen=True)
class Field:
    """One configuration key."""

    name: str
    kind: str  # bool | int | float | str | list
    default: Any
    category: str
    help: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    aria2: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def type_label(self) -> str:
        if self.choices:
            return "enum"
        if self.kind == "list":
            return "list"
        return self.kind

    @property
    def constraint_label(self) -> str:
        if self.choices:
            return "|".join(self.choices)
        if self.minimum is not None and self.maximum is not None:
            return f"{_num(self.minimum)}..{_num(self.maximum)}"
        return ""

    def describe(self) -> str:
        return self.help


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _f(
    name: str,
    kind: str,
    default: Any,
    category: str,
    help: str,
    *,
    choices: Sequence[str] = (),
    minimum: float | None = None,
    maximum: float | None = None,
    aria2: str = "",
    aliases: Sequence[str] = (),
) -> Field:
    return Field(
        name=name,
        kind=kind,
        default=default,
        category=category,
        help=help,
        choices=tuple(choices),
        minimum=minimum,
        maximum=maximum,
        aria2=aria2,
        aliases=tuple(aliases),
    )


FIELDS: tuple[Field, ...] = (
    # ---------------------------------------------------------------- download
    _f(
        "split",
        "int",
        5,
        "download",
        "Parallel connections per server (chunk count). 1 disables segmentation.",
        minimum=1,
        maximum=64,
        aria2="--split",
        aliases=("s", "chunks", "connections_per_server"),
    ),
    _f(
        "max_connection_per_server",
        "int",
        5,
        "download",
        "Maximum connections to a single server.",
        minimum=1,
        maximum=64,
        aria2="-x/--max-connection-per-server",
        aliases=("x", "connections", "max_connections"),
    ),
    _f(
        "min_split_size",
        "str",
        "1M",
        "download",
        "Do not split pieces smaller than this (aria2 -k).",
        aria2="-k/--min-split-size",
        aliases=("k",),
    ),
    _f(
        "max_concurrent_downloads",
        "int",
        5,
        "download",
        "How many URLs are downloaded at the same time.",
        minimum=1,
        maximum=64,
        aria2="-j/--max-concurrent-downloads",
        aliases=("j", "jobs", "concurrency"),
    ),
    _f(
        "file_allocation",
        "str",
        "prealloc",
        "download",
        "How disk space is reserved before downloading.",
        choices=("none", "prealloc", "trunc", "falloc"),
        aria2="--file-allocation",
        aliases=("allocation",),
    ),
    _f(
        "auto_resume",
        "bool",
        True,
        "download",
        "断点续传: resume a partially downloaded target file instead of restarting it.",
        aria2="-c/--continue",
        aliases=("resume", "continue", "continue_download"),
    ),
    _f(
        "allow_overwrite",
        "bool",
        True,
        "download",
        "Overwrite an existing complete file (curl semantics).",
        aria2="--allow-overwrite",
        aliases=("overwrite",),
    ),
    _f(
        "auto_file_renaming",
        "bool",
        False,
        "download",
        "Rename to file.1, file.2 when the target exists instead of overwriting.",
        aria2="--auto-file-renaming",
        aliases=("renaming",),
    ),
    _f(
        "max_tries",
        "int",
        5,
        "download",
        "Attempts per download, 0 means unlimited.",
        minimum=0,
        maximum=100,
        aria2="-m/--max-tries",
        aliases=("tries", "retry"),
    ),
    _f(
        "retry_wait",
        "int",
        2,
        "download",
        "Seconds to wait between attempts.",
        minimum=0,
        maximum=600,
        aria2="--retry-wait",
        aliases=("retry_delay",),
    ),
    _f(
        "lowest_speed_limit",
        "str",
        "0",
        "download",
        "Abort a transfer slower than this (bytes per second, suffix K/M allowed).",
        aria2="--lowest-speed-limit",
        aliases=("speed_limit",),
    ),
    _f(
        "timeout",
        "int",
        60,
        "download",
        "Per-read timeout in seconds.",
        minimum=1,
        maximum=86400,
        aria2="--timeout",
    ),
    _f(
        "connect_timeout",
        "int",
        60,
        "download",
        "Connection setup timeout in seconds.",
        minimum=1,
        maximum=86400,
        aria2="--connect-timeout",
    ),
    _f(
        "check_certificate",
        "bool",
        True,
        "download",
        "Verify TLS certificates.",
        aria2="--check-certificate",
    ),
    _f(
        "ca_certificate",
        "str",
        "",
        "download",
        "PEM bundle used to verify servers (empty = system default).",
        aria2="--ca-certificate",
        aliases=("cacert",),
    ),
    _f(
        "user_agent",
        "str",
        "",
        "download",
        "Default User-Agent (empty = aria2's own).",
        aria2="--user-agent",
        aliases=("ua",),
    ),
    _f(
        "referer",
        "str",
        "",
        "download",
        "Default Referer header (empty = none).",
        aria2="--referer",
    ),
    _f(
        "headers",
        "list",
        [],
        "download",
        "Extra HTTP headers sent with every download.",
        aria2="--header",
    ),
    _f(
        "proxy",
        "str",
        "",
        "download",
        "Default proxy URL used when curl supplies none.",
        aria2="--all-proxy",
    ),
    _f(
        "no_proxy",
        "str",
        "",
        "download",
        "Comma separated hosts that bypass the proxy.",
        aria2="--no-proxy",
    ),
    _f(
        "interface",
        "str",
        "",
        "download",
        "Bind to this network interface / address.",
        aria2="--interface",
    ),
    _f(
        "disable_ipv6",
        "bool",
        False,
        "download",
        "Force IPv4 only.",
        aria2="--disable-ipv6",
        aliases=("ipv4",),
    ),
    _f(
        "min_tls_version",
        "str",
        "",
        "download",
        "Lowest TLS version accepted.",
        choices=("", "TLSv1.1", "TLSv1.2", "TLSv1.3"),
        aria2="--min-tls-version",
    ),
    _f(
        "remote_time",
        "bool",
        False,
        "download",
        "Set the local mtime from the server's Last-Modified header.",
        aria2="--remote-time",
        aliases=("remote_timestamp",),
    ),
    _f(
        "content_disposition",
        "bool",
        False,
        "download",
        "Honour Content-Disposition filenames (curl -J).",
        aria2="--content-disposition",
    ),
    _f(
        "conditional_get",
        "bool",
        False,
        "download",
        "Only download when the remote file is newer than the local one.",
        aria2="--conditional-get",
    ),
    _f(
        "http_accept_gzip",
        "bool",
        False,
        "download",
        "Ask the server for gzip/deflate transfer encoding (curl --compressed).",
        aria2="--http-accept-gzip",
    ),
    _f(
        "dir",
        "str",
        "",
        "download",
        "Default download directory (empty = current directory).",
        aria2="--dir",
        aliases=("directory", "output_dir"),
    ),
    _f(
        "disk_cache",
        "str",
        "0",
        "download",
        "aria2 disk cache size (0 disables the cache).",
        aria2="--disk-cache",
    ),
    _f(
        "cookie_file",
        "str",
        "",
        "download",
        "Cookie file loaded into every download (Netscape format).",
        aria2="--load-cookies",
        aliases=("load_cookies",),
    ),
    _f(
        "extra_args",
        "list",
        [],
        "download",
        "Raw extra aria2c arguments appended last (power-user escape hatch).",
        aria2="(verbatim)",
    ),
    # ------------------------------------------------------------------ engine
    _f(
        "mode",
        "str",
        "auto",
        "engine",
        "auto: aria2 when possible, curl otherwise. curl: always exec curl.",
        choices=("auto", "aria2", "curl"),
    ),
    _f(
        "aria2_path",
        "str",
        "aria2c",
        "engine",
        "Path to the aria2c binary.",
        aliases=("aria2c",),
    ),
    _f(
        "curl_path",
        "str",
        "curl",
        "engine",
        "Path to the curl binary used for fallback.",
    ),
    _f(
        "fallback_to_curl",
        "bool",
        True,
        "engine",
        "Exec curl when the command line cannot be translated faithfully.",
    ),
    _f(
        "implicit_output",
        "str",
        "auto",
        "engine",
        "Behaviour when neither -o nor -O is given and stdout is a TTY.",
        choices=("auto", "remote-name", "fallback"),
    ),
    _f(
        "curlrc_policy",
        "str",
        "warn",
        "engine",
        "What to do when ~/.curlrc exists and would be ignored by the aria2 path.",
        choices=("warn", "fallback", "ignore"),
    ),
    _f(
        "honor_proxy_env",
        "bool",
        True,
        "engine",
        "Translate http_proxy/https_proxy/ALL_PROXY/NO_PROXY for aria2.",
    ),
    _f(
        "auto_resume_retry",
        "bool",
        True,
        "engine",
        "If resume fails (aria2 exit 8), retry the download from scratch.",
    ),
    _f(
        "rpc_start_timeout",
        "float",
        10.0,
        "engine",
        "Seconds to wait for aria2's JSON-RPC endpoint to come up.",
        minimum=1.0,
        maximum=120.0,
    ),
    _f(
        "poll_interval",
        "float",
        0.3,
        "engine",
        "Seconds between progress updates.",
        minimum=0.05,
        maximum=5.0,
    ),
    _f(
        "stop_with_process",
        "bool",
        True,
        "engine",
        "Kill aria2c automatically if aria2curl dies unexpectedly.",
    ),
    _f(
        "keep_aria2_output",
        "bool",
        False,
        "engine",
        "Do not silence aria2c's own console output.",
    ),
    _f(
        "console_log_level",
        "str",
        "warn",
        "engine",
        "aria2c console log level when its output is silenced.",
        choices=("debug", "info", "notice", "warn", "error"),
    ),
    # ----------------------------------------------------------------- display
    _f(
        "engine",
        "str",
        "auto",
        "display",
        "Progress renderer. auto picks rich when importable, else plain.",
        choices=("auto", "rich", "plain", "none", "json"),
        aliases=("progress", "renderer"),
    ),
    _f(
        "style",
        "str",
        "bar",
        "display",
        "Progress bar or one compact line per download.",
        choices=("bar", "compact"),
    ),
    _f(
        "color",
        "str",
        "auto",
        "display",
        "Colour output.",
        choices=("auto", "always", "never"),
    ),
    _f(
        "unicode",
        "str",
        "auto",
        "display",
        "Use Unicode block glyphs for bars.",
        choices=("auto", "always", "never"),
        aliases=("utf8",),
    ),
    _f(
        "refresh_per_second",
        "float",
        10.0,
        "display",
        "Display refresh rate.",
        minimum=1.0,
        maximum=60.0,
    ),
    _f(
        "show_header",
        "bool",
        True,
        "display",
        "Show the status header above the progress rows.",
    ),
    _f(
        "show_connections",
        "bool",
        True,
        "display",
        "Show the number of active connections per download.",
    ),
    _f(
        "show_speed",
        "bool",
        True,
        "display",
        "Show transfer speed per download.",
    ),
    _f(
        "show_eta",
        "bool",
        True,
        "display",
        "Show the estimated time remaining.",
    ),
    _f(
        "show_summary",
        "bool",
        True,
        "display",
        "Print a summary panel when all downloads finish.",
    ),
    # ----------------------------------------------------------------- notices
    _f(
        "show_fallback_reason",
        "bool",
        True,
        "notices",
        "Explain on stderr why curl was used instead of aria2.",
    ),
    _f(
        "quiet",
        "bool",
        False,
        "notices",
        "Suppress aria2curl's own messages (progress still honours -s).",
    ),
    _f(
        "debug",
        "bool",
        False,
        "notices",
        "Print parser and aria2c command details to stderr.",
        aliases=("verbose",),
    ),
)

FIELD_MAP: Mapping[str, Field] = {f.name: f for f in FIELDS}
CATEGORY_MAP: Mapping[str, tuple[Field, ...]] = {
    cat: tuple(f for f in FIELDS if f.category == cat) for cat in CATEGORY_ORDER
}

_ALIAS_MAP: dict[str, str] = {}
for _field in FIELDS:
    _ALIAS_MAP[_field.name] = _field.name
    for _alias in _field.aliases:
        _ALIAS_MAP.setdefault(_alias, _field.name)

_STRIP_PREFIXES = ("aria2curl.", "aria2c.", "acurl.", "config.")


def all_keys() -> list[str]:
    return [f.name for f in FIELDS]


def resolve_field(key: str) -> Field:
    """Resolve a user supplied key (with aliases and section prefixes) to a Field."""
    raw = (key or "").strip()
    if not raw:
        raise ConfigError("empty configuration key")
    candidate = raw.lower().replace("-", "_").replace(" ", "_")
    for prefix in _STRIP_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break

    found = _ALIAS_MAP.get(candidate)
    if found:
        return FIELD_MAP[found]

    head, _, tail = candidate.rpartition(".")
    if head:
        for prefix in _STRIP_PREFIXES:
            if head.startswith(prefix):
                head = head[len(prefix) :]
        if head in CATEGORY_ORDER:
            found = _ALIAS_MAP.get(tail)
            if found:
                return FIELD_MAP[found]
        found = _ALIAS_MAP.get(tail)
        if found and head not in CATEGORY_ORDER:
            # tolerate "aria2.split" style namespacing
            return FIELD_MAP[found]

    close = difflib.get_close_matches(candidate, list(_ALIAS_MAP), n=3, cutoff=0.6)
    hint = f" (did you mean: {', '.join(close)}?)" if close else ""
    raise ConfigError(f"unknown configuration key {key!r}{hint}")


def _parse_bool(field: Field, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "y", "enabled"):
            return True
        if text in ("0", "false", "no", "off", "n", "disabled"):
            return False
    raise ConfigError(f"{field.name}: expected a boolean, got {value!r}")


def _parse_int(field: Field, value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field.name}: expected an integer, got {value!r}")
    try:
        result = int(str(value).strip(), 10)
    except (TypeError, ValueError):
        raise ConfigError(f"{field.name}: expected an integer, got {value!r}") from None
    return _check_range(field, result)


def _parse_float(field: Field, value: Any) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{field.name}: expected a number, got {value!r}")
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{field.name}: expected a number, got {value!r}") from None
    return float(_check_range(field, result))


def _check_range(field: Field, value: float):
    if field.minimum is not None and value < field.minimum:
        raise ConfigError(f"{field.name}: must be >= {_num(field.minimum)} (got {_num(value)})")
    if field.maximum is not None and value > field.maximum:
        raise ConfigError(f"{field.name}: must be <= {_num(field.maximum)} (got {_num(value)})")
    return value


def _parse_str(field: Field, value: Any) -> str:
    if isinstance(value, (list, tuple)):
        raise ConfigError(f"{field.name}: expected a string, got a list")
    text = "" if value is None else str(value)
    if field.choices and text not in field.choices:
        allowed = ", ".join(repr(c) if c else "'' (empty)" for c in field.choices)
        raise ConfigError(f"{field.name}: must be one of {allowed} (got {text!r})")
    return text


def _parse_list(field: Field, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{field.name}: invalid JSON list: {exc}") from None
        if not isinstance(decoded, list):
            raise ConfigError(f"{field.name}: expected a JSON list, got {type(decoded).__name__}")
        return [str(item) for item in decoded]
    if "\n" in text:
        return [line.strip() for line in text.splitlines() if line.strip()]
    # Values such as HTTP headers legitimately contain commas, so only split on
    # commas when the text clearly is not a header-ish value.
    if "," in text and ":" not in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


_COERCERS = {
    "bool": _parse_bool,
    "int": _parse_int,
    "float": _parse_float,
    "str": _parse_str,
    "list": _parse_list,
}


def coerce(field: Field, value: Any) -> Any:
    """Validate/normalise ``value`` for ``field``."""
    return _COERCERS[field.kind](field, value)


def toml_literal(field: Field, value: Any) -> str:
    """Render a value the way it should appear in the TOML file."""
    if field.kind == "bool":
        return "true" if value else "false"
    if field.kind == "int":
        return str(int(value))
    if field.kind == "float":
        return repr(float(value))
    if field.kind == "list":
        return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def display_value(field: Field, value: Any) -> str:
    """Human readable rendering used by the config CLI."""
    if field.kind == "bool":
        return "true" if value else "false"
    if field.kind == "list":
        return ", ".join(value) if value else "(empty)"
    if field.kind == "float":
        return _num(value)
    if value == "":
        return "(empty)"
    return str(value)


def default_config_path() -> Path | None:
    """Where the configuration file lives (``None`` when disabled)."""
    override = os.environ.get(CONFIG_ENV)
    if override is not None:
        override = override.strip()
        if not override:
            return None
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "aria2curl" / "config.toml"


def system_config_path() -> Path | None:
    """System wide configuration, layered underneath the user file.

    Package maintainers and ``install-system.sh`` use this to pin a known-good
    configuration for every user (notably ``curl_path``, so that the system wide
    ``curl`` shim can never recurse).  ``ARIA2CURL_SYSTEM_CONFIG`` overrides the
    location; an empty value disables the layer.
    """
    override = os.environ.get(SYSTEM_CONFIG_ENV)
    if override is not None:
        override = override.strip()
        if not override:
            return None
        return Path(override).expanduser()
    return Path(DEFAULT_SYSTEM_CONFIG)


def _flatten(table: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in table.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            yield from _flatten(value, f"{name}.")
        else:
            yield name, value


class Config:
    """A validated snapshot of the effective configuration."""

    def __init__(
        self,
        path: Path | None,
        values: Mapping[str, Any],
        sources: Mapping[str, str],
        system_path: Path | None = None,
    ):
        self.path = path
        self.system_path = system_path
        self._values = dict(values)
        self.sources = dict(sources)
        self.dirty = False

    # ------------------------------------------------------------------ loading
    @classmethod
    def defaults(cls, path: Path | None = None, system_path: Path | None = None) -> "Config":
        values = {f.name: f.default for f in FIELDS}
        sources = {f.name: SOURCE_DEFAULT for f in FIELDS}
        return cls(path, values, sources, system_path)

    @classmethod
    def load(
        cls,
        path: Path | None | object = ...,
        *,
        system_path: Path | None | object = ...,
        use_env: bool = True,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Config":
        if path is ...:
            path = default_config_path()
        if system_path is ...:
            system_path = system_config_path()
        assert path is None or isinstance(path, Path)
        assert system_path is None or isinstance(system_path, Path)
        config = cls.defaults(path, system_path)
        # Layering: defaults < system file < user file < environment < CLI.
        for candidate, source in ((system_path, SOURCE_SYSTEM), (path, SOURCE_FILE)):
            if candidate is not None and candidate.is_file():
                config._load_file(candidate, source=source)
        if use_env:
            config.apply_env()
        if overrides:
            for key, value in overrides.items():
                config.set(key, value, source=SOURCE_CLI)
        return config

    def _load_file(self, path: Path, *, source: str = SOURCE_FILE) -> None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        try:
            table = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

        seen: dict[str, str] = {}
        for flat_key, value in _flatten(table):
            try:
                field = resolve_field(flat_key)
            except ConfigError:
                raise ConfigError(f"{path}: unknown setting {flat_key!r}") from None
            if field.name in seen:
                raise ConfigError(
                    f"{path}: setting {field.name!r} defined twice ({seen[field.name]!r} and {flat_key!r})"
                )
            seen[field.name] = flat_key
            self._values[field.name] = coerce(field, value)
            self.sources[field.name] = source

    def apply_env(self, environ: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ
        for field in FIELDS:
            name = ENV_PREFIX + field.name.upper()
            if name not in env:
                continue
            raw = env[name]
            if raw == "" and field.kind not in ("str", "list"):
                continue
            self._values[field.name] = coerce(field, raw)
            self.sources[field.name] = SOURCE_ENV
        return self

    # ------------------------------------------------------------------- access
    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __contains__(self, key: object) -> bool:
        try:
            resolve_field(str(key))
        except ConfigError:
            return False
        return True

    def get(self, key: str, default: Any = None) -> Any:
        try:
            field = resolve_field(key)
        except ConfigError:
            if default is not None:
                return default
            raise
        return self._values[field.name]

    def field(self, key: str) -> Field:
        return resolve_field(key)

    def set(self, key: str, value: Any, *, source: str = SOURCE_CLI) -> Any:
        field = resolve_field(key)
        coerced = coerce(field, value)
        self._values[field.name] = coerced
        self.sources[field.name] = source
        self.dirty = True
        return coerced

    def unset(self, key: str) -> Any:
        field = resolve_field(key)
        self._values[field.name] = field.default
        self.sources[field.name] = SOURCE_DEFAULT
        self.dirty = True
        return field.default

    def reset(self) -> None:
        for field in FIELDS:
            self._values[field.name] = field.default
            self.sources[field.name] = SOURCE_DEFAULT
        self.dirty = True

    def as_dict(self) -> dict[str, Any]:
        return {f.name: self._values[f.name] for f in FIELDS}

    def source_of(self, key: str) -> str:
        return self.sources[resolve_field(key).name]

    def non_default(self) -> dict[str, Any]:
        return {f.name: self._values[f.name] for f in FIELDS if self._values[f.name] != f.default}

    # -------------------------------------------------------------------- saving
    def render_toml(self) -> str:
        lines = [
            "# aria2curl configuration",
            "#",
            "# Managed by `aria2curl config set/unset/reset`; hand editing is fine.",
            "# Run `aria2curl config list` to see defaults, types and descriptions.",
            "",
        ]
        for category in CATEGORY_ORDER:
            lines.append(f"# {CATEGORY_TITLES.get(category, category)}")
            lines.append(f"[{category}]")
            for field in CATEGORY_MAP[category]:
                action = f"  # {field.aria2}" if field.aria2 else ""
                lines.append(f"{field.name} = {toml_literal(field, self._values[field.name])}{action}")
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"

    def save(self, path: Path | None = None, *, adopt: bool = True) -> Path:
        target = path or self.path
        if target is None:
            raise ConfigError(f"no configuration file path (unset {CONFIG_ENV} to use the default location)")
        target = Path(target).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"cannot create {target.parent}: {exc}") from exc
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_text(self.render_toml(), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            raise ConfigError(f"cannot write {target}: {exc}") from exc
        if adopt:
            self.path = target
        self.dirty = False
        return target
