#!/bin/sh
# aria2curl — user level installer.
#
#   curl -fsSL https://raw.githubusercontent.com/kevinhuang001/aria2curlwrapper/main/install-user.sh | sh
#
# Installs aria2curl into a private virtual environment under <prefix>, links
# `aria2curl` and `acurl` into <prefix>/bin and (by default) appends
# `alias curl='aria2curl'` to YOUR shell rc file.  Nothing outside your home
# directory is touched; use install-system.sh for a machine-wide install.
# Everything is undoable with --uninstall.
#
# Options:
#   --prefix DIR     install root (default: ~/.local, or $ARIA2CURL_PREFIX)
#   --source DIR     install from a local checkout instead of downloading
#   --ref REF        git ref to download (default: main, or $ARIA2CURL_REF)
#   --python PATH    interpreter used to build the venv (default: python3)
#   --alias NAMES    curl (default) | acurl | both | all | none
#   --shell NAME     bash|zsh|fish|sh|powershell (default: detected from $SHELL)
#   --uv             build the venv with uv when it is available (faster)
#   --with-aria2     install the aria2 package with the system package manager
#   --no-aria2       never touch the system package manager (default)
#   --uninstall      remove aria2curl, its venv and its alias block
#   --dry-run        print what would happen
#   -y, --yes        do not prompt
#   -h, --help       this text
set -eu

REPO_SLUG="${ARIA2CURL_REPO:-kevinhuang001/aria2curlwrapper}"
REF="${ARIA2CURL_REF:-main}"
PREFIX="${ARIA2CURL_PREFIX:-$HOME/.local}"
ALIAS_NAMES="${ARIA2CURL_ALIAS:-curl}"
SHELL_NAME="${ARIA2CURL_SHELL:-}"
SOURCE_DIR=""
PYTHON_BIN=""
WITH_ARIA2=""
DRY_RUN=0
ASSUME_YES=0
UNINSTALL=0
USE_UV="${ARIA2CURL_UV:-0}"

log() { printf '%s\n' "aria2curl: $*" >&2; }
warn() { printf '%s\n' "aria2curl: warning: $*" >&2; }
die() { printf '%s\n' "aria2curl: error: $*" >&2; exit 1; }
run() {
    if [ "$DRY_RUN" = 1 ]; then
        log "dry-run: $*"
    else
        "$@"
    fi
}
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    if [ -r "$0" ]; then
        awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    else
        echo "aria2curl user installer — see $REPO_SLUG/install-user.sh"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --source) SOURCE_DIR="$2"; shift 2 ;;
        --source=*) SOURCE_DIR="${1#*=}"; shift ;;
        --ref) REF="$2"; shift 2 ;;
        --ref=*) REF="${1#*=}"; shift ;;
        --python) PYTHON_BIN="$2"; shift 2 ;;
        --python=*) PYTHON_BIN="${1#*=}"; shift ;;
        --alias) ALIAS_NAMES="$2"; shift 2 ;;
        --alias=*) ALIAS_NAMES="${1#*=}"; shift ;;
        --shell) SHELL_NAME="$2"; shift 2 ;;
        --shell=*) SHELL_NAME="${1#*=}"; shift ;;
        --with-aria2) WITH_ARIA2="yes"; shift ;;
        --no-aria2) WITH_ARIA2="no"; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --uv) USE_UV=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (try --help)" ;;
    esac
done

# Prompts are impossible when the script arrives on stdin.
if [ -t 0 ] && [ "$ASSUME_YES" != 1 ]; then
    INTERACTIVE=1
else
    INTERACTIVE=0
fi

PREFIX="${PREFIX%/}"
VENV_DIR="$PREFIX/share/aria2curl/venv"
BIN_DIR="$PREFIX/bin"

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
    log "uninstalling aria2curl from $PREFIX"
    if [ -x "$BIN_DIR/aria2curl" ]; then
        run "$BIN_DIR/aria2curl" alias uninstall ${SHELL_NAME:+--shell "$SHELL_NAME"} ||
            warn "could not remove the alias block automatically"
    fi
    run rm -f "$BIN_DIR/aria2curl" "$BIN_DIR/acurl"
    run rm -rf "$VENV_DIR"
    run rm -rf "$PREFIX/share/aria2curl"
    log "done"
    exit 0
fi

# ------------------------------------------------------------------ python
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if have "$candidate"; then PYTHON_BIN="$candidate"; break; fi
    done
fi
[ -n "$PYTHON_BIN" ] || die "no python interpreter found; install Python 3.11+ first"
have "$PYTHON_BIN" || die "$PYTHON_BIN was not found"

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' ||
    die "$PYTHON_BIN is too old; aria2curl needs Python 3.11+"
log "using $PYTHON_BIN ($("$PYTHON_BIN" -c 'import platform; print(platform.python_version())'))"

# ------------------------------------------------------------------- source
TMP_DIR=""
cleanup() {
    if [ -n "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    return 0
}
trap cleanup EXIT INT TERM

if [ -n "$SOURCE_DIR" ]; then
    [ -f "$SOURCE_DIR/pyproject.toml" ] || die "$SOURCE_DIR does not look like an aria2curl checkout"
    SOURCE_PATH="$(cd "$SOURCE_DIR" && pwd)"
    log "installing from the local checkout $SOURCE_PATH"
elif [ -f "./pyproject.toml" ] && grep -q 'name = "aria2curl"' ./pyproject.toml 2>/dev/null; then
    SOURCE_PATH="$(pwd)"
    log "installing from the current directory $SOURCE_PATH"
else
    have curl || die "curl is required to download the source (or use --source DIR)"
    TMP_DIR="$(mktemp -d)"
    ARCHIVE_URL="https://codeload.github.com/$REPO_SLUG/tar.gz/refs/heads/$REF"
    log "downloading $ARCHIVE_URL"
    if ! run curl -fsSL "$ARCHIVE_URL" -o "$TMP_DIR/src.tar.gz"; then
        ARCHIVE_URL="https://codeload.github.com/$REPO_SLUG/tar.gz/refs/tags/$REF"
        log "retrying with $ARCHIVE_URL"
        run curl -fsSL "$ARCHIVE_URL" -o "$TMP_DIR/src.tar.gz"
    fi
    if [ "$DRY_RUN" != 1 ]; then
        tar -xzf "$TMP_DIR/src.tar.gz" -C "$TMP_DIR"
        SOURCE_PATH="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'aria2curlwrapper-*' | head -n 1)"
        [ -n "$SOURCE_PATH" ] || die "could not unpack the downloaded archive"
    else
        SOURCE_PATH="$TMP_DIR/aria2curlwrapper-$REF"
    fi
fi

# --------------------------------------------------------------------- venv
if [ "$USE_UV" = 1 ] && have uv && [ "$DRY_RUN" != 1 ]; then
    log "creating the virtual environment with uv"
    run uv venv --python "$PYTHON_BIN" "$VENV_DIR"
    run uv pip install --python "$VENV_DIR/bin/python" "$SOURCE_PATH"
else
    log "creating the virtual environment in $VENV_DIR"
    run "$PYTHON_BIN" -m venv "$VENV_DIR" ||
        die "could not create a virtual environment (on Debian/Ubuntu install python3-venv)"
    run "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
    if ! run "$VENV_DIR/bin/python" -m pip install --quiet "$SOURCE_PATH"; then
        warn "pip could not fetch dependencies (offline?); installing without them"
        warn "the plain renderer will be used instead of the rich dashboard"
        run "$VENV_DIR/bin/python" -m pip install --quiet --no-deps "$SOURCE_PATH"
    fi
fi

# ------------------------------------------------------------------ symlinks
run mkdir -p "$BIN_DIR"
for name in aria2curl acurl; do
    if [ "$DRY_RUN" != 1 ]; then
        [ -x "$VENV_DIR/bin/$name" ] || die "$VENV_DIR/bin/$name is missing; installation failed"
        ln -sf "$VENV_DIR/bin/$name" "$BIN_DIR/$name"
    else
        log "dry-run: ln -sf $VENV_DIR/bin/$name $BIN_DIR/$name"
    fi
done

# ---------------------------------------------------------------------- aria2
if [ -z "$WITH_ARIA2" ]; then
    if [ "$INTERACTIVE" = 1 ] && ! have aria2c; then
        printf 'aria2 is not installed. Install it now with the system package manager? [y/N] ' >&2
        read -r answer || answer=""
        case "$answer" in y|Y|yes|YES) WITH_ARIA2="yes" ;; *) WITH_ARIA2="no" ;; esac
    else
        WITH_ARIA2="no"
    fi
fi

if [ "$WITH_ARIA2" = "yes" ]; then
    if have aria2c; then
        log "aria2 is already installed ($(aria2c --version | head -n 1))"
    else
        SUDO=""
        if [ "$(id -u)" != 0 ] && have sudo; then
            SUDO="sudo"
        fi
        if have apt-get; then
            run $SUDO apt-get update && run $SUDO apt-get install -y aria2
        elif have dnf; then
            run $SUDO dnf install -y aria2
        elif have yum; then
            run $SUDO yum install -y aria2
        elif have pacman; then
            run $SUDO pacman -S --noconfirm aria2
        elif have apk; then
            run $SUDO apk add aria2
        elif have brew; then
            run brew install aria2
        else
            warn "no supported package manager found; install aria2 manually"
            warn "aria2curl still works without it by falling back to plain curl"
        fi
    fi
else
    have aria2c || warn "aria2c not found: aria2curl will fall back to plain curl until you install aria2"
fi

# ----------------------------------------------------------------------- alias
if [ "$ALIAS_NAMES" != "none" ]; then
    if [ "$DRY_RUN" = 1 ]; then
        log "dry-run: $BIN_DIR/aria2curl alias install --name $ALIAS_NAMES --command $BIN_DIR/aria2curl ${SHELL_NAME:+--shell $SHELL_NAME}"
    elif "$BIN_DIR/aria2curl" alias install --name "$ALIAS_NAMES" --command "$BIN_DIR/aria2curl" ${SHELL_NAME:+--shell "$SHELL_NAME"}; then
        :
    else
        warn "could not install the shell alias automatically"
        warn "run: aria2curl alias install --name $ALIAS_NAMES"
    fi
fi

# ------------------------------------------------------------------- epilogue
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not on your PATH; add it with:"
        warn "  export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac

if [ "$DRY_RUN" != 1 ]; then
    log "installed $("$BIN_DIR/aria2curl" version 2>/dev/null | head -n 1 || echo aria2curl)"
fi
log "try:    aria2curl -o file.zip https://example.com/file.zip"
log "config: aria2curl config list"
log "remove: sh install-user.sh --uninstall"
