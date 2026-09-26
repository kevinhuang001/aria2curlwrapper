"""Exit-code translation from aria2c back to curl's numbering.

The wrapper is meant to be a drop-in replacement, so scripts that inspect
``$?`` should keep seeing curl-shaped numbers even though aria2 did the work.
Only codes with a defensible curl counterpart are translated; everything else
degrades to ``1`` (curl's "unsupported protocol" is deliberately not used, as
it would be actively misleading).
"""

from __future__ import annotations

# curl exit codes we emit.
CURL_OK = 0
CURL_UNSUPPORTED_PROTOCOL = 1
CURL_FAILED_INIT = 2
CURL_URL_MALFORMAT = 3
CURL_COULDNT_RESOLVE_PROXY = 5
CURL_COULDNT_RESOLVE_HOST = 6
CURL_COULDNT_CONNECT = 7
CURL_PARTIAL_FILE = 18
CURL_HTTP_RETURNED_ERROR = 22
CURL_WRITE_ERROR = 23
CURL_OPERATION_TIMEDOUT = 28
CURL_RANGE_ERROR = 33
CURL_SSL_CONNECT_ERROR = 35
CURL_BAD_DOWNLOAD_RESUME = 36
CURL_TOO_MANY_REDIRECTS = 47
CURL_GOT_NOTHING = 52
CURL_LOGIN_DENIED = 67
CURL_INTERRUPTED = 130
CURL_TERMINATED = 143

# aria2c exit status -> curl exit status.
_ARIA2_TO_CURL: dict[int, int] = {
    0: CURL_OK,
    1: 1,  # unknown error
    2: CURL_OPERATION_TIMEDOUT,  # timeout
    3: CURL_HTTP_RETURNED_ERROR,  # resource not found (404/410)
    4: CURL_HTTP_RETURNED_ERROR,  # max-file-not-found reached
    5: CURL_OPERATION_TIMEDOUT,  # too slow (lowest-speed-limit)
    6: CURL_COULDNT_CONNECT,  # network problem
    7: CURL_PARTIAL_FILE,  # unfinished downloads (interrupted)
    8: CURL_BAD_DOWNLOAD_RESUME,  # server does not support resume
    9: CURL_WRITE_ERROR,  # not enough disk space
    10: 1,  # piece length differed
    11: 1,  # same file already being downloaded
    12: 1,  # same info hash already being downloaded
    13: CURL_WRITE_ERROR,  # file already existed and overwrite disabled
    14: CURL_WRITE_ERROR,  # renaming failed
    15: CURL_WRITE_ERROR,  # could not open existing file
    16: CURL_WRITE_ERROR,  # could not create/truncate file
    17: CURL_WRITE_ERROR,  # file I/O error
    18: CURL_WRITE_ERROR,  # could not create directory
    19: CURL_COULDNT_RESOLVE_HOST,  # name resolution failed
    20: CURL_URL_MALFORMAT,  # could not parse Metalink
    21: 1,  # FTP command failed
    22: CURL_GOT_NOTHING,  # bad/duplicate HTTP response header
    23: CURL_TOO_MANY_REDIRECTS,  # too many redirects
    24: CURL_LOGIN_DENIED,  # HTTP authorization failed
    25: 1,  # could not parse bencoded file
    26: CURL_HTTP_RETURNED_ERROR,  # torrent corrupted / missing
    27: CURL_URL_MALFORMAT,  # bad Magnet URI
    28: CURL_FAILED_INIT,  # bad aria2 option (our bug)
    29: CURL_GOT_NOTHING,  # remote server overloaded
    30: 1,  # could not parse JSON-RPC request
    32: CURL_HTTP_RETURNED_ERROR,  # checksum validation failed
}


def aria2_to_curl(code: int) -> int:
    """Translate an aria2c exit status into the closest curl exit status."""
    if code < 0:
        # Killed by a signal.  Python reports -signum.
        return 128 + abs(code)
    return _ARIA2_TO_CURL.get(code, 1)


def curl_status_name(code: int) -> str:
    """Short human readable name for a curl exit code (used in summaries)."""
    names = {
        0: "OK",
        1: "unsupported protocol / generic error",
        2: "failed to initialize",
        3: "URL malformed",
        5: "could not resolve proxy",
        6: "could not resolve host",
        7: "could not connect",
        18: "partial file transferred",
        22: "HTTP response code said error",
        23: "write error",
        28: "operation timed out",
        33: "HTTP range error",
        35: "SSL connect error",
        36: "bad download resume",
        47: "too many redirects",
        52: "empty reply from server",
        67: "login denied",
        130: "interrupted",
        143: "terminated",
    }
    return names.get(code, "error")
