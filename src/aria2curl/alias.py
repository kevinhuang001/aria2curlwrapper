"""Shell alias management for ``aria2curl alias`` and the installer.

An *alias* (rather than a symlink in ``PATH``) is the safe way to make ``curl``
transparent: shell aliases are not inherited by ``execvp``, so aria2curl's own
fallback to the real curl can never recurse back into itself.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

BEGIN_MARK = "# >>> aria2curl >>>"
END_MARK = "# <<< aria2curl <<<"

#: Alias names the installer and ``alias install`` know about.
DEFAULT_NAMES = ("curl",)
KNOWN_NAMES = ("curl", "aria2curl", "acurl")


def detect_shell() -> str:
    shell = os.environ.get("SHELL", "")
    name = os.path.basename(shell)
    if name in ("bash", "zsh", "fish", "ksh", "dash", "sh"):
        return "fish" if name == "fish" else "bash"
    return "bash"


def rc_file(shell: str, home: Path | None = None) -> Path:
    home = home or Path.home()
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "sh":
        return home / ".profile"
    return home / ".bashrc"


def render_block(shell: str, command: str, names: Sequence[str]) -> str:
    """The text appended to a shell rc file."""
    lines = [BEGIN_MARK, "# Added by `aria2curl alias install`. Remove with `aria2curl alias uninstall`."]
    if shell == "fish":
        for name in names:
            lines.append(f"function {name}")
            lines.append(f"    {command} $argv")
            lines.append("end")
    elif shell == "powershell":
        for name in names:
            lines.append(f"function {name} {{ {command} @args }}")
    else:
        for name in names:
            lines.append(f"alias {name}='{command}'")
    lines.append(END_MARK)
    return "\n".join(lines) + "\n"


_BLOCK_RE = re.compile(
    re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK) + r"\n?",
    re.DOTALL,
)


def has_block(text: str) -> bool:
    return BEGIN_MARK in text and END_MARK in text


def strip_block(text: str) -> str:
    return _BLOCK_RE.sub("", text)


@dataclass
class AliasStatus:
    path: Path
    installed: bool
    names: tuple[str, ...]


def status(shell: str, home: Path | None = None) -> AliasStatus:
    path = rc_file(shell, home)
    if not path.is_file():
        return AliasStatus(path, False, ())
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return AliasStatus(path, False, ())
    if not has_block(text):
        return AliasStatus(path, False, ())
    names = []
    for match in re.finditer(r"^(?:alias\s+(\w+)=|function\s+(\w+))", text, re.MULTILINE):
        name = match.group(1) or match.group(2)
        if name and name not in names:
            names.append(name)
    return AliasStatus(path, True, tuple(names))


def install(
    shell: str,
    command: str,
    names: Sequence[str],
    *,
    home: Path | None = None,
    dry_run: bool = False,
) -> AliasStatus:
    """Idempotently add the alias block to the shell rc file."""
    path = rc_file(shell, home)
    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
    cleaned = strip_block(existing)
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    block = render_block(shell, command, names)
    updated = cleaned + block
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
    return AliasStatus(path, True, tuple(names))


def uninstall(shell: str, *, home: Path | None = None) -> AliasStatus:
    path = rc_file(shell, home)
    if not path.is_file():
        return AliasStatus(path, False, ())
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return AliasStatus(path, False, ())
    if not has_block(text):
        return AliasStatus(path, False, ())
    path.write_text(strip_block(text), encoding="utf-8")
    return AliasStatus(path, False, ())


def resolve_command(explicit: str | None = None) -> str:
    """The command the alias should expand to (absolute when possible)."""
    if explicit:
        return explicit
    found = shutil.which("aria2curl")
    if found:
        return found
    return "aria2curl"


def parse_names(spec: str) -> list[str]:
    """``curl``, ``acurl``, ``both`` or a comma separated list."""
    spec = (spec or "curl").strip()
    if spec in ("none", "no", "off"):
        return []
    if spec == "both":
        return ["curl", "acurl"]
    if spec == "all":
        return list(KNOWN_NAMES)
    names = [part.strip() for part in spec.split(",") if part.strip()]
    unknown = [name for name in names if name not in KNOWN_NAMES]
    if unknown:
        raise ValueError(f"unknown alias name(s): {', '.join(unknown)} (known: {', '.join(KNOWN_NAMES)})")
    return names
