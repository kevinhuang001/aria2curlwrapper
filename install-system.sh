#!/bin/sh
# aria2curl — system level installer (all users).
#
#   curl -fsSL https://raw.githubusercontent.com/kevinhuang001/aria2curlwrapper/main/install-system.sh | sudo sh
#
# Installs aria2curl machine wide:
#   * a private virtual environment in <libexec>/venv
#   * `aria2curl` and `acurl` in <prefix>/bin               (default /usr/local/bin)
#   * /etc/aria2curl/config.toml pinning curl_path to the real curl
#   * /etc/profile.d/aria2curl.sh with `alias curl='aria2curl'` for login shells
#   * with --wrap-curl: <prefix>/bin/curl, a transparent shim so that even
#     scripts and other programs that shell out to `curl` get aria2
#
# The shim is safe: it exports ARIA2CURL_REAL_CURL, and aria2curl refuses to
# execute any `curl` that would resolve back into itself, so falling back can
# never recurse.
#
# Options:
#   --prefix DIR      binaries (default /usr/local, or $ARIA2CURL_PREFIX)
#   --libexec DIR     private venv (default <prefix>/lib/aria2curl)
#   --sysconfdir DIR  configuration root (default /etc)
#   --source DIR      install from a local checkout instead of downloading
#   --ref REF         git ref to download (default: main, or $ARIA2CURL_REF)
#   --python PATH     interpreter used to build the venv (default: python3)
#   --wrap-curl       install the <prefix>/bin/curl shim (scripts included)
#   --no-profile      do not write /etc/profile.d/aria2curl.sh
#   --uv              build the venv with uv when it is available (faster)
#   --with-aria2      install the aria2 package with the system package manager
#   --no-aria2        never touch the system package manager (default)
#   --force           replace an existing non-aria2curl <prefix>/bin/curl
#   --allow-non-root  install into a writable prefix without root (packaging)
#   --uninstall       remove everything this script installed
#   --purge           with --uninstall, also delete /etc/aria2curl
#   --dry-run         print what would happen
#   -y, --yes         do not prompt
#   -h, --help        this text
set -eu

REPO_SLUG="${ARIA2CURL_REPO:-kevinhuang001/aria2curlwrapper}"
REF="${ARIA2CURL_REF:-main}"
PREFIX="${ARIA2CURL_PREFIX:-/usr/local}"
LIBEXEC=""
SYSCONFDIR="${ARIA2CURL_SYSCONFDIR:-/etc}"
SOURCE_DIR=""
PYTHON_BIN=""
WRAP_CURL=0
PROFILE=1
FORCE=0
ALLOW_NON_ROOT=0
WITH_ARIA2=""
UNINSTALL=0
PURGE=0
DRY_RUN=0
ASSUME_YES=0
MARKER="aria2curl"
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
is_our_file() { [ -f "$1" ] && grep -q "$MARKER" "$1" 2>/dev/null; }

usage() {
    if [ -r "$0" ]; then
        awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    else
        echo "aria2curl system installer — see $REPO_SLUG/install-system.sh"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --libexec) LIBEXEC="$2"; shift 2 ;;
        --libexec=*) LIBEXEC="${1#*=}"; shift ;;
        --sysconfdir) SYSCONFDIR="$2"; shift 2 ;;
        --sysconfdir=*) SYSCONFDIR="${1#*=}"; shift ;;
        --source) SOURCE_DIR="$2"; shift 2 ;;
        --source=*) SOURCE_DIR="${1#*=}"; shift ;;
        --ref) REF="$2"; shift 2 ;;
        --ref=*) REF="${1#*=}"; shift ;;
        --python) PYTHON_BIN="$2"; shift 2 ;;
        --python=*) PYTHON_BIN="${1#*=}"; shift ;;
        --wrap-curl) WRAP_CURL=1; shift ;;
        --no-profile) PROFILE=0; shift ;;
        --with-aria2) WITH_ARIA2="yes"; shift ;;
        --no-aria2) WITH_ARIA2="no"; shift ;;
        --force) FORCE=1; shift ;;
        --allow-non-root) ALLOW_NON_ROOT=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge) PURGE=1; shift ;;
        --uv) USE_UV=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (try --help)" ;;
    esac
done

PREFIX="${PREFIX%/}"
[ -n "$LIBEXEC" ] || LIBEXEC="$PREFIX/lib/aria2curl"
LIBEXEC="${LIBEXEC%/}"
VENV_DIR="$LIBEXEC/venv"
BIN_DIR="$PREFIX/bin"
CONFIG_DIR="$SYSCONFDIR/aria2curl"
CONFIG_FILE="$CONFIG_DIR/config.toml"
PROFILE_FILE="$SYSCONFDIR/profile.d/aria2curl.sh"
SHIM="$BIN_DIR/curl"
STAMP="$LIBEXEC/install-system.stamp"

# ------------------------------------------------------------------- privileges
if [ "$(id -u)" != 0 ] && [ "$ALLOW_NON_ROOT" != 1 ]; then
    if have sudo && [ "${ARIA2CURL_SUDOED:-}" != 1 ]; then
        log "root is required for a system wide install; re-running with sudo"
        ARIA2CURL_SUDOED=1 exec sudo -E sh "$0" "$@"
    fi
    die "system wide install needs root (or pass --allow-non-root to install into a writable prefix)"
fi

if [ -t 0 ] && [ "$ASSUME_YES" != 1 ]; then
    INTERACTIVE=1
else
    INTERACTIVE=0
fi

# The real curl must be found before we shadow the name.
detect_real_curl() {
    for candidate in /usr/bin/curl /bin/curl /usr/local/bin/curl; do
        if [ -x "$candidate" ] && ! grep -q "$MARKER" "$candidate" 2>/dev/null; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    found="$(command -v curl || true)"
    if [ -n "$found" ] && ! grep -q "$MARKER" "$found" 2>/dev/null; then
        printf '%s\n' "$found"
        return 0
    fi
    return 1
}
REAL_CURL="$(detect_real_curl || true)"

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" = 1 ]; then
    log "uninstalling the system wide aria2curl from $PREFIX"
    if [ -e "$SHIM" ] || [ -L "$SHIM" ]; then
        if is_our_file "$SHIM"; then
            run rm -f "$SHIM"
            log "removed the curl shim"
        else
            warn "$SHIM is not an aria2curl shim; leaving it alone"
        fi
    fi
    if [ -f "$PROFILE_FILE" ]; then
        run rm -f "$PROFILE_FILE"
        log "removed $PROFILE_FILE"
    fi
    run rm -f "$BIN_DIR/aria2curl" "$BIN_DIR/acurl"
    run rm -rf "$VENV_DIR" "$LIBEXEC"
    if [ "$PURGE" = 1 ]; then
        run rm -rf "$CONFIG_DIR"
        log "removed $CONFIG_DIR"
    else
        if [ -f "$CONFIG_FILE" ]; then
            log "kept $CONFIG_FILE (use --purge to delete it)"
        fi
    fi
    log "done"
    exit 0
fi

# ------------------------------------------------------------------ preflight
[ -n "$REAL_CURL" ] || die "could not find the real curl; install curl first"
log "real curl: $REAL_CURL"

if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if have "$candidate"; then PYTHON_BIN="$candidate"; break; fi
    done
fi
[ -n "$PYTHON_BIN" ] || die "no python interpreter found; install Python 3.11+ first"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' ||
    die "$PYTHON_BIN is too old; aria2curl needs Python 3.11+"
log "using $PYTHON_BIN ($("$PYTHON_BIN" -c 'import platform; print(platform.python_version())'))"

if [ "$WRAP_CURL" != 1 ] && [ "$INTERACTIVE" = 1 ]; then
    printf 'Also install a %s shim so that scripts (not just interactive shells) use aria2? [y/N] ' "$SHIM" >&2
    read -r answer || answer=""
    case "$answer" in y|Y|yes|YES) WRAP_CURL=1 ;; esac
fi

# -------------------------------------------------------------------- source
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
    if ! run "$REAL_CURL" -fsSL "$ARCHIVE_URL" -o "$TMP_DIR/src.tar.gz"; then
        ARCHIVE_URL="https://codeload.github.com/$REPO_SLUG/tar.gz/refs/tags/$REF"
        log "retrying with $ARCHIVE_URL"
        run "$REAL_CURL" -fsSL "$ARCHIVE_URL" -o "$TMP_DIR/src.tar.gz"
    fi
    if [ "$DRY_RUN" != 1 ]; then
        tar -xzf "$TMP_DIR/src.tar.gz" -C "$TMP_DIR"
        SOURCE_PATH="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'aria2curlwrapper-*' | head -n 1)"
        [ -n "$SOURCE_PATH" ] || die "could not unpack the downloaded archive"
    else
        SOURCE_PATH="$TMP_DIR/aria2curlwrapper-$REF"
    fi
fi

# ---------------------------------------------------------------------- venv
if [ "$USE_UV" = 1 ] && have uv && [ "$DRY_RUN" != 1 ]; then
    log "creating the virtual environment with uv in $VENV_DIR"
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

run mkdir -p "$BIN_DIR"
for name in aria2curl acurl; do
    if [ "$DRY_RUN" != 1 ]; then
        [ -x "$VENV_DIR/bin/$name" ] || die "$VENV_DIR/bin/$name is missing; installation failed"
        ln -sf "$VENV_DIR/bin/$name" "$BIN_DIR/$name"
    else
        log "dry-run: ln -sf $VENV_DIR/bin/$name $BIN_DIR/$name"
    fi
done

# ------------------------------------------------------------- system config
run mkdir -p "$CONFIG_DIR"
CONFIG_WRITE=1
if [ -f "$CONFIG_FILE" ] && ! grep -q "$MARKER" "$CONFIG_FILE" 2>/dev/null; then
    if [ "$FORCE" = 1 ]; then
        run cp -p "$CONFIG_FILE" "$CONFIG_FILE.aria2curl-backup"
        warn "backed up the previous $CONFIG_FILE"
    else
        warn "$CONFIG_FILE exists and was not written by aria2curl"
        warn "therefore curl_path stays unpinned; re-run with --force to replace it (a backup is kept)"
        CONFIG_WRITE=0
    fi
else
    CONFIG_WRITE=1
fi
if [ "$CONFIG_WRITE" != 0 ]; then
    if [ "$DRY_RUN" = 1 ]; then
        log "dry-run: write $CONFIG_FILE (curl_path=$REAL_CURL)"
    else
        cat >"$CONFIG_FILE" <<EOF
# aria2curl — system wide defaults (installed by install-system.sh).
#
# Every value here is a *default*: each user can override it in
# ~/.config/aria2curl/config.toml or per run with ARIA2CURL_<KEY>.
# Inspect with: aria2curl config list --system
[engine]
# Pinned so that the /usr/local/bin/curl shim can never recurse into itself.
curl_path = "$REAL_CURL"
stop_with_process = true
EOF
        log "wrote $CONFIG_FILE"
    fi
fi

if [ "$DRY_RUN" != 1 ]; then
    mkdir -p "$LIBEXEC"
    printf 'prefix=%s\nlibexec=%s\nsysconfdir=%s\nreal_curl=%s\nwrap_curl=%s\n' \
        "$PREFIX" "$LIBEXEC" "$SYSCONFDIR" "$REAL_CURL" "$WRAP_CURL" >"$STAMP"
fi

# ------------------------------------------------------------- profile.d alias
if [ "$PROFILE" = 1 ]; then
    if [ "$DRY_RUN" = 1 ]; then
        log "dry-run: write $PROFILE_FILE"
    else
        mkdir -p "$(dirname "$PROFILE_FILE")"
        cat >"$PROFILE_FILE" <<EOF
# Make \`curl\` use aria2curl for every login shell (installed by aria2curl).
# Remove with: sh install-system.sh --uninstall
# Note: aliases only apply to interactive shells; use install-system.sh
# --wrap-curl if you also want scripts and other programs to be accelerated.
if [ -x "$BIN_DIR/aria2curl" ]; then
    alias curl='$BIN_DIR/aria2curl'
fi
EOF
        log "wrote $PROFILE_FILE"
    fi
else
    log "skipping the profile.d alias (--no-profile)"
fi

# ------------------------------------------------------------------ curl shim
if [ "$WRAP_CURL" = 1 ]; then
    if [ -e "$SHIM" ] || [ -L "$SHIM" ]; then
        if is_our_file "$SHIM"; then
            log "refreshing the existing aria2curl curl shim"
        elif [ "$FORCE" = 1 ]; then
            run cp -p "$SHIM" "$SHIM.aria2curl-backup" || true
            warn "backed up the previous $SHIM to $SHIM.aria2curl-backup"
        else
            die "$SHIM already exists and is not an aria2curl shim; re-run with --force to replace it (a backup is kept)"
        fi
    fi
    if [ "$DRY_RUN" = 1 ]; then
        log "dry-run: write the curl shim $SHIM (real curl: $REAL_CURL)"
    else
        cat >"$SHIM" <<EOF
#!/bin/sh
# Transparent \`curl\` shim installed by aria2curl (system wide).
#
# It hands everything to aria2curl, which either accelerates the download with
# aria2 or executes $REAL_CURL with the original arguments.  ARIA2CURL_REAL_CURL
# tells aria2curl which binary to fall back to, and aria2curl additionally
# refuses to execute any curl that resolves back into itself, so this can never
# recurse.  Bypass it with: $REAL_CURL ...
export ARIA2CURL_REAL_CURL="\${ARIA2CURL_REAL_CURL:-$REAL_CURL}"
if [ -x "$BIN_DIR/aria2curl" ]; then
    exec "$BIN_DIR/aria2curl" "\$@"
fi
exec "$REAL_CURL" "\$@"
EOF
        chmod 0755 "$SHIM"
        log "wrote the curl shim $SHIM"
    fi
fi

# --------------------------------------------------------------------- aria2
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
    elif have apt-get; then
        run apt-get update && run apt-get install -y aria2
    elif have dnf; then
        run dnf install -y aria2
    elif have yum; then
        run yum install -y aria2
    elif have pacman; then
        run pacman -S --noconfirm aria2
    elif have apk; then
        run apk add aria2
    elif have brew; then
        run brew install aria2
    else
        warn "no supported package manager found; install aria2 manually"
    fi
else
    have aria2c || warn "aria2c not found: aria2curl will fall back to plain curl until you install aria2"
fi

# --------------------------------------------------------------------- verify
if [ "$DRY_RUN" != 1 ]; then
    log "installed $("$BIN_DIR/aria2curl" version 2>/dev/null | head -n 1 || echo aria2curl)"
    if [ "$WRAP_CURL" = 1 ]; then
        out="$(PATH="$BIN_DIR:$PATH" "$SHIM" -sS --version 2>&1 | head -n 1 || true)"
        case "$out" in
            curl*) log "shim self-test: ok ($out)" ;;
            *) warn "shim self-test returned: ${out:-<nothing>}" ;;
        esac
        first="$(PATH="$BIN_DIR:$PATH" command -v curl || true)"
        if [ "$first" != "$SHIM" ]; then
            warn "$first precedes $SHIM in PATH; the shim will not take effect until PATH is reordered"
        fi
    fi
fi

log "try:    curl -o file.zip https://example.com/file.zip    (login shell)"
if [ "$WRAP_CURL" = 1 ]; then
    log "        curl -o file.zip https://example.com/file.zip    (any script)"
fi
log "config: aria2curl config list --system"
log "remove: sh install-system.sh --uninstall"
