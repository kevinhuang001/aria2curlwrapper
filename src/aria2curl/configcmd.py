"""Implementation of the ``aria2curl config`` sub-commands."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

from .config import (
    CATEGORY_TITLES,
    CONFIG_ENV,
    FIELD_MAP,
    FIELDS,
    SOURCE_CLI,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_FILE,
    SOURCE_SYSTEM,
    Config,
    display_value,
    resolve_field,
)
from .errors import ConfigError, UsageError

SOURCE_STYLE = {
    SOURCE_DEFAULT: ("grey62", "default"),
    SOURCE_SYSTEM: ("bright_blue", "system"),
    SOURCE_FILE: ("cyan", "file"),
    SOURCE_ENV: ("magenta", "env"),
    SOURCE_CLI: ("yellow", "cli"),
}


def _rich_console(stream: TextIO):
    try:
        from rich.console import Console

        return Console(file=stream, highlight=False, soft_wrap=False)
    except Exception:  # pragma: no cover - rich is a declared dependency
        return None


def _layer(cfg: Config, options: dict[str, Any]) -> tuple[Path | None, str]:
    """Which configuration file should a write command target?"""
    if options.get("system"):
        if cfg.system_path is None:
            raise ConfigError("the system configuration layer is disabled")
        return cfg.system_path, SOURCE_SYSTEM
    if cfg.path is None:
        raise ConfigError(f"the user configuration file is disabled because {CONFIG_ENV} is empty")
    return cfg.path, SOURCE_CLI


# ------------------------------------------------------------------------ list


def _collect_rows(cfg: Config, only_changed: bool) -> list[tuple[str, str, str, str, str, str, str]]:
    rows = []
    for field in FIELDS:
        value = cfg.as_dict()[field.name]
        if only_changed and value == field.default:
            continue
        style, label = SOURCE_STYLE.get(cfg.sources[field.name], ("white", "?"))
        rows.append(
            (
                field.category,
                field.name,
                display_value(field, value),
                display_value(field, field.default),
                label,
                field.aria2 or "-",
                field.help,
            )
        )
    return rows


def _print_list_rich(cfg: Config, rows, stream: TextIO, *, only_changed: bool) -> bool:
    console = _rich_console(stream)
    if console is None or not getattr(stream, "isatty", lambda: False)():
        return False
    from rich.table import Table
    from rich.text import Text

    current_category = None
    table: Any = None
    console.print(f"[bold]aria2curl configuration[/bold]  [grey62]{cfg.path if cfg.path else '(no file)'}[/grey62]")
    for row in rows:
        category, name, value, default, source, aria2, help_text = row
        if category != current_category:
            if table is not None:
                console.print(table)
                console.print()
            current_category = category
            console.print(f"[bold cyan]{CATEGORY_TITLES.get(category, category)}[/bold cyan]")
            table = Table(box=None, padding=(0, 1), show_header=True, header_style="bold")
            table.add_column("key", style="white", no_wrap=True)
            table.add_column("value", style="bold green", no_wrap=True)
            table.add_column("source", no_wrap=True)
            table.add_column("aria2 option", style="grey62", no_wrap=True)
            table.add_column("description", style="grey70", overflow="ellipsis")
        style, label = SOURCE_STYLE.get(source, ("white", source))
        table.add_row(
            Text(name),
            Text(value),
            Text(label, style=style),
            Text(aria2),
            Text(help_text),
        )
    if table is not None:
        console.print(table)
    if only_changed and not rows:
        console.print("[yellow]no values differ from the defaults[/yellow]")
    return True


def _print_list_plain(cfg: Config, rows, stream: TextIO) -> None:
    print(f"aria2curl configuration (user: {cfg.path if cfg.path else 'disabled'})", file=stream)
    if cfg.system_path:
        print(
            f"system layer: {cfg.system_path} ({'exists' if Path(cfg.system_path).is_file() else 'absent'})",
            file=stream,
        )
    current_category = None
    for category, name, value, _default, source, aria2, help_text in rows:
        if category != current_category:
            current_category = category
            print(f"\n[{CATEGORY_TITLES.get(category, category)}]", file=stream)
        print(f"  {name:<26} {value:<28} [{source}]  {aria2:<32} {help_text}", file=stream)


def cmd_list(cfg: Config, options: dict[str, Any], *, stream: TextIO | None = None) -> int:
    stream = stream or sys.stdout
    rows = _collect_rows(cfg, bool(options.get("changed")))
    if options.get("json"):
        payload = {
            "path": str(cfg.path) if cfg.path else None,
            "values": {
                field.name: {
                    "value": cfg.as_dict()[field.name],
                    "default": field.default,
                    "source": cfg.sources[field.name],
                    "type": field.type_label,
                    "constraint": field.constraint_label,
                    "aria2": field.aria2,
                    "help": field.help,
                }
                for field in FIELDS
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), file=stream)
        return 0
    if options.get("defaults"):
        rows = [
            (c, n, display_value(FIELD_MAP[n], FIELD_MAP[n].default), d, "default", a, h)
            for c, n, _, d, _, a, h in rows
        ]
    if not _print_list_rich(cfg, rows, stream, only_changed=bool(options.get("changed"))):
        _print_list_plain(cfg, rows, stream)
    return 0


# ------------------------------------------------------------------------- get


def cmd_get(cfg: Config, options: dict[str, Any], keys: Sequence[str], *, stream: TextIO | None = None) -> int:
    stream = stream or sys.stdout
    if not keys:
        raise UsageError("config get needs at least one key")
    results: dict[str, Any] = {}
    for key in keys:
        field = resolve_field(key)
        results[field.name] = cfg.as_dict()[field.name]
    if options.get("json"):
        print(json.dumps(results, indent=2, ensure_ascii=False), file=stream)
        return 0
    for key, value in results.items():
        field = resolve_field(key)
        if options.get("explain"):
            print(f"{field.name} = {display_value(field, value)}", file=stream)
            print(f"  type:        {field.type_label} {field.constraint_label}".rstrip(), file=stream)
            print(f"  source:      {cfg.sources[field.name]}", file=stream)
            print(f"  default:     {display_value(field, field.default)}", file=stream)
            print(f"  aria2:       {field.aria2 or '-'}", file=stream)
            print(f"  description: {field.help}", file=stream)
        else:
            print(display_value(field, value), file=stream)
    return 0


# ------------------------------------------------------------------------- set


def parse_assignments(pairs: Sequence[str]) -> list[tuple[str, str]]:
    """Accept ``key value``, ``key=value`` and repeated pairs."""
    if not pairs:
        raise UsageError("config set needs a key and a value, e.g. `aria2curl config set split 16`")
    assignments: list[tuple[str, str]] = []
    index = 0
    while index < len(pairs):
        token = pairs[index]
        if "=" in token:
            key, _, value = token.partition("=")
            assignments.append((key, value))
            index += 1
            continue
        if index + 1 >= len(pairs):
            raise UsageError(f"config set: missing value for {token!r}")
        assignments.append((token, pairs[index + 1]))
        index += 2
    return assignments


def cmd_set(cfg: Config, options: dict[str, Any], pairs: Sequence[str], *, stream: TextIO | None = None) -> int:
    stream = stream or sys.stdout
    target_path, source = _layer(cfg, options)
    assignments = parse_assignments(pairs)
    applied: list[tuple[str, Any, Any]] = []
    for key, raw in assignments:
        field = resolve_field(key)
        before = cfg.as_dict()[field.name]
        value = cfg.set(field.name, raw, source=source)
        applied.append((field.name, before, value))
    if options.get("dry_run"):
        for name, before, value in applied:
            print(f"{name}: {before!r} -> {value!r} (not saved)", file=stream)
        return 0
    target = cfg.save(target_path, adopt=not options.get("system"))
    print(f"updated {target}", file=stream)
    for name, before, value in applied:
        field = resolve_field(name)
        marker = "" if before == value else f"  (was {display_value(field, before)})"
        print(f"  {name} = {display_value(field, value)}{marker}", file=stream)
    return 0


def cmd_unset(cfg: Config, options: dict[str, Any], keys: Sequence[str], *, stream: TextIO | None = None) -> int:
    stream = stream or sys.stdout
    target_path, _source = _layer(cfg, options)
    if not keys:
        raise UsageError("config unset needs at least one key (see `aria2curl config reset`)")
    changed: list[tuple[str, Any, Any]] = []
    for key in keys:
        field = resolve_field(key)
        before = cfg.as_dict()[field.name]
        value = cfg.unset(field.name)
        changed.append((field.name, before, value))
    if options.get("dry_run"):
        for name, before, value in changed:
            print(f"{name}: {before!r} -> {value!r} (not saved)", file=stream)
        return 0
    cfg.save(target_path, adopt=not options.get("system"))
    for name, before, value in changed:
        field = resolve_field(name)
        print(f"  {name} = {display_value(field, value)}  (was {display_value(field, before)}, default)", file=stream)
    return 0


def cmd_reset(cfg: Config, options: dict[str, Any], *, stream: TextIO | None = None) -> int:
    stream = stream or sys.stdout
    if not options.get("yes") and getattr(stream, "isatty", lambda: False)():
        where = cfg.system_path if options.get("system") else cfg.path
        answer = input(f"Reset every setting in {where} to its default? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted", file=stream)
            return 1
    target_path, _source = _layer(cfg, options)
    cfg.reset()
    if options.get("dry_run"):
        print("configuration reset (not saved)", file=stream)
        return 0
    target = cfg.save(target_path, adopt=not options.get("system"))
    print(f"reset {target} to defaults", file=stream)
    return 0


# ------------------------------------------------------------------- path/edit


def cmd_path(cfg: Config, options: dict[str, Any] | None = None, *, stream: TextIO | None = None) -> int:
    stream = stream or sys.stdout
    options = options or {}
    if options.get("system"):
        if cfg.system_path is None:
            print("(disabled: ARIA2CURL_SYSTEM_CONFIG is empty)", file=stream)
            return 0
        print(str(cfg.system_path), file=stream)
        return 0
    if cfg.path is None:
        print(f"(disabled: {CONFIG_ENV} is set to an empty value)", file=stream)
        return 0
    print(str(cfg.path), file=stream)
    return 0


def cmd_init(cfg: Config, options: dict[str, Any], *, stream: TextIO | None = None) -> int:
    stream = stream or sys.stdout
    target_path, _source = _layer(cfg, options)
    target = cfg.save(target_path, adopt=not options.get("system"))
    print(f"wrote {target}", file=stream)
    return 0


def cmd_edit(cfg: Config, options: dict[str, Any], *, stream: TextIO | None = None) -> int:
    stream = stream or sys.stdout
    if cfg.path is None:
        raise ConfigError(f"configuration file is disabled because {CONFIG_ENV} is empty")
    path = Path(cfg.path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cfg.render_toml(), encoding="utf-8")
    editor = options.get("editor") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    if not getattr(sys.stdin, "isatty", lambda: False)() and not options.get("force"):
        raise UsageError("config edit needs a terminal; use `config set` instead")
    code = subprocess.call([*shlex.split(editor), str(path)])
    if code != 0:
        return code
    try:
        Config.load(path, use_env=False)
    except ConfigError as exc:
        print(f"warning: {exc}", file=stream)
        return 1
    print(f"{path} is valid", file=stream)
    return 0


# ----------------------------------------------------------------- dispatching

_VERBS = ("list", "get", "set", "unset", "reset", "path", "edit", "init", "help")


def run(argv: Sequence[str], cfg: Config, *, stream: TextIO | None = None) -> int:
    """Entry point for ``aria2curl config ...``.

    ``argv`` is the argument list *after* the ``config`` keyword.
    """
    stream = stream or sys.stdout
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(config_usage(), file=stream)
        return 0 if argv else 1
    verb, *rest = argv
    if verb not in _VERBS:
        raise UsageError(f"unknown config command {verb!r}; try `aria2curl config help`")
    options, positional = _split_options(rest)
    if verb == "list":
        return cmd_list(cfg, options, stream=stream)
    if verb == "get":
        return cmd_get(cfg, options, positional, stream=stream)
    if verb == "set":
        return cmd_set(cfg, options, positional, stream=stream)
    if verb == "unset":
        return cmd_unset(cfg, options, positional, stream=stream)
    if verb == "reset":
        return cmd_reset(cfg, options, stream=stream)
    if verb == "path":
        return cmd_path(cfg, options, stream=stream)
    if verb == "init":
        return cmd_init(cfg, options, stream=stream)
    if verb == "edit":
        return cmd_edit(cfg, options, stream=stream)
    print(config_usage(), file=stream)
    return 0


def _split_options(argv: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    options: dict[str, Any] = {}
    positional: list[str] = []
    for token in argv:
        if token in ("--json",):
            options["json"] = True
        elif token in ("--changed", "--only-changed"):
            options["changed"] = True
        elif token in ("--defaults",):
            options["defaults"] = True
        elif token in ("--explain", "-e"):
            options["explain"] = True
        elif token in ("--dry-run", "-n"):
            options["dry_run"] = True
        elif token in ("--yes", "-y"):
            options["yes"] = True
        elif token in ("--force", "-f"):
            options["force"] = True
        elif token == "--system":
            options["system"] = True
        elif token.startswith("--editor="):
            options["editor"] = token.split("=", 1)[1]
        else:
            positional.append(token)
    return options, positional


def config_usage() -> str:
    return "\n".join(
        [
            "usage: aria2curl config <command> [options]",
            "",
            "commands:",
            "  list [--json] [--changed] [--defaults]   show the effective configuration",
            "  get <key>... [--json] [--explain]        print value(s) for scripting",
            "  set <key> <value> [<key> <value>...]     validate, persist and report",
            "  unset <key>...                           restore the built-in default",
            "  reset [--yes]                            restore every default",
            "  path [--system]                          print a configuration file path",
            "  init [--system]                          write the file if it is missing",
            "  edit                                     open the file in $EDITOR and validate",
            "",
            "options:",
            "  --dry-run   show what would change without writing",
            "  --system    read/write the system layer (/etc/aria2curl/config.toml)",
            "",
            "examples:",
            "  aria2curl config set split 16",
            "  aria2curl config set auto_resume true max_tries 10",
            "  aria2curl config set display.engine json",
            "  aria2curl config get split",
        ]
    )
