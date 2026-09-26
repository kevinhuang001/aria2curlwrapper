#!/bin/sh
# aria2curl installer entry point.
#
# There are two installers:
#   install-user.sh    per-user install: private venv + `alias curl` in your rc
#   install-system.sh  machine-wide install: /usr/local + /etc, optional shim
#
# This script is a convenience alias for the user level one:
#
#   curl -fsSL https://raw.githubusercontent.com/kevinhuang001/aria2curlwrapper/main/install.sh | sh
#
# Every argument is forwarded, so `sh install.sh --alias both` works too.
set -eu

REPO_SLUG="${ARIA2CURL_REPO:-kevinhuang001/aria2curlwrapper}"
REF="${ARIA2CURL_REF:-main}"

self_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || self_dir=""

# Local checkout: run the real script directly.
if [ -n "$self_dir" ] && [ -f "$self_dir/install-user.sh" ]; then
    exec sh "$self_dir/install-user.sh" "$@"
fi

# Piped from the network ($0 is "sh"): fetch the user installer and run it.
printf '%s\n' "aria2curl: fetching install-user.sh (the user level installer)" >&2
printf '%s\n' "aria2curl: for a machine-wide install use install-system.sh instead" >&2
curl -fsSL "https://raw.githubusercontent.com/$REPO_SLUG/$REF/install-user.sh" | sh -s -- "$@"
